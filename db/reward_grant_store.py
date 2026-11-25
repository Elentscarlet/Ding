#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
奖励假期发放台账（Mongo）。

为什么单独记一份：
  审批通过后是**直接改假期余额**，不是记账。可一旦这单被「修改」或「撤销」，
  钉钉不会改原实例，而是生成一个附属实例（bizAction=MODIFY / REVOKE），
  接口只给你附件事例的表单，不会告诉你"当初实际发了多少小时"。
  要按差额修正、按原额扣回，就必须自己记住每一次发放。

  台账同时承担幂等：钉钉会重投同一个 finish 事件（日志里已出现过同一实例
  5 秒内两次 finish），同一实例只允许发放一次，重复事件直接跳过。

约定：
  - root_instance_id 是「逻辑审批单」的根实例：
      正常单 = 自身；修正/撤销单 = 详情里的 mainProcessInstanceId
  - granted_hours 是当前生效的发放小时数；撤销后置 0、status=revoked
  - 台账不可用时调用方退化为回查原实例表单（能用，但没有幂等保护）
"""

import logging
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

from .mongo_store import _MongoBase


class RewardGrantStore(_MongoBase):
    """奖励假期发放台账。"""

    index_specs = (
        ([("root_instance_id", ASCENDING)], {"unique": True, "name": "uniq_root_instance"}),
        ([("user_id", ASCENDING), ("updated_at", DESCENDING)], {"name": "user_updated"}),
    )

    def __init__(self, cfg, logger: logging.Logger,
                 collection_name: str = "reward_grants"):
        super().__init__(cfg, logger, collection_name)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def get(self, root_instance_id: str):
        """取一条发放记录；没有返回 None。"""
        if not root_instance_id:
            return None
        try:
            return self._ensure_collection().find_one(
                {"root_instance_id": root_instance_id}
            )
        except PyMongoError as e:
            self._warn_unavailable(e)
            raise

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def upsert_grant(self, root_instance_id: str, instance_id: str,
                     user_id: str, leave_code: str,
                     granted_hours: float, biz_action: str) -> None:
        """记录/更新当前生效的发放额。

        正常发放、修正后的额度都走这里：granted_hours 始终是"现在生效"的值。
        """
        if not root_instance_id:
            raise ValueError("发放台账缺少 root_instance_id")
        now = datetime.now(timezone.utc)
        try:
            self._ensure_collection().update_one(
                {"root_instance_id": root_instance_id},
                {
                    "$set": {
                        "root_instance_id": root_instance_id,
                        "last_instance_id": instance_id,
                        "user_id": user_id or "",
                        "leave_code": leave_code or "",
                        "granted_hours": float(granted_hours),
                        "status": "granted",
                        "last_biz_action": biz_action,
                        "updated_at": now,
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
            self._failures = 0
        except PyMongoError as e:
            self._warn_unavailable(e)
            raise

    def mark_revoked(self, root_instance_id: str, instance_id: str,
                     user_id: str = "", leave_code: str = "") -> None:
        """标记整单已撤销：生效额度归零，之后重复的撤销事件不再扣减。"""
        if not root_instance_id:
            raise ValueError("发放台账缺少 root_instance_id")
        now = datetime.now(timezone.utc)
        try:
            self._ensure_collection().update_one(
                {"root_instance_id": root_instance_id},
                {
                    "$set": {
                        "root_instance_id": root_instance_id,
                        "last_instance_id": instance_id,
                        "status": "revoked",
                        "granted_hours": 0.0,
                        "last_biz_action": "REVOKE",
                        "updated_at": now,
                        **({"user_id": user_id} if user_id else {}),
                        **({"leave_code": leave_code} if leave_code else {}),
                    },
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )
            self._failures = 0
        except PyMongoError as e:
            self._warn_unavailable(e)
            raise
