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
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

from .mongo_store import _MongoBase

DEFAULT_QUERY_LIMIT = 5000


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
                 "biz_date": 1, "amount": 1},
            )
            return {doc["instance_id"]: doc for doc in cursor}
        except PyMongoError as e:
            self._warn_unavailable(e)
            raise

    def query(self, kinds: Optional[Iterable[str]],
              start_date: str, end_date: str,
              limit: int = DEFAULT_QUERY_LIMIT) -> List[dict]:
        """按业务日期区间查缓存（start_date / end_date 为 "YYYY-MM-DD"）。"""
        query = {"biz_date": {"$gte": start_date, "$lte": end_date}}
        if kinds:
            query["kind"] = {"$in": list(kinds)}
        try:
            return list(
                self._ensure_collection()
                .find(query, {"_id": 0})
                .sort("biz_date", ASCENDING)
                .limit(limit)
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
