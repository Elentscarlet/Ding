#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
收支明细的取数编排层。

一次查询分三步：
  1. **实例 ID 列表永远以接口为准**：「获取审批实例ID列表」按模板 + 发起时间窗口
     拿 statuses=["COMPLETED"] 的 ID。不能拿缓存里的 ID 反推——
     实例可能已经被删除，缓存里的 ID 不可靠，删掉的单子必须从结果里消失。
  2. 缓存只用来**替代详情调用**：对这批接口返回的 ID，缓存里有的（且没超过
     cache_ttl_days）就不再调「实例详情」，只补缺失的/过期的。
  3. 汇总时按「接口返回的 ID 集合」从缓存里取记录，再按业务日期过滤；
     详情接口若报"实例不存在"（已被删除），把它从缓存里删掉并排除出结果。

缓存放 Mongo（ApprovalStore）；Mongo 不可用时退化为"只查不缓存"，
本次结果照常返回，只是在回复里注明。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple

from api import DingTalkApiClient, DingTalkApiError
from cashflow_report import aggregate, extract_instance

# 实例列表接口限制：startTime 距今最多 365 天（普通版）
MAX_LOOKBACK_DAYS = 365

# 一次查询最多补多少个发起人姓名（通讯录接口，避免人多了拖慢）
MAX_NAME_LOOKUPS = 50

# 详情接口报这些错误，说明实例已经不存在（被删了），缓存行要清掉
_MISSING_MARKERS = (
    "processinstanceiderror",
    "instancenotexist",
    "instance not exist",
    "invalidprocessinstanceid",
    "不存在",
)


def is_missing_instance_error(error: Exception) -> bool:
    """判断详情接口的报错是否是「实例已不存在」。"""
    code = str(getattr(error, "code", "") or "").lower()
    message = str(error).lower()
    return any(marker in code or marker in message for marker in _MISSING_MARKERS)


