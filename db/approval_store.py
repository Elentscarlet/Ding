#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
审批实例缓存（收支明细用）。

为什么单独落一份：
  审批实例列表接口只能按「发起时间 + 状态」拿到实例 ID，金额 / 日期 / 事由
  都在实例详情里，一条一笔调用。收支明细要按月汇总，每次全量拉详情既慢
  又容易撞限流，所以把拉过的实例存起来，下次只补新的。

约定：
  - instance_id 唯一索引，重复拉取只更新不新增
  - biz_date 存 "YYYY-MM-DD" 字符串（字典序即时间序，区间查询直接用）
  - fetched_at 用于 TTL：超过 cache_ttl_days 的实例会被重新拉一次详情
  - 这份缓存只负责"替代详情调用"；实例 ID 列表永远以接口为准，
    查不到的（已删除）实例会被删掉
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Iterable, List

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

from .mongo_store import _MongoBase


class ApprovalStore(_MongoBase):
    """审批实例缓存。"""

    index_specs = (
        ([("instance_id", ASCENDING)], {"unique": True, "name": "uniq_instance_id"}),
        ([("kind", ASCENDING), ("biz_date", ASCENDING)], {"name": "kind_date"}),
        ([("process_code", ASCENDING), ("biz_date", DESCENDING)], {"name": "code_date"}),
    )

    def __init__(self, cfg, logger: logging.Logger,
                 collection_name: str = "approval_instances"):
        super().__init__(cfg, logger, collection_name)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def get_cached(self, instance_ids: Iterable[str]) -> Dict[str, dict]:
        """返回 {instance_id: {status, result, fetched_at}}，用于判断是否需要补拉。"""
        ids = [i for i in instance_ids if i]
        if not ids:
            return {}
        try:
            cursor = self._ensure_collection().find(
                {"instance_id": {"$in": ids}},
                {"instance_id": 1, "status": 1, "result": 1, "fetched_at": 1,
                 "biz_date": 1, "amount": 1, "schema_version": 1},
            )
            return {doc["instance_id"]: doc for doc in cursor}
        except PyMongoError as e:
            self._warn_unavailable(e)
            raise

    def get_by_ids(self, instance_ids: Iterable[str]) -> List[dict]:
        """按 instance_id 批量取完整记录（汇总时用）。

        注意：只按 ID 取，**不按日期范围捞**。缓存里的记录集合必须受
        「接口返回的实例 ID」约束，否则已删除的实例会被重新捞进报表。
        """
        ids = [i for i in instance_ids if i]
        if not ids:
            return []
        try:
            return list(
                self._ensure_collection().find(
                    {"instance_id": {"$in": ids}}, {"_id": 0}
                )
            )
        except PyMongoError as e:
            self._warn_unavailable(e)
            raise

    def count(self) -> int:
        try:
            return self._ensure_collection().count_documents({})
        except PyMongoError as e:
            self._warn_unavailable(e)
            return -1

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def upsert(self, record: dict) -> None:
        """按 instance_id 写入/更新一条实例缓存。"""
        if not record.get("instance_id"):
            raise ValueError("实例缓存缺少 instance_id")
        now = datetime.now(timezone.utc)
        try:
            self._ensure_collection().update_one(
                {"instance_id": record["instance_id"]},
                {"$set": {**record, "updated_at": now}},
                upsert=True,
            )
            self._failures = 0
        except PyMongoError as e:
            self._warn_unavailable(e)
            raise

    def upsert_many(self, records: Iterable[dict]) -> int:
        written = 0
        for record in records:
            self.upsert(record)
            written += 1
        return written

    def delete(self, instance_id: str) -> bool:
        """删除一条缓存（详情接口报"实例不存在"时调用）。"""
        try:
            result = self._ensure_collection().delete_one({"instance_id": instance_id})
            return (result.deleted_count or 0) > 0
        except PyMongoError as e:
            self._warn_unavailable(e)
            raise
