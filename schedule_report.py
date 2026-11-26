#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
排班表导出：拉取钉钉排班 -> 明细 + 排班矩阵 -> xlsx。

数据来源：/topapi/attendance/listschedule（「查询企业考勤排班详情」）
  - 按天查询，返回全公司当天的排班；可分页，size 上限 200
  - 每条记录：userid / check_type(OnDuty|OffDuty) / plan_check_time /
              changed_check_time / class_id / group_id / plan_id
  - 已知限制：固定班制只能查到未来 15 天；企业总人数需 10000 以下

表结构：
  明细 —— 一行一条排班记录（日期/员工/班次/上下班/计划时间/调整后时间…）
  矩阵 —— 行=员工，列=日期，格子=该日「上班~下班」时间（取计划打卡时间）

与入口无关：机器人指令和命令行都复用这里的纯函数。
"""

import io
import re
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from checkin_report import resolve_range

DEFAULT_DAYS = 7
DEFAULT_MAX_DAYS = 31

DETAIL_HEADERS = ["日期", "员工userId", "姓名", "打卡类型",
                  "计划打卡时间", "调整后打卡时间", "班次ID", "考勤组ID", "排班ID"]

CHECK_TYPE_TEXT = {"onduty": "上班", "offduty": "下班"}


# ======================================================================
# 日期区间
# ======================================================================
def resolve_date_range(from_text: Optional[str], to_text: Optional[str],
                       tz: timezone, default_days: int = DEFAULT_DAYS,
                       max_days: int = DEFAULT_MAX_DAYS
                       ) -> Tuple[date, date, List[str], Optional[str]]:
    """解析起止日期，返回 (起始日期, 结束日期, 日期字符串列表, 提示)。

    天数超过 max_days 时**收窄**并给出提示（接口是按天查，天数越多调用越多）。
    """
    from_date, to_date, _, _ = resolve_range(from_text, to_text, tz, default_days)

    note = None
    span = (to_date - from_date).days + 1
    if max_days and span > max_days:
        to_date = from_date + timedelta(days=max_days - 1)
        span = max_days
        note = f"日期范围超过上限 {max_days} 天，已收窄到 {from_date} ~ {to_date}"

    days = [(from_date + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span)]
    return from_date, to_date, days, note


# ======================================================================
# 拉取
# ======================================================================
def fetch_schedules(api, days: Sequence[str], logger=None) -> Tuple[List[dict], List[str]]:
    """逐天拉取排班，返回 (带 date 字段的记录列表, 失败的日期列表)。

    单天失败不影响其它天，失败日期由调用方提示用户。
    """
    rows: List[dict] = []
    failed: List[str] = []

    for day in days:
        try:
            batch = api.list_schedule_all(day)
        except Exception as e:
            failed.append(day)
            if logger:
                logger.error("拉取 %s 的排班失败: %s", day, e, exc_info=True)
            continue

        for item in batch or []:
            if not isinstance(item, dict):
                continue
            record = dict(item)
            record["date"] = day
            rows.append(record)

        if logger:
            logger.info("排班 %s 取到 %d 条", day, len(batch or []))

    return rows, failed


def build_name_map(user_store, user_ids: Sequence[str], logger=None) -> Dict[str, str]:
    """从姓名缓存取 userid->姓名；缓存不可用就返回空字典（调用方退回显示 userId）。"""
    ids = [uid for uid in user_ids if uid]
    if not ids or user_store is None:
        return {}
    try:
        return user_store.name_map(ids) or {}
    except Exception as e:
        if logger:
            logger.warning("读取员工姓名缓存失败，改为显示 userId: %s", e)
        return {}


# ======================================================================
# 组装行
# ======================================================================
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def _time_part(value) -> str:
    """把 plan_check_time / changed_check_time 归一成 "HH:MM"。

    兼容 "2026-11-01 09:30:00"、"2026-11-01T09:30:00Z"、毫秒时间戳。
    归一不出合法时间就返回空串，避免把 "2020-" 这种半截值写进表里。
    """
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            return datetime.fromtimestamp(int(value) / 1000).strftime("%H:%M")
        except (ValueError, OSError, OverflowError):
            return ""

    text = str(value).strip()
    for separator in (" ", "T"):
        if separator in text:
            text = text.split(separator, 1)[1]
            break
    if text.endswith("Z"):
        text = text[:-1]

    head = text[:5]
    return head if _TIME_RE.match(head) else ""


def _check_type_text(value) -> str:
    key = str(value or "").strip().lower()
    return CHECK_TYPE_TEXT.get(key, str(value or ""))


def group_by_user_date(rows: Sequence[dict]) -> Dict[Tuple[str, str], dict]:
    """按 (userId, 日期) 归并，拆出上班/下班时间点。"""
    buckets: Dict[Tuple[str, str], dict] = {}
    for row in rows:
        user_id = str(row.get("userid") or "").strip()
        day = str(row.get("date") or "").strip()
        if not user_id or not day:
            continue
        bucket = buckets.setdefault((user_id, day), {"on": [], "off": [], "other": []})
        moment = _time_part(row.get("plan_check_time"))
        check_type = str(row.get("check_type") or "").strip().lower()
        if check_type == "onduty":
            bucket["on"].append(moment)
        elif check_type == "offduty":
            bucket["off"].append(moment)
        else:
            bucket["other"].append(moment)
    return buckets


def matrix_cell(bucket: Optional[dict]) -> str:
    """矩阵单元格：上班~下班；只有单边时单独标注。"""
    if not bucket:
        return ""
    on_times = [t for t in bucket["on"] if t]
    off_times = [t for t in bucket["off"] if t]
    if on_times and off_times:
        return f"{min(on_times)}-{max(off_times)}"
    if on_times:
        return f"上班{min(on_times)}"
    if off_times:
        return f"下班{max(off_times)}"
    other = [t for t in bucket["other"] if t]
    return other[0] if other else ""


def build_detail_rows(rows: Sequence[dict],
                      name_of: Callable[[str], str]) -> List[dict]:
    """明细行：按 日期、姓名、打卡时间 排序。"""
    prepared = []
    for row in rows:
        user_id = str(row.get("userid") or "").strip()
        day = str(row.get("date") or "").strip()
        moment = _time_part(row.get("plan_check_time"))
        prepared.append((day, name_of(user_id), moment, user_id, row))
    prepared.sort(key=lambda item: (item[0], item[1], item[2]))

    detail = []
    for day, name, moment, user_id, row in prepared:
        detail.append({
            "date": day,
            "user_id": user_id,
            "name": name,
            "check_type": _check_type_text(row.get("check_type")),
            "plan_check_time": moment,
            "changed_check_time": _time_part(row.get("changed_check_time")),
            "class_id": row.get("class_id"),
            "group_id": row.get("group_id"),
            "plan_id": row.get("plan_id"),
            "approve_id": row.get("approve_id"),
        })
    return detail


def build_matrix(rows: Sequence[dict], days: Sequence[str],
                 name_of: Callable[[str], str]) -> dict:
    """员工 × 日期 的排班矩阵。"""
    buckets = group_by_user_date(rows)

    users = sorted({str(r.get("userid") or "").strip() for r in rows if r.get("userid")},
                   key=lambda uid: (name_of(uid), uid))
    table = []
    for user_id in users:
        cells = [matrix_cell(buckets.get((user_id, day))) for day in days]
        table.append({
            "name": name_of(user_id),
            "user_id": user_id,
            "cells": cells,
            "scheduled_days": sum(1 for cell in cells if cell),
        })

    return {
        "headers": ["员工", "userId"] + [day[5:] for day in days],   # 列头用 MM-DD
        "days": list(days),
        "rows": table,
    }


# ======================================================================
# Excel
# ======================================================================
def build_schedule_workbook(detail_rows: Sequence[dict], matrix: dict,
                            meta: dict) -> bytes:
    """生成排班表 xlsx（明细 + 排班矩阵 两个 sheet），返回字节流。"""
    workbook = Workbook()

    header_font = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="DDEBF7")
    center = Alignment(horizontal="center", vertical="center")

    # ---------------- 明细 ----------------
    sheet = workbook.active
    sheet.title = "明细"
    sheet.append(DETAIL_HEADERS)
    for cell in sheet[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center

    for row in detail_rows:
        sheet.append([
            row["date"], row["user_id"], row["name"], row["check_type"],
            row["plan_check_time"], row["changed_check_time"],
            row["class_id"], row["group_id"], row["plan_id"],
        ])

    sheet.freeze_panes = "A2"
    if detail_rows:
        sheet.auto_filter.ref = sheet.dimensions
    for index, width in enumerate([12, 24, 12, 10, 14, 14, 14, 14, 16], start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width

    # 查询条件写在明细尾部，便于回溯口径
    sheet.append([])
    sheet.append(["查询条件", str(meta.get("range_text") or ""),
                  "天数", str(meta.get("day_count") or ""),
                  "导出时间", str(meta.get("exported_at") or "")])

    # ---------------- 排班矩阵 ----------------
    matrix_sheet = workbook.create_sheet("排班矩阵")
    matrix_sheet.append(matrix["headers"])
    for cell in matrix_sheet[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center

    for row in matrix["rows"]:
        matrix_sheet.append([row["name"], row["user_id"]] + row["cells"])

    # 冻结前两列 + 表头，横向滚动时员工不动
    matrix_sheet.freeze_panes = "C2"
    matrix_sheet.column_dimensions["A"].width = 14
    matrix_sheet.column_dimensions["B"].width = 24
    for index in range(3, 3 + len(matrix["days"])):
        matrix_sheet.column_dimensions[get_column_letter(index)].width = 13
    for row in matrix_sheet.iter_rows(min_row=2):
        for cell in row[2:]:
            cell.alignment = center

    if matrix["rows"]:
        matrix_sheet.append([])
        matrix_sheet.append([
            "说明",
            f"共 {len(matrix['rows'])} 人 × {len(matrix['days'])} 天；"
            "格子内容为「上班~下班」计划打卡时间，空白表示当天无排班",
        ])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ======================================================================
# 一句话汇总（机器人回复用）
# ======================================================================
def format_schedule_markdown(matrix: dict, detail_rows: Sequence[dict],
                             failed_days: Sequence[str] = (),
                             note: Optional[str] = None) -> str:
    days = matrix["days"]
    lines = [
        "### 排班表",
        f"**范围**：{days[0]} ~ {days[-1]}（{len(days)} 天）",
        f"**人员**：{len(matrix['rows'])} 人　**排班记录**：{len(detail_rows)} 条",
    ]
    if note:
        lines.append(f"**提示**：{note}")
    if failed_days:
        lines.append(f"**未取到**：{'、'.join(failed_days)}")
    if not matrix["rows"]:
        lines.append("")
        lines.append("该范围内没有排班数据。")
    return "  \n".join(lines)