class CashflowService:
    def __init__(self, cfg, api: DingTalkApiClient, store, user_store,
                 tz: timezone, logger: logging.Logger):
        """
        :param cfg: AppConfig.cashflow
        :param store: ApprovalStore（可为 None，此时不缓存）
        :param user_store: UserStore（可为 None，仅用于姓名缓存）
        """
        self.cfg = cfg
        self.api = api
        self.store = store
        self.user_store = user_store
        self.tz = tz
        self.logger = logger

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def collect(self, start_date: str, end_date: str) -> dict:
        """取 [start_date, end_date] 区间内的已审批完成收支记录并汇总。

        :param start_date: "YYYY-MM-DD"
        :param end_date: "YYYY-MM-DD"
        """
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=self.tz)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=self.tz
        )

        now = datetime.now(self.tz)
        earliest = now - timedelta(days=MAX_LOOKBACK_DAYS)
        notes: List[str] = []
        if start_dt < earliest:
            start_dt = earliest
            notes.append(f"接口最多回看 {MAX_LOOKBACK_DAYS} 天，已自动收窄起始日期")
        if end_dt > now:
            end_dt = now
        if start_dt > end_dt:
            raise ValueError("起始日期晚于结束日期")

        start_text = start_dt.strftime("%Y-%m-%d")
        end_text = end_dt.strftime("%Y-%m-%d")
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        fetched_records: List[dict] = []
        fetched_count = 0
        reused_count = 0
        errors: List[str] = []
        listed_count = 0
        budget = int(self.cfg.max_fetch_per_query)

        # instance_id -> kind：只装**接口这次返回的 ID**
        # 缓存里的孤儿记录（实例已被删除）不参与汇总
        current_ids: Dict[str, str] = {}
        missing_ids: Set[str] = set()

        for template in self.cfg.templates:
            try:
                ids = self.api.list_process_instance_ids(
                    template.process_code, start_ms, end_ms,
                    statuses=["COMPLETED"],
                )
            except DingTalkApiError as e:
                self.logger.error("获取 %s 实例ID失败: %s", template.label, e, exc_info=True)
                errors.append(f"{template.label}模板拉取失败：{e}")
                continue

            listed_count += len(ids)
            for instance_id in ids:
                current_ids[instance_id] = template.kind

            self.logger.info(
                "收支：%s 模板共 %d 个已完成实例 | %s ~ %s",
                template.label, len(ids), start_text, end_text,
            )

            cached = self._get_cached(ids)
            stale_before = datetime.now(timezone.utc) - timedelta(
                days=self.cfg.cache_ttl_days
            )
            for instance_id in ids:
                doc = cached.get(instance_id)
                if doc is not None and self._is_fresh(doc, stale_before):
                    reused_count += 1
                    continue
                if budget <= 0:
                    continue
                budget -= 1
                record, missing = self._fetch_one(template, instance_id)
                if missing:
                    missing_ids.add(instance_id)
                    continue
                if record is None:
                    # 临时失败：保留缓存里的旧详情兜底，不把单子丢掉
                    continue
                fetched_count += 1
                fetched_records.append(record)

        for instance_id in missing_ids:
            current_ids.pop(instance_id, None)

        records = self._load_records(
            current_ids.keys(), start_text, end_text, fallback=fetched_records,
        )
        self._resolve_names(records)

        summary = aggregate(records)
        cache_note = ""
        if self.store is None:
            cache_note = "⚠ 未启用本地缓存，本次结果不落库"
        else:
            cache_note = (f"**缓存**：新增 {fetched_count} 条，复用 {reused_count} 条")
            if budget <= 0:
                cache_note += f"（本次拉取已达上限 {self.cfg.max_fetch_per_query} 条，剩余下次补齐）"
        if missing_ids:
            cache_note += f"（{len(missing_ids)} 个实例已不存在，已剔除）"

        # 缓存命中情况要能从日志里回溯（回复里的 cache_note 去掉 markdown 标记）
        self.logger.info(
            "收支查询完成 | 区间=%s ~ %s | 接口实例ID=%d 个 | 记录=%d 条 | %s",
            start_text, end_text, len(current_ids), len(records),
            cache_note.replace("**", ""),
        )
        for note in notes:
            self.logger.info("收支查询提示 | %s", note)

        return {
            "range_text": f"{start_text} ~ {end_text}",
            "start": start_text,
            "end": end_text,
            "records": records,
            "summary": summary,
            "errors": errors,
            "notes": notes,
            "cache_note": cache_note,
            "fetched": fetched_count,
            "reused": reused_count,
            "money_unit": self.cfg.money_unit,
        }

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------
    def _get_cached(self, instance_ids: Sequence[str]) -> Dict[str, dict]:
        if self.store is None or not instance_ids:
            return {}
        try:
            return self.store.get_cached(instance_ids)
        except Exception as e:
            self.logger.warning("读取审批实例缓存失败，本次全量拉取: %s", e)
            return {}

    @staticmethod
    def _is_fresh(doc: dict, stale_before: datetime) -> bool:
        fetched_at = doc.get("fetched_at")
        if not isinstance(fetched_at, datetime):
            return False
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return fetched_at >= stale_before

    def _fetch_one(self, template, instance_id: str) -> Tuple[Optional[dict], bool]:
        """拉一单详情。

        :return: (记录, 实例是否已不存在)；实例被删时返回 (None, True)
        """
        try:
            instance = self.api.get_process_instance(instance_id)
        except DingTalkApiError as e:
            if is_missing_instance_error(e):
                self.logger.warning(
                    "实例已不存在（可能被删除），从缓存移除 | %s | %s", instance_id, e
                )
                self._drop_cached(instance_id)
                return None, True
            self.logger.error("拉取实例详情失败 | %s | %s", instance_id, e)
            return None, False

        detail = instance.get("result") or {}
        record = extract_instance(template, detail, instance_id, self.tz)
        if self.store is not None:
            try:
                self.store.upsert(record)
            except Exception as e:
                self.logger.warning("写入审批实例缓存失败 | %s | %s", instance_id, e)
        return record, False

    def _drop_cached(self, instance_id: str) -> None:
        if self.store is None:
            return
        try:
            self.store.delete(instance_id)
        except Exception as e:
            self.logger.warning("删除审批实例缓存失败 | %s | %s", instance_id, e)

    def _load_records(self, instance_ids: Sequence[str], start_text: str,
                      end_text: str, fallback: Sequence[dict]) -> List[dict]:
        """按「接口这次返回的 ID」取缓存记录，再按业务日期过滤。

        不能按日期范围去缓存里"捞"记录 —— 那样会把已删除的实例重新捞回来。
        缓存只负责替代详情调用。
        """
        ids = [i for i in instance_ids if i]
        if self.store is not None:
            try:
                docs = self.store.get_by_ids(ids)
            except Exception as e:
                self.logger.warning("读取审批实例缓存失败，改用本次结果: %s", e)
                docs = None
            if docs is not None:
                return [
                    doc for doc in docs
                    if start_text <= (doc.get("biz_date") or "") <= end_text
                ]

        id_set = set(ids)
        return [
            record for record in fallback
            if record.get("instance_id") in id_set
            and start_text <= (record.get("biz_date") or "") <= end_text
        ]

    # ------------------------------------------------------------------
    # 姓名
    # ------------------------------------------------------------------
    def _resolve_names(self, records: Sequence[dict]) -> None:
        user_ids = {r.get("originator_user_id") for r in records
                    if r.get("originator_user_id")}
        if not user_ids:
            return

        mapping: Dict[str, str] = {}
        if self.user_store is not None:
            try:
                mapping = self.user_store.name_map(list(user_ids)) or {}
            except Exception as e:
                self.logger.warning("读取员工姓名缓存失败: %s", e)

        missing = [uid for uid in user_ids if not mapping.get(uid)][:MAX_NAME_LOOKUPS]
        for user_id in missing:
            try:
                name = self.api.get_user_name(user_id)
            except DingTalkApiError as e:
                self.logger.debug("获取用户姓名失败 | %s | %s", user_id, e)
                continue
            if not name:
                continue
            mapping[user_id] = name
            if self.user_store is not None:
                try:
                    self.user_store.upsert_users([{"user_id": user_id, "name": name}])
                except Exception as e:
                    self.logger.debug("写入员工姓名缓存失败 | %s | %s", user_id, e)

        for record in records:
            record["originator_name"] = mapping.get(
                record.get("originator_user_id") or "", ""
            )
