#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
钉钉 OpenAPI 客户端封装：
  - access_token 自动缓存刷新
  - 获取审批实例详情
  - 查询 / 批量更新假期余额
  - 错误分类 + 退避重试（区分"可安全重试"与"不可重试"）
"""

import logging
import random
import re
import time
from datetime import datetime
from typing import Any, Optional

import requests


class DingTalkApiError(RuntimeError):
    """钉钉接口调用失败。

    retryable 表示"重试这个请求是安全的"：
      - 幂等接口（查询类）：限流、5xx、连接超时、SSL 中断都可以重试；
      - 非幂等接口（写假期余额）：只有服务端明确拒绝了请求（限流/配额拒绝）
        才能重试；连接超时或 5xx 时请求可能在服务端已经生效，
        盲目重试会造成重复发放。
    """

    def __init__(self, message: str, *, code=None, errcode=None,
                 status_code=None, retryable: bool = False,
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.code = code
        self.errcode = errcode
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


# 服务端"明确拒绝、请求未被受理"的信号 —— 写接口也能安全重试
_REJECT_HINTS = ("qpslimit", "qps", "throttl", "ratelimit", "toomanyrequests")
_REJECT_ERRCODES = {88, 90018}  # 请求被限流 / 请求过于频繁
# 临时性故障 —— 只有幂等接口可以重试
_TRANSIENT_HINTS = ("systembusy", "serviceunavailable", "internalerror",
                    "requesttimeout", "serverbusy", "gatewaytimeout")
_TRANSIENT_ERRCODES = {-1, 50002}  # 系统繁忙

# 钉钉限流报错里会给出解除时间，例如："限制将在 2026-10-13 10:30:01 结束"
_LIMIT_END_RE = re.compile(r"限制将在\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")

_MIN_WAIT = 0.2
_MAX_WAIT = 8.0


def _as_bool(value) -> bool:
    """钉钉部分接口把布尔值返回成字符串（"true"/"false"），需要归一化。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


