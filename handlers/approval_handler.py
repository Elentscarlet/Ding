#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
审批事件处理器：
  监听 bpms_instance_change 事件，
  当奖励假期审批通过（result=agree）时，
  自动调用 API 为员工累加假期余额。
"""

import asyncio
import logging

import dingtalk_stream
from dingtalk_stream import AckMessage

from api import DingTalkApiClient


class ApprovalEventHandler(dingtalk_stream.EventHandler):
    """只处理奖励假期审批通过事件。"""

    # 关注的事件类型
    EVENT_TYPE = "bpms_instance_change"

    # 审批实例状态：结束
    STATUS_FINISH = "finish"
    # 审批结果：同意
    RESULT_AGREE = "agree"

    def __init__(self, approval_cfg, api: DingTalkApiClient, logger: logging.Logger):
        """
        :param approval_cfg: AppConfig.approval
        :param api: 已初始化的钉钉 API 客户端
        :param logger: 日志对象
        """
        super().__init__()
        self.cfg = approval_cfg
        self.api = api
        self.logger = logger

    async def process(self, event: dingtalk_stream.EventMessage):
        # 1. 只处理审批实例变更事件
        if event.headers.event_type != self.EVENT_TYPE:
            return AckMessage.STATUS_OK, "OK"

        data = event.data
        process_code = data.get("processCode", "")
        result = data.get("result", "")
        status = data.get("type", "")
        instance_id = data.get("processInstanceId", "")

        self.logger.info(
            "审批事件 | processCode=%s | type=%s | result=%s | instanceId=%s",
            process_code, status, result, instance_id,
        )

        # 2. 过滤：仅处理"审批完成 + 同意"
        if status != self.STATUS_FINISH or result != self.RESULT_AGREE:
            self.logger.info("filtered 1 :%s", instance_id)
            return AckMessage.STATUS_OK, "OK"

        # 3. 过滤：仅处理奖励假期审批模板
        if process_code != self.cfg.reward_leave_process_code:
            self.logger.info("filtered 2 :%s", instance_id)
            return AckMessage.STATUS_OK, "OK"

        # 4. 业务处理放入线程池，避免阻塞 Stream 事件循环
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._handle, instance_id)

        return AckMessage.STATUS_OK, "OK"

    # ==================================================================
    # 业务逻辑
    # ==================================================================
    def _handle(self, process_instance_id: str):
        try:
            # 1. 获取审批实例详情
            instance = self.api.get_process_instance(process_instance_id)
            self.logger.info("已获取审批实例详情: %s", process_instance_id)
            self.logger.debug("审批实例原始数据: %s", instance)

            # 钉钉返回结构: { "result": {...}, "success": true }
            if not instance.get("success"):
                self.logger.error("审批详情接口返回失败: %s", instance)
                return

            detail = instance.get("result", {})

            # 2. 提取关键字段
            user_id = detail.get("originatorUserId")  # 发起人即被奖励人
            form_values = detail.get("formComponentValues", [])
            days = self._parse_days(form_values)

            if not user_id:
                self.logger.warning("未获取到 originatorUserId，跳过发放")
                return
            if not days or days <= 0:
                self.logger.warning("未解析到有效奖励天数，跳过发放 | days=%s", days)
                return

            self.logger.info(
                "准备发放奖励假期 | userId=%s | days=%s | leaveCode=%s",
                user_id, days, self.cfg.reward_leave_code,
            )

            query_res = self.api.get_vacation_quota(user_id, self.cfg.reward_leave_code)
            # 解析剩余假期
            remaining_days = None
            if query_res.get("errcode") == 0:
                quotas = query_res.get("result", {}).get("leave_quotas", [])
                if quotas:
                    q = quotas[0]
                    total = q.get("quota_num_per_day", 0)  # 总余额，百分之一天
                    used = q.get("used_num_per_day", 0)  # 已使用，百分之一天
                    remaining_days = (total - used) / 100.0  # 转为天

            if remaining_days is not None:
                self.logger.info(
                    "用户当前剩余假期 | userId=%s | 剩余=%s天",
                    user_id, remaining_days,
                )
            else:
                self.logger.warning("未能获取剩余假期 | userId=%s", user_id)

            # 3. 调用 API 累加假期余额
            days = days + remaining_days
            result = self.api.update_vacation_quota(
                user_id=user_id,
                leave_code=self.cfg.reward_leave_code,
                days=days,
            )
            self.logger.info("假期余额更新成功: %s", result)

        except Exception as e:
            self.logger.error(
                "处理奖励假期审批失败 | instanceId=%s | 错误: %s",
                process_instance_id, e, exc_info=True,
            )

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
