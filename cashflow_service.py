#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
收支明细的取数编排层。

一次查询分三步：
  1. 按模板 + 发起时间窗口，用「获取审批实例ID列表」拿到**已完成**的实例 ID
     （statuses=["COMPLETED"]，把审批中的、被撤销的直接过滤在源头）
  2. 对本地缓存里没有（或超过 cache_ttl_days）的实例，才去拉「实例详情」
  3. 从缓存里按业务日期区间取记录，做按月汇总

缓存放 Mongo（ApprovalStore）；Mongo 不可用时退化为"只查不缓存"，
本次结果照常返回，只是在回复里注明。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from api import DingTalkApiClient, DingTalkApiError
from cashflow_report import aggregate, extract_instance

# 实例列表接口限制：startTime 距今最多 365 天（普通版）
MAX_LOOKBACK_DAYS = 365

# 一次查询最多补多少个发起人姓名（通讯录接口，避免人多了拖慢）
MAX_NAME_LOOKUPS = 50


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
        budget = int(self.cfg.max_fetch_per_query)

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
                record = self._fetch_one(template, instance_id)
                if record is None:
                    continue
                fetched_count += 1
                fetched_records.append(record)

        records = self._load_records(
            [t.kind for t in self.cfg.templates], start_text, end_text,
            fallback=fetched_records,
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

        # 缓存命中情况要能从日志里回溯（回复里的 cache_note 去掉 markdown 标记）
        self.logger.info(
            "收支查询完成 | 区间=%s ~ %s | 记录=%d 条 | %s",
            start_text, end_text, len(records),
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

    def _fetch_one(self, template, instance_id: str) -> Optional[dict]:
        try:
            instance = self.api.get_process_instance(instance_id)
        except DingTalkApiError as e:
            self.logger.error("拉取实例详情失败 | %s | %s", instance_id, e)
            return None

        detail = instance.get("result") or {}
        record = extract_instance(template, detail, instance_id, self.tz)
        if self.store is not None:
            try:
                self.store.upsert(record)
            except Exception as e:
                self.logger.warning("写入审批实例缓存失败 | %s | %s", instance_id, e)
        return record

    def _load_records(self, kinds: Sequence[str], start_text: str,
                      end_text: str, fallback: Sequence[dict]) -> List[dict]:
        """优先从缓存读；缓存不可用时用本次拉到的记录兜底。"""
        if self.store is not None:
            try:
                return self.store.query(kinds, start_text, end_text)
            except Exception as e:
                self.logger.warning("读取审批实例缓存失败，改用本次结果: %s", e)

        return [
            record for record in fallback
            if start_text <= (record.get("biz_date") or "") <= end_text
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
