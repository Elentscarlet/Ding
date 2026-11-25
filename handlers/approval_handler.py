#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
奖励假期发放处理器。

监听奖励假期审批模板的 bpms_instance_change 事件，按实例详情里的 bizAction 分流：

  bizAction=NONE    正常发起并审批通过  -> 发放奖励假期
  bizAction=MODIFY  基于原实例修改而来  -> 按「新表单 - 原发放」的差额修正余额
  bizAction=REVOKE  由原实例撤销后重新发起 -> 扣回原实例已发放的假期

为什么要看 bizAction：
  修改/撤销一张**已通过**的审批单时，钉钉不改原实例，而是生成一个附属实例，
  新实例的 bizAction 标出动作、mainProcessInstanceId 指回原实例。如果还按
  "finish + agree 就发放"处理，修正单会被当成新单再发一次，撤销单也会再发，
  越改越多。所以必须按动作分流。

"原来发了多少"从哪来：
  1. 发放台账（RewardGrantStore，Mongo）里记着每个逻辑审批单当前生效的小时数；
  2. 台账没有（功能上线前发的老单、或 Mongo 不可用）时，回查原实例表单，
     用它当初填的小时数当原发放额。

事件可能被钉钉重投（同一实例 5 秒内两次 finish 已经出现过），
所以 NONE 分支用台账做幂等：同一实例只发一次。
"""

import asyncio
import logging

from api import DingTalkApiClient, DingTalkApiError

# 审批实例业务动作（详情接口 result.bizAction）
ACTION_NONE = "NONE"
ACTION_MODIFY = "MODIFY"
ACTION_REVOKE = "REVOKE"

# 小时数比较/调整的浮点容差（接口按 0.01 小时计价）
_EPS = 0.005


class RewardLeaveHandler:
    """只处理奖励假期审批模板的完成事件。"""

    # 审批实例状态：结束
    STATUS_FINISH = "finish"
    # 审批结果：同意
    RESULT_AGREE = "agree"
    # 单据已经结束（撤销/终止也算"动作已生效"）
    ENDED_STATUSES = ("COMPLETED", "TERMINATED", "CANCELED")

    def __init__(self, approval_cfg, api: DingTalkApiClient, logger: logging.Logger,
                 grant_store=None):
        """
        :param approval_cfg: AppConfig.approval
        :param api: 已初始化的钉钉 API 客户端
        :param logger: 日志对象
        :param grant_store: RewardGrantStore，可为 None（退化为回查原实例，且无幂等保护）
        """
        self.cfg = approval_cfg
        self.api = api
        self.logger = logger
        self.grant_store = grant_store

    @property
    def process_code(self) -> str:
        return self.cfg.reward_leave_process_code

    async def handle(self, data: dict):
        process_code = data.get("processCode", "")
        result = data.get("result", "")
        status = data.get("type", "")
        instance_id = data.get("processInstanceId", "")

        self.logger.info(
            "审批事件 | processCode=%s | type=%s | result=%s | instanceId=%s",
            process_code, status, result, instance_id,
        )

        # 只在"审批结束"时拉详情：修正单/撤销单的 result 不一定是 agree，
        # 真正的动作以详情里的 bizAction 为准，所以这里不再按 result 过滤。
        if status != self.STATUS_FINISH:
            self.logger.info("filtered 1 :%s", instance_id)
            return

        # 仅处理奖励假期审批模板（路由器已按模板分发，这里再兜一层）
        if process_code != self.cfg.reward_leave_process_code:
            self.logger.info("filtered 2 :%s", instance_id)
            return

        # 业务处理放入线程池，避免阻塞 Stream 事件循环
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._handle, instance_id)

    # ==================================================================
    # 分发
    # ==================================================================
    def _handle(self, process_instance_id: str):
        try:
            instance = self.api.get_process_instance(process_instance_id)
            if not self._succeeded(instance):
                self.logger.error("审批详情接口返回失败: %s", instance)
                return

            detail = instance.get("result") or {}
            action = self._biz_action(detail)
            main_id = self._main_instance_id(detail, process_instance_id)
            detail_status = str(detail.get("status") or "").upper()
            detail_result = str(detail.get("result") or "").lower()
            approved = (
                detail_status == "COMPLETED"
                and detail_result == self.RESULT_AGREE
            )

            self.logger.info(
                "审批实例详情 | instanceId=%s | bizAction=%s | mainInstanceId=%s | "
                "status=%s | result=%s",
                process_instance_id, action, main_id, detail_status, detail_result,
            )

            if action == ACTION_NONE:
                if not approved:
                    self.logger.info(
                        "审批未通过，不发放 | instanceId=%s", process_instance_id
                    )
                    return
                self._grant(detail, process_instance_id)

            elif action == ACTION_MODIFY:
                if not approved:
                    self.logger.info(
                        "修正单尚未审批通过，暂不调整 | instanceId=%s", process_instance_id
                    )
                    return
                self._correct(detail, process_instance_id, main_id)

            elif action == ACTION_REVOKE:
                if detail_status not in self.ENDED_STATUSES and not approved:
                    self.logger.info(
                        "撤销单尚未结束（status=%s），暂不扣回 | instanceId=%s",
                        detail_status, process_instance_id,
                    )
                    return
                self._revoke(detail, process_instance_id, main_id)

            else:
                self.logger.error(
                    "未知的 bizAction=%s，未做自动处理，请人工确认 | instanceId=%s",
                    action, process_instance_id,
                )

        except DingTalkApiError as e:
            # 重试已经在 API 层做过；走到这里说明已经放弃，
            # 这张单子不会被重推，必须人工处理。
            self.logger.error(
                "处理奖励假期审批失败，需人工处理 | instanceId=%s | code=%s | "
                "status=%s | 可重试=%s | 错误: %s",
                process_instance_id, e.code or e.errcode, e.status_code,
                e.retryable, e, exc_info=True,
            )
        except Exception as e:
            self.logger.error(
                "处理奖励假期审批失败 | instanceId=%s | 错误: %s",
                process_instance_id, e, exc_info=True,
            )

    # ==================================================================
    # 正常发放
    # ==================================================================
    def _grant(self, detail: dict, instance_id: str):
        user_id = (detail.get("originatorUserId") or "").strip()
        form_values = detail.get("formComponentValues") or []
        days = self._parse_days(form_values)
        hours = self._parse_hours(form_values)

        if not user_id:
            self.logger.warning("未获取到 originatorUserId，跳过发放")
            return
        if (days is None or days <= 0) and (hours is None or hours <= 0):
            self.logger.warning(
                "未解析到有效奖励天数/小时数，跳过发放 | days=%s | hours=%s",
                days, hours,
            )
            return

        grant_hours = float(hours or 0.0)
        if grant_hours <= 0:
            self.logger.warning("奖励小时数不是正数，跳过发放 | hours=%s", hours)
            return

        # 幂等：同一个实例重复投递时不再发放。
        # 台账里已经有这张逻辑审批单的记录（无论是正常发放、修正还是撤销），
        # 说明它已经处理过，绝不能再发一次。
        existed = self._get_grant(instance_id)
        if existed is not None:
            self.logger.info(
                "该实例已处理过（重复事件），跳过 | instanceId=%s | 状态=%s | 生效额度=%s 小时",
                instance_id, existed.get("status"), existed.get("granted_hours"),
            )
            return

        total, used = self._current_quota(user_id, instance_id)
        if total is None:
            self.logger.error(
                "无法获取当前假期余额，本次未发放，需人工补发 | instanceId=%s | "
                "userId=%s | 应发=%s 小时",
                instance_id, user_id, grant_hours,
            )
            return

        new_total = total + grant_hours
        self.logger.info(
            "准备发放奖励假期 | userId=%s | 本次=%s 小时 | 原总额=%s 小时 | "
            "已使用=%s 小时 | 发放后总额=%s 小时",
            user_id, grant_hours, total, used, new_total,
        )
        self._apply_total(
            user_id, new_total, instance_id,
            reason="OA审批通过自动发放奖励假期",
        )
        self._save_grant(instance_id, instance_id, user_id, grant_hours, ACTION_NONE)
        self.logger.info(
            "奖励假期发放完成 | instanceId=%s | userId=%s | 本次=%s 小时 | 总额=%s 小时",
            instance_id, user_id, grant_hours, round(new_total, 2),
        )

    # ==================================================================
    # 修正（bizAction=MODIFY）
    # ==================================================================
    def _correct(self, detail: dict, instance_id: str, main_id: str):
        new_hours = self._parse_hours(detail.get("formComponentValues") or [])
        if new_hours is None:
            self.logger.error(
                "修正单里没有解析到小时数，未调整，需人工处理 | instanceId=%s",
                instance_id,
            )
            return
        new_hours = max(float(new_hours), 0.0)

        old_hours, main_detail = self._previous_grant(main_id, instance_id)
        if old_hours is None:
            self.logger.error(
                "无法确定原单已发放的小时数，未调整，需人工处理 | instanceId=%s | "
                "mainInstanceId=%s",
                instance_id, main_id,
            )
            return

        user_id = (detail.get("originatorUserId") or "").strip()
        if not user_id and main_detail is not None:
            user_id = (main_detail.get("originatorUserId") or "").strip()
        if not user_id:
            self.logger.error(
                "修正单缺少发起人，未调整，需人工处理 | instanceId=%s", instance_id
            )
            return

        delta = new_hours - old_hours
        self.logger.info(
            "收到修正单 | instanceId=%s | mainInstanceId=%s | userId=%s | "
            "原发放=%s 小时 | 修正后=%s 小时 | 差额=%+.2f 小时",
            instance_id, main_id, user_id, old_hours, new_hours, delta,
        )

        if abs(delta) < _EPS:
            # 额度没变，只更新台账（也顺便把后续重复事件挡掉）
            self._save_grant(main_id, instance_id, user_id, new_hours, ACTION_MODIFY)
            self.logger.info("修正前后额度一致，无需调整 | instanceId=%s", instance_id)
            return

        total, used = self._current_quota(user_id, instance_id)
        if total is None:
            self.logger.error(
                "无法获取当前假期余额，修正未生效，需人工处理 | instanceId=%s | "
                "差额=%+.2f 小时",
                instance_id, delta,
            )
            return

        new_total = total + delta
        if new_total + _EPS < used:
            self.logger.error(
                "修正后总额 %s 小时低于已使用 %s 小时，无法自动调整，需人工处理 | "
                "instanceId=%s | userId=%s | 差额=%+.2f 小时",
                round(new_total, 2), used, instance_id, user_id, delta,
            )
            return

        new_total = max(new_total, 0.0)
        self._apply_total(
            user_id, new_total, instance_id,
            reason="OA审批修正自动调整奖励假期",
        )
        self._save_grant(main_id, instance_id, user_id, new_hours, ACTION_MODIFY)
        self.logger.info(
            "修正完成 | instanceId=%s | userId=%s | 调整=%+.2f 小时 | 新总额=%s 小时",
            instance_id, user_id, delta, round(new_total, 2),
        )

    # ==================================================================
    # 撤销（bizAction=REVOKE）
    # ==================================================================
    def _revoke(self, detail: dict, instance_id: str, main_id: str):
        granted, main_detail = self._previous_grant(main_id, instance_id)

        user_id = (detail.get("originatorUserId") or "").strip()
        if not user_id and main_detail is not None:
            user_id = (main_detail.get("originatorUserId") or "").strip()

        if granted is None:
            self.logger.error(
                "无法确定原单已发放的小时数，撤销未生效，需人工扣回 | "
                "instanceId=%s | mainInstanceId=%s",
                instance_id, main_id,
            )
            return

        self.logger.info(
            "收到撤销单 | instanceId=%s | mainInstanceId=%s | userId=%s | 应扣回=%s 小时",
            instance_id, main_id, user_id, granted,
        )

        if granted <= _EPS:
            # 台账已经是 revoked（重复事件），或本来就没发放
            self._mark_revoked(main_id, instance_id, user_id)
            self.logger.info(
                "该单没有可扣回的发放（已撤销过或未发放），跳过 | instanceId=%s",
                instance_id,
            )
            return

        if not user_id:
            self.logger.error(
                "撤销单缺少发起人，未扣回，需人工处理 | instanceId=%s", instance_id
            )
            return

        total, used = self._current_quota(user_id, instance_id)
        if total is None:
            self.logger.error(
                "无法获取当前假期余额，撤销未生效，需人工扣回 %s 小时 | instanceId=%s",
                granted, instance_id,
            )
            return

        new_total = total - granted
        if new_total + _EPS < used:
            self.logger.error(
                "扣回后总额 %s 小时低于已使用 %s 小时，无法自动扣回 %s 小时，"
                "需人工处理 | instanceId=%s | userId=%s",
                round(new_total, 2), used, granted, instance_id, user_id,
            )
            return

        new_total = max(new_total, 0.0)
        self._apply_total(
            user_id, new_total, instance_id,
            reason="OA审批撤销自动扣回奖励假期",
        )
        self._mark_revoked(main_id, instance_id, user_id)
        self.logger.info(
            "撤销完成 | instanceId=%s | userId=%s | 扣回=%s 小时 | 新总额=%s 小时",
            instance_id, user_id, granted, round(new_total, 2),
        )

    # ==================================================================
    # 已发放额度 / 余额 / 写入
    # ==================================================================
    def _previous_grant(self, main_id: str, instance_id: str):
        """取「原单已经发放的小时数」。

        :return: (小时数 或 None, 原实例详情 或 None)
                 None 表示无法确定 —— 调用方必须放弃自动调整、转人工。
        """
        # 1. 台账优先（也能正确处理连续多次修正）
        if self.grant_store is not None:
            try:
                record = self.grant_store.get(main_id)
            except Exception as e:
                self.logger.warning(
                    "读取奖励假期发放台账失败，改为回查原实例: %s", e
                )
                record = None
            if record is not None and self._same_leave_code(record):
                if str(record.get("status") or "granted") == "revoked":
                    return 0.0, None
                return float(record.get("granted_hours") or 0.0), None

        # 2. 回查原实例表单（功能上线前的老单、或台账不可用）
        if not main_id or main_id == instance_id:
            # 自己就是根实例，却带着 MODIFY/REVOKE，拿不到"原发放额"
            return None, None

        try:
            instance = self.api.get_process_instance(main_id)
        except DingTalkApiError as e:
            self.logger.error(
                "回查原审批实例失败 | mainInstanceId=%s | %s", main_id, e
            )
            return None, None

        main_detail = instance.get("result") or {}
        hours = self._parse_hours(main_detail.get("formComponentValues") or [])
        if hours is None:
            return None, main_detail
        return max(float(hours), 0.0), main_detail

    def _current_quota(self, user_id: str, instance_id: str):
        """查当前假期总额与已使用（小时）。

        :return: (总额, 已使用)；查不到时 (None, None)，调用方必须放弃调整。
        """
        try:
            data = self.api.get_vacation_quota(user_id, self.cfg.reward_leave_code)
        except DingTalkApiError as e:
            self.logger.error(
                "查询假期余额失败 | userId=%s | instanceId=%s | %s",
                user_id, instance_id, e,
            )
            return None, None

        if data.get("errcode") != 0:
            self.logger.error(
                "查询假期余额返回失败 | userId=%s | instanceId=%s | %s",
                user_id, instance_id, data,
            )
            return None, None

        quotas = (data.get("result") or {}).get("leave_quotas") or []
        if not quotas:
            self.logger.error(
                "假期余额为空，无法计算 | userId=%s | instanceId=%s | %s",
                user_id, instance_id, data,
            )
            return None, None

        quota = quotas[0]
        total = float(quota.get("quota_num_per_hour") or 0) / 100.0
        used = float(quota.get("used_num_per_hour") or 0) / 100.0
        return total, used

    def _apply_total(self, user_id: str, total_hours: float, instance_id: str,
                     reason: str) -> dict:
        """把假期总额设为 total_hours（小时）。"""
        total_hours = round(max(total_hours, 0.0), 2)
        hours_per_day = float(getattr(self.cfg, "hours_per_day", 6.0) or 6.0)
        days = total_hours / hours_per_day

        result = self.api.update_vacation_quota(
            user_id=user_id,
            leave_code=self.cfg.reward_leave_code,
            days=days,
            hours=total_hours,
            reason=reason,
        )
        self.logger.info(
            "假期余额接口返回 | instanceId=%s | userId=%s | 总额=%s 小时 | %s",
            instance_id, user_id, total_hours, result,
        )
        return result

    def _get_grant(self, root_instance_id: str):
        if self.grant_store is None:
            return None
        try:
            return self.grant_store.get(root_instance_id)
        except Exception as e:
            self.logger.warning("读取奖励假期发放台账失败: %s", e)
            return None

    def _save_grant(self, root_id: str, instance_id: str, user_id: str,
                    hours: float, action: str):
        if self.grant_store is None:
            return
        try:
            self.grant_store.upsert_grant(
                root_instance_id=root_id,
                instance_id=instance_id,
                user_id=user_id,
                leave_code=self.cfg.reward_leave_code,
                granted_hours=hours,
                biz_action=action,
            )
        except Exception as e:
            self.logger.error(
                "写入奖励假期发放台账失败 | instanceId=%s | %s", instance_id, e
            )

    def _mark_revoked(self, root_id: str, instance_id: str, user_id: str):
        if self.grant_store is None:
            return
        try:
            self.grant_store.mark_revoked(
                root_instance_id=root_id,
                instance_id=instance_id,
                user_id=user_id,
                leave_code=self.cfg.reward_leave_code,
            )
        except Exception as e:
            self.logger.error(
                "更新奖励假期发放台账失败（撤销）| instanceId=%s | %s",
                instance_id, e,
            )

    def _same_leave_code(self, record: dict) -> bool:
        code = record.get("leave_code")
        return not code or code == self.cfg.reward_leave_code

    # ==================================================================
    # 详情字段
    # ==================================================================
    @staticmethod
    def _succeeded(instance) -> bool:
        """详情接口的 success 是布尔；兼容个别返回字符串的情况。"""
        value = instance.get("success") if isinstance(instance, dict) else None
        if isinstance(value, str):
            return value.strip().lower() == "true"
        return bool(value)

    @staticmethod
    def _biz_action(detail: dict) -> str:
        return str(detail.get("bizAction") or ACTION_NONE).strip().upper()

    @staticmethod
    def _main_instance_id(detail: dict, instance_id: str) -> str:
        main = str(detail.get("mainProcessInstanceId") or "").strip()
        if main and main != instance_id:
            return main
        return instance_id

    # ==================================================================
    # 表单解析
    # ==================================================================
    @staticmethod
    def _parse_days(form_component_values: list) -> float | None:
        """
        从审批表单中解析奖励天数。

        实际表单结构（来自真实响应）：
          [
            {"componentType": "TextField",     "name": "测试（理由）", "value": "..."},
            {"componentType": "NumberField",   "name": "day",        "value": "2"},
            {"componentType": "DDAttachment",  "name": "附件（图）"}
          ]

        匹配策略（按优先级）：
          1. 名称命中白名单 -> 直接取值
          2. 否则取第一个 NumberField 的值
        """
        # 字段名白名单（按需增删）
        NAME_WHITELIST = {"day", "天数", "奖励天数", "假期天数"}

        fallback: float | None = None

        for comp in form_component_values:
            name = (comp.get("name") or "").strip()
            value = comp.get("value")

            if value in (None, "", []):
                continue

            # 优先匹配白名单字段名
            if name in NAME_WHITELIST:
                try:
                    return float(value)
                except (ValueError, TypeError):
                    continue

            # 记录第一个数字控件的值作为兜底
            if fallback is None and comp.get("componentType") == "NumberField":
                try:
                    fallback = float(value)
                except (ValueError, TypeError):
                    pass

        return fallback

    @staticmethod
    def _parse_hours(form_component_values: list) -> float | None:
        """
        从审批表单中解析奖励小时数。

        匹配策略（按优先级）：
          1. 名称命中白名单 -> 直接取值
          2. 否则取第一个 NumberField 的值
        """
        # 字段名白名单（按需增删）
        NAME_WHITELIST = {"hour", "小时数", "奖励小时数", "假期小时数"}

        fallback: float | None = None

        for comp in form_component_values:
            name = (comp.get("name") or "").strip()
            value = comp.get("value")

            if value in (None, "", []):
                continue

            # 优先匹配白名单字段名
            if name in NAME_WHITELIST:
                try:
                    return float(value)
                except (ValueError, TypeError):
                    continue

            # 记录第一个数字控件的值作为兜底
            if fallback is None and comp.get("componentType") == "NumberField":
                try:
                    fallback = float(value)
                except (ValueError, TypeError):
                    pass

        return fallback
