#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
钉钉 OpenAPI 客户端封装：
  - access_token 自动缓存刷新
  - 获取审批实例详情
  - 批量更新假期余额
"""

import logging
import time
from typing import Optional

import requests


class DingTalkApiClient:
    OAPI_BASE = "https://oapi.dingtalk.com"
    API_BASE = "https://api.dingtalk.com"

    def __init__(self, client_id: str, client_secret: str,
                 admin_user_id: str, logger: logging.Logger):
        self.client_id = client_id
        self.client_secret = client_secret
        self.admin_user_id = admin_user_id
        self.logger = logger
        self._access_token: Optional[str] = None
        self._token_expire_at: float = 0.0

    # ------------------------------------------------------------------
    # access_token 管理
    # ------------------------------------------------------------------
    def get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expire_at - 300:
            return self._access_token

        resp = requests.get(
            f"{self.OAPI_BASE}/gettoken",
            params={"appkey": self.client_id, "appsecret": self.client_secret},
            timeout=10,
        )
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"获取 access_token 失败: {data}")

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

        resp = requests.get(url, headers=headers, params=params, timeout=15)
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(f"获取审批实例详情失败: {data}")
        return data

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

        resp = requests.post(url, params=params, json=body, timeout=15)
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"查询假期余额失败: {data}")
        return data
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

        resp = requests.post(url, params=params, json=body, timeout=15)
        data = resp.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"更新假期余额失败: {data}")
        return data