class DingTalkApiClient:
    OAPI_BASE = "https://oapi.dingtalk.com"
    API_BASE = "https://api.dingtalk.com"

    def __init__(self, client_id: str, client_secret: str,
                 admin_user_id: str, logger: logging.Logger,
                 max_retries: int = 3,
                 retry_base_delay: float = 0.5,
                 retry_deadline: float = 10.0,
                 timeout: float = 10.0):
        """
        :param max_retries: 单个请求的最大重试次数（总尝试次数 = max_retries + 1）
        :param retry_base_delay: 退避基数（秒），第 n 次重试等待 base * 2^(n-1)
        :param retry_deadline: 单个请求的重试总时长上限（秒），超过即放弃
        :param timeout: 单次 HTTP 请求超时（秒）
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.admin_user_id = admin_user_id
        self.logger = logger
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.retry_deadline = retry_deadline
        self.timeout = timeout
        self._access_token: Optional[str] = None
        self._token_expire_at: float = 0.0

    # ------------------------------------------------------------------
    # access_token 管理
    # ------------------------------------------------------------------
    def get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expire_at - 300:
            return self._access_token

        data = self._call(
            "GET",
            f"{self.OAPI_BASE}/gettoken",
            idempotent=True,
            describe="获取 access_token",
            params={"appkey": self.client_id, "appsecret": self.client_secret},
        )
        if not data.get("access_token"):
            raise DingTalkApiError(f"获取 access_token 失败: {data}")

        self._access_token = data["access_token"]
        self._token_expire_at = now + data.get("expires_in", 7200)
        self.logger.info("access_token 已刷新")
        return self._access_token

    # ------------------------------------------------------------------
    # 获取单个审批实例详情
    # ------------------------------------------------------------------
    def get_process_instance(self, process_instance_id: str) -> dict:
        url = f"{self.API_BASE}/v1.0/workflow/processInstances"
        headers = {"x-acs-dingtalk-access-token": self.get_access_token()}
        params = {"processInstanceId": process_instance_id}

        return self._call(
            "GET", url,
            idempotent=True,
            describe=f"获取审批实例详情({process_instance_id})",
            headers=headers,
            params=params,
        )

    # ------------------------------------------------------------------
    # 查询假期余额
    # ------------------------------------------------------------------
    def get_vacation_quota(self, user_ids: str, leave_code: str) -> dict:
        url = f"{self.OAPI_BASE}/topapi/attendance/vacation/quota/list"
        params = {"access_token": self.get_access_token()}

        body = {
            "userids": user_ids,
            "leave_code": leave_code,
            "op_userid": self.admin_user_id,
            "offset": 0,
            "size": 20,
        }

        return self._call(
            "POST", url,
            idempotent=True,
            describe=f"查询假期余额({user_ids})",
            params=params,
            json=body,
        )

    # ------------------------------------------------------------------
    # 批量更新假期余额
    # ------------------------------------------------------------------
    def update_vacation_quota(self, user_id: str, leave_code: str,
                              days: float,
                              reason: str = "OA审批通过自动发放奖励假期") -> dict:
        url = f"{self.OAPI_BASE}/topapi/attendance/vacation/quota/update"
        params = {"access_token": self.get_access_token()}

        # 生成时间戳（毫秒）
        now_ms = int(time.time() * 1000)
        one_year_later_ms = now_ms + 365 * 24 * 60 * 60 * 1000

        body = {
            "op_userid": self.admin_user_id,
            "leave_quotas": [
                {
                    "userid": user_id,
                    "leave_code": leave_code,
                    "quota_num_per_day": int(round(days * 100)),
                    "quota_cycle": str(time.localtime().tm_year),
                    "start_time": now_ms,
                    "end_time": one_year_later_ms,
                    "reason": reason,
                }
            ],
        }

        # 写接口非幂等：只有服务端明确拒绝（限流等）才允许重试
        data = self._call(
            "POST", url,
            idempotent=False,
            describe=f"更新假期余额({user_id})",
            params=params,
            json=body,
        )

        # 该接口单条操作失败时顶层 errcode 仍为 0，
        # 真正的失败原因在 result[] 的 reason 字段里，必须单独判定。
        rejected = [
            item for item in (data.get("result") or [])
            if isinstance(item, dict) and item.get("reason")
        ]
        if rejected:
            raise DingTalkApiError(
                f"更新假期余额被拒绝: {rejected}",
                errcode=data.get("errcode"),
                status_code=200,
                retryable=False,
            )
        return data

    # ------------------------------------------------------------------
    # 通讯录：子部门 / 部门成员
    # ------------------------------------------------------------------
    def list_sub_departments(self, dept_id: int) -> list:
        """获取指定部门的下一级子部门（单层，不含更深层级）。"""
        url = f"{self.OAPI_BASE}/topapi/v2/department/listsub"
        data = self._call(
            "POST", url,
            idempotent=True,
            describe=f"获取子部门列表(dept_id={dept_id})",
            params={"access_token": self.get_access_token()},
            json={"dept_id": int(dept_id), "language": "zh_CN"},
        )
        return data.get("result") or []

    def collect_department_users(self, dept_ids, include_sub_departments: bool = True,
                                 max_depth: int = 5) -> list:
        """汇总若干部门（可选含多级子部门）下的成员，返回 [{"user_id","name"}]，去重且保序。

        子部门接口需要「通讯录部门信息读权限」；未开通时只告警一次并退化为
        仅统计配置里列出的部门，不影响主流程。
        """
        ordered: list = []
        seen: set = set()
        visited_depts: set = set()
        sub_warned = False

        queue = [(int(d), 0) for d in dept_ids]
        while queue:
            dept_id, depth = queue.pop(0)
            if dept_id in visited_depts:
                continue
            visited_depts.add(dept_id)

            try:
                for user in self.list_department_users(dept_id):
                    user_id = user.get("userid")
                    if user_id and user_id not in seen:
                        seen.add(user_id)
                        ordered.append({
                            "user_id": user_id,
                            "name": user.get("name") or "",
                        })
            except DingTalkApiError as e:
                self.logger.error("获取部门 %s 的成员失败，跳过该部门: %s", dept_id, e)

            if not include_sub_departments or depth >= max_depth:
                continue
            try:
                for sub in self.list_sub_departments(dept_id):
                    sub_id = sub.get("dept_id")
                    if sub_id is not None:
                        queue.append((int(sub_id), depth + 1))
            except DingTalkApiError as e:
                if not sub_warned:
                    self.logger.warning(
                        "获取子部门失败（通常是缺少「通讯录部门信息读权限」），"
                        "本次仅统计配置中列出的部门: %s", e,
                    )
                    sub_warned = True

        return ordered

    def list_department_users(self, dept_id: int, page_size: int = 100,
                              max_pages: int = 50) -> list:
        """获取部门成员详情（含姓名），自动翻页。不含子部门。

        注意：该接口的 has_more / next_cursor 可能返回字符串，需要归一化。
        """
        url = f"{self.OAPI_BASE}/topapi/v2/user/list"
        users: list = []
        cursor = 0

        for _ in range(max_pages):
            data = self._call(
                "POST", url,
                idempotent=True,
                describe=f"获取部门成员详情(dept_id={dept_id}, cursor={cursor})",
                params={"access_token": self.get_access_token()},
                json={
                    "dept_id": int(dept_id),
                    "cursor": cursor,
                    "size": page_size,
                    "order_field": "custom",
                    "contain_access_limit": False,
                    "language": "zh_CN",
                },
            )
            result = data.get("result") or {}
            users.extend(result.get("list") or [])

            if not _as_bool(result.get("has_more")):
                return users

            next_cursor = result.get("next_cursor")
            try:
                next_cursor = int(next_cursor)
            except (TypeError, ValueError):
                return users
            if next_cursor == cursor:
                return users
            cursor = next_cursor

        self.logger.warning(
            "部门 %s 成员翻页达到上限 %d，可能未取全", dept_id, max_pages
        )
        return users

    # ------------------------------------------------------------------
    # 免登：用授权码换用户信息
    # ------------------------------------------------------------------
    def get_userinfo_by_code(self, code: str) -> dict:
        """用免登授权码换取用户信息（userid / name / sys / sys_level）。

        授权码 5 分钟内有效且**只能使用一次**，所以按非幂等处理：
        网络超时后不重试（码可能已被消耗），只有服务端明确拒绝才重试。
        """
        url = f"{self.OAPI_BASE}/topapi/v2/user/getuserinfo"
        data = self._call(
            "POST", url,
            idempotent=False,
            describe="免登码换取用户信息",
            params={"access_token": self.get_access_token()},
            json={"code": code},
        )
        return data.get("result") or {}

    # ------------------------------------------------------------------
    # 考勤：打卡详情（含外勤坐标）
    # ------------------------------------------------------------------
    # 接口硬限制
    MAX_RECORD_USER_IDS = 50      # userIds 单次最多 50 个
    MAX_RECORD_RANGE_DAYS = 7     # checkDateFrom/checkDateTo 跨度最多 7 天

    def list_attendance_records(self, user_ids, date_from: str, date_to: str,
                                is_i18n: bool = False) -> list:
        """获取打卡详情，返回 recordresult 列表。

        与大部分 topapi 不同，该接口把记录放在**顶层** recordresult 字段里，
        而不是 result 下面。

        :param user_ids: userid 列表，单次最多 50 个
        :param date_from: "yyyy-MM-dd HH:mm:ss"
        :param date_to: "yyyy-MM-dd HH:mm:ss"，与 date_from 跨度最多 7 天
        :param is_i18n: 是否海外企业
        """
        user_ids = [uid for uid in user_ids if uid]
        if not user_ids:
            return []
        if len(user_ids) > self.MAX_RECORD_USER_IDS:
            raise ValueError(
                f"userIds 单次最多 {self.MAX_RECORD_USER_IDS} 个，当前传入 {len(user_ids)} 个"
            )

        url = f"{self.OAPI_BASE}/attendance/listRecord"
        data = self._call(
            "POST", url,
            idempotent=True,
            describe=f"获取打卡详情({len(user_ids)}人 {date_from}~{date_to})",
            params={"access_token": self.get_access_token()},
            json={
                "userIds": user_ids,
                "checkDateFrom": date_from,
                "checkDateTo": date_to,
                "isI18n": is_i18n,
            },
        )
        return data.get("recordresult") or []

    # ------------------------------------------------------------------
    # 重试与错误分类（内部实现）
    # ------------------------------------------------------------------
    def _call(self, method: str, url: str, *, idempotent: bool,
              describe: str, timeout: Optional[float] = None, **kwargs) -> dict:
        """发起请求，按错误类型决定是否退避重试；成功时返回解析后的 JSON。

        失败时抛出 DingTalkApiError（携带 retryable 标记），
        不再区分 RuntimeError / 返回码，调用方只需捕获一种异常。
        """
        attempts = self.max_retries + 1
        started = time.monotonic()

        for attempt in range(1, attempts + 1):
            status_code: Optional[int] = None
            parsed = True
            try:
                resp = requests.request(
                    method, url, timeout=timeout or self.timeout, **kwargs
                )
                status_code = resp.status_code
                try:
                    data = resp.json()
                except ValueError:
                    parsed = False
                    data = {"message": (resp.text or "")[:500]}
            except requests.RequestException as e:
                data = {"message": f"{type(e).__name__}: {e}"}

            failed = (
                not parsed
                or status_code != 200
                or (isinstance(data, dict) and data.get("errcode") not in (None, 0))
                or (isinstance(data, dict) and data.get("success") is False)
            )
            if not failed:
                return data

            retryable, retry_after = self._classify(
                status_code, data, idempotent=idempotent
            )
            if retryable and attempt < attempts:
                wait = retry_after if retry_after is not None else self._backoff(attempt)
                elapsed = time.monotonic() - started
                if elapsed + wait > self.retry_deadline:
                    self.logger.error(
                        "%s：重试已耗时 %.1fs，放弃重试 | %s", describe, elapsed, data,
                    )
                    break
                self.logger.warning(
                    "%s 失败（第 %d/%d 次，可重试）: %s，%.2f 秒后重试",
                    describe, attempt, attempts, data, wait,
                )
                time.sleep(wait)
                continue

            if attempt > 1:
                self.logger.error(
                    "%s：重试 %d 次后仍失败 | %s", describe, attempt - 1, data,
                )
            raise self._build_error(describe, status_code, data, retryable, retry_after)

        # 仅在上面的 deadline 分支跳出时到达
        raise self._build_error(describe, status_code, data, True, None)

    @classmethod
    def _classify(cls, status_code: Optional[int], payload: Any, *,
                  idempotent: bool):
        """返回 (retryable, retry_after)。"""
        text = cls._payload_text(payload)
        errcode = payload.get("errcode") if isinstance(payload, dict) else None

        # 服务端明确拒绝，请求未被受理 —— 任何接口都可安全重试
        if (
            status_code == 429
            or any(hint in text for hint in _REJECT_HINTS)
            or errcode in _REJECT_ERRCODES
        ):
            return True, cls._retry_after_from(payload)

        # 结果不确定的故障：只有幂等接口可以重试
        if not idempotent:
            return False, None

        transient = (
            status_code is None            # 连接层异常（超时/SSL/连接失败）
            or status_code >= 500
            or any(hint in text for hint in _TRANSIENT_HINTS)
            or errcode in _TRANSIENT_ERRCODES
        )
        return transient, None

    @staticmethod
    def _payload_text(payload: Any) -> str:
        if not isinstance(payload, dict):
            return str(payload).lower()
        return " ".join(
            str(payload.get(key, ""))
            for key in ("code", "message", "errmsg", "subCode", "subMessage")
        ).lower()

    @staticmethod
    def _retry_after_from(payload: Any) -> Optional[float]:
        """从限流报错里解析解除时间，得到需要等待的秒数。"""
        if not isinstance(payload, dict):
            return None
        match = _LIMIT_END_RE.search(str(payload.get("message", "")))
        if not match:
            return None
        try:
            end = datetime.strptime(
                match.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            return None
        wait = (end - datetime.now()).total_seconds() + 0.2
        return max(_MIN_WAIT, min(wait, _MAX_WAIT))

    def _backoff(self, attempt: int) -> float:
        delay = min(self.retry_base_delay * (2 ** (attempt - 1)), _MAX_WAIT)
        return delay * random.uniform(0.8, 1.2)

    @staticmethod
    def _build_error(describe: str, status_code: Optional[int], payload: Any,
                     retryable: bool, retry_after: Optional[float]) -> DingTalkApiError:
        code = payload.get("code") if isinstance(payload, dict) else None
        errcode = payload.get("errcode") if isinstance(payload, dict) else None
        return DingTalkApiError(
            f"{describe}失败: {payload}",
            code=code,
            errcode=errcode,
            status_code=status_code,
            retryable=retryable,
            retry_after=retry_after,
        )
