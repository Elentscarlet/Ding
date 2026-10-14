#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MongoDB 存储层。

  - CheckinStore：外勤打卡记录，按 record_id 唯一索引去重落库
  - UserStore：员工姓名缓存（userid -> 姓名），供页面展示"到岗人"

设计要点：
  - MongoDB 不可用时不影响主进程：连接失败只告警（带降噪），由调用方下个周期重试。
  - 懒连接：MongoDB 没起也不会拖慢启动。
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from pymongo import ASCENDING, DESCENDING, MongoClient, UpdateOne
from pymongo.errors import DuplicateKeyError, PyMongoError

# 连接不可用时的告警降噪：第 1 次必报，之后每 N 次报一次
_WARN_EVERY = 10

# 明细查询一次最多返回的条数，避免页面/导出把内存打爆
DEFAULT_QUERY_LIMIT = 20000


class _MongoBase:
    """连接、索引、告警的公共逻辑。"""

    # 子类覆盖：((keys, options), ...)
    index_specs: tuple = ()

    def __init__(self, cfg, logger: logging.Logger, collection_name: str):
        self.cfg = cfg
        self.logger = logger
        self.collection_name = collection_name
        self._client: Optional[MongoClient] = None
        self._collection = None
        self._index_ready = False
        self._failures = 0

    def _ensure_client(self) -> MongoClient:
        if self._client is None:
            timeout = self.cfg.server_selection_timeout_ms
            self._client = MongoClient(
                self.cfg.uri,
                serverSelectionTimeoutMS=timeout,
                connectTimeoutMS=timeout,
                socketTimeoutMS=max(timeout, 5000),
                appname="ding-checkin",
            )
        return self._client

    def _ensure_collection(self):
        if self._collection is None:
            self._collection = self._ensure_client()[self.cfg.database][self.collection_name]
        if not self._index_ready:
            for keys, options in self.index_specs:
                self._collection.create_index(keys, **options)
            self._index_ready = True
        return self._collection

    def ping(self) -> bool:
        """探活，用于每个轮询周期只告警一次。"""
        try:
            self._ensure_client().admin.command("ping")
            self._failures = 0
            return True
        except PyMongoError as e:
            self._warn_unavailable(e)
            return False

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            self._collection = None
            self._index_ready = False

    def _warn_unavailable(self, error):
        self._failures += 1
        if self._failures == 1 or self._failures % _WARN_EVERY == 0:
            self.logger.warning(
                "MongoDB 不可用（累计第 %d 次，%s/%s）：%s",
                self._failures, self.cfg.database, self.collection_name, error,
            )


class CheckinStore(_MongoBase):
    """外勤打卡记录。"""

    index_specs = (
        ([("record_id", ASCENDING)], {"unique": True, "name": "uniq_record_id"}),
        ([("user_id", ASCENDING), ("user_check_time", DESCENDING)],
         {"name": "user_check_time"}),
        ([("user_check_time", ASCENDING)], {"name": "check_time"}),
        ([("matched_place", ASCENDING), ("user_check_time", ASCENDING)],
         {"name": "place_time"}),
    )

    def __init__(self, cfg, logger: logging.Logger):
        super().__init__(cfg, logger, cfg.collection)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def insert_if_absent(self, doc: dict) -> bool:
        """按 record_id 去重写入。

        :return: True = 本次新记录；False = 之前已经记录过。
        """
        record_id = doc.get("record_id")
        if not record_id:
            raise ValueError("打卡记录缺少 record_id，无法去重")

        try:
            result = self._ensure_collection().update_one(
                {"record_id": record_id}, {"$setOnInsert": doc}, upsert=True
            )
            self._failures = 0
            return result.upserted_id is not None
        except DuplicateKeyError:
            # 唯一索引兜底：并发插入同一条打卡时视为已存在
            return False
        except PyMongoError as e:
            self._warn_unavailable(e)
            raise

    # ------------------------------------------------------------------
    # 查询（供页面与导出使用）
    # ------------------------------------------------------------------
    def find_records(self, start_utc: datetime, end_utc: datetime,
                     place: Optional[str] = None,
                     limit: int = DEFAULT_QUERY_LIMIT) -> List[dict]:
        """按打卡时间范围（UTC）查询记录，可按地点过滤。不含 raw 原始报文。"""
        query = {"user_check_time": {"$gte": start_utc, "$lte": end_utc}}
        if place:
            query["matched_place"] = place
        try:
            cursor = (
                self._ensure_collection()
                .find(query, {"raw": 0})
                .sort("user_check_time", ASCENDING)
                .limit(limit)
            )
            return list(cursor)
        except PyMongoError as e:
            self._warn_unavailable(e)
            raise

    def distinct_places(self) -> List[str]:
        """已被记录过的地点名列表，用于页面下拉。"""
        try:
            return sorted(p for p in self._ensure_collection().distinct("matched_place") if p)
        except PyMongoError as e:
            self._warn_unavailable(e)
            return []

    def count(self) -> int:
        """当前集合文档数，连接不可用时返回 -1。"""
        try:
            return self._ensure_collection().count_documents({})
        except PyMongoError as e:
            self._warn_unavailable(e)
            return -1


class UserStore(_MongoBase):
    """员工姓名缓存，供查询结果展示"到岗人"。"""

    index_specs = (
        ([("user_id", ASCENDING)], {"unique": True, "name": "uniq_user_id"}),
    )

    def __init__(self, cfg, logger: logging.Logger):
        super().__init__(cfg, logger, getattr(cfg, "users_collection", "users"))

    def upsert_users(self, users: List[dict]) -> int:
        """批量写入/更新 userid -> 姓名。users 为 [{"user_id":..., "name":...}]。"""
        now = datetime.now(timezone.utc)
        ops = [
            UpdateOne(
                {"user_id": item["user_id"]},
                {"$set": {"name": item.get("name") or "", "updated_at": now}},
                upsert=True,
            )
            for item in users
            if item.get("user_id")
        ]
        if not ops:
            return 0
        try:
            result = self._ensure_collection().bulk_write(ops, ordered=False)
            self._failures = 0
            return (result.upserted_count or 0) + (result.modified_count or 0)
        except PyMongoError as e:
            self._warn_unavailable(e)
            raise

    def name_map(self, user_ids: Optional[List[str]] = None) -> dict:
        """返回 {user_id: 姓名}；user_ids 为空则返回全部。"""
        query = {}
        if user_ids:
            query = {"user_id": {"$in": list(user_ids)}}
        try:
            return {
                doc["user_id"]: (doc.get("name") or "")
                for doc in self._ensure_collection().find(
                    query, {"user_id": 1, "name": 1}
                )
            }
        except PyMongoError as e:
            self._warn_unavailable(e)
            raise
