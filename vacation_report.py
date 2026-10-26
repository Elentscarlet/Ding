#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
奖励假期余额一览表：聚合、聊天展示、Excel 导出。

口径（与业务确认过，和钉钉「员工假期余额」页面一致）：

  初始 = 区间内 leaveRecordType=modify_quota 且 calType=insert 的额度
         （管理员导入 / 初始化余额）
  增加 = 区间内 leaveRecordType=modify_quota 且 calType ∈ {add, update} 的额度
         （OA 审批通过自动发放的奖励假日）
  请假 = 区间内 leaveRecordType=leave 且 leaveStatus=success 的消耗
         已通过但被撤销（leaveStatus=revoke）的会回补
  余额 = 钉钉「查询假期余额」接口的 当前总额 - 已用

  对账：初始 + 增加 - 请假 应等于 余额；不等时在「校验」列标出差额，
        避免静默算错（例如初始化记录落在统计区间之外）。

所有金额内部按**小时**存，展示时按 hours_per_day 折算成天。
"""

import io
import unicodedata
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

BALANCE_HEADERS = ["姓名", "初始", "增加", "请假", "余额"]

# 变更记录归类
KIND_INIT = "init"
KIND_GRANT = "grant"
KIND_GRANT_NEG = "grant_neg"
KIND_LEAVE = "leave"
KIND_LEAVE_NEG = "leave_neg"

KIND_LABELS = {
    KIND_INIT: "初始化",
    KIND_GRANT: "发放",
    KIND_GRANT_NEG: "发放扣减",
    KIND_LEAVE: "请假",
    KIND_LEAVE_NEG: "撤销回补",
}

DETAIL_HEADERS = ["记录时间", "姓名", "userId", "类型",
                  "数量(小时)", "数量(天)", "原因", "状态"]

# 对账容差（小时）：两个接口的取整口径偶有 1 分钱的差异，0.01 小时以上才算不平
DIFF_TOLERANCE_HOURS = 0.01


# ======================================================================
# 记录归类与单位换算
# ======================================================================
def classify_record(record: dict) -> Optional[str]:
    """把一条变更记录归到 初始化 / 发放 / 请假 三类之一；无关记录返回 None。

    字段语义见钉钉文档：
      calType       insert=新纪录 add=新增 update=更新 delete=删除 None=请假消耗
      leaveRecordType leave=请假 update/modify_quota=额度变更
      leaveStatus   init=申请中 success=已通过 refuse=被拒 abort=撤销申请
                    revoke=已通过但撤销
    """
    record_type = (record.get("leaveRecordType") or "").strip()
    cal_type = (record.get("calType") or "").strip()
    status = (record.get("leaveStatus") or "").strip()

    # calType 为空 / 缺失 = 请假消耗（这才是最可靠的判据，优先于 leaveRecordType）
    if record_type == "leave" or (record_type != "modify_quota" and not cal_type):
        if status == "success":
            return KIND_LEAVE
        if status == "revoke":
            return KIND_LEAVE_NEG
        return None  # 申请中 / 被拒 / 用户自己撤销申请，都不影响余额

    if record_type in ("modify_quota", "update"):
        if cal_type == "insert":
            return KIND_INIT
        if cal_type in ("add", "update"):
            return KIND_GRANT
        if cal_type == "delete":
            return KIND_GRANT_NEG

    return None


def record_hours(record: dict, hours_per_day: float) -> float:
    """一条记录对应的小时数（接口按百分之一天/小时折算，返回绝对值）。"""
    per_hour = record.get("recordNumPerHour")
    if per_hour not in (None, ""):
        try:
            return float(per_hour) / 100.0
        except (TypeError, ValueError):
            return 0.0
    per_day = record.get("recordNumPerDay")
    if per_day not in (None, ""):
        try:
            return float(per_day) / 100.0 * hours_per_day
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def quota_hours(quota: dict, hours_per_day: float):
    """额度记录 -> (总额小时, 已用小时)。"""
    total = quota.get("quota_num_per_hour")
    used = quota.get("used_num_per_hour")
    if total not in (None, ""):
        return float(total) / 100.0, float(used or 0) / 100.0

    total_day = quota.get("quota_num_per_day")
    used_day = quota.get("used_num_per_day")
    return (float(total_day or 0) / 100.0 * hours_per_day,
            float(used_day or 0) / 100.0 * hours_per_day)


def pick_quota(quotas: Sequence[dict], year: int) -> Optional[dict]:
    """在多个额度周期里挑一个：优先当年，其次有效期最晚的。"""
    if not quotas:
        return None
    for quota in quotas:
        if str(quota.get("quota_cycle") or "") == str(year):
            return quota

    def end_time(item: dict) -> float:
        try:
            return float(item.get("end_time") or 0)
        except (TypeError, ValueError):
            return 0.0

    return max(quotas, key=end_time)


def to_local(moment_ms, tz: timezone) -> Optional[datetime]:
    """毫秒时间戳 -> 本地时间。"""
    if moment_ms in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(moment_ms) / 1000, tz)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def balance_range(tz: timezone, all_history: bool = False,
                  now: Optional[datetime] = None):
    """余额表的统计区间：默认今年 1 月 1 日至今；all_history=True 表示不限。

    :return: (start, end, year)；start/end 为 None 表示不限
    """
    now = now or datetime.now(tz)
    if all_history:
        return None, None, now.year
    start = datetime(now.year, 1, 1, tzinfo=tz)
    return start, now, now.year


# ======================================================================
# 聚合
# ======================================================================
def build_balance(users: List[dict], quotas: dict, records: List[dict],
                  tz: timezone, start: Optional[datetime],
                  end: Optional[datetime], hours_per_day: float,
                  year: int) -> dict:
    """把余额 + 变更记录聚合成一览表。

    :param users: [{"user_id","name"}] 统计范围内的人
    :param quotas: {user_id: [额度记录, ...]}（list_vacation_quotas 的返回）
    :param records: 变更记录列表（list_vacation_records 的返回）
    :param start/end: 统计区间；传 None 表示不限（全历史）
    """
    index = {u.get("user_id"): u for u in users if u.get("user_id")}
    buckets: dict = {}
    details: List[dict] = []
    seen_records = set()
    skipped = 0

    start_ms = int(start.timestamp() * 1000) if start else None
    end_ms = int(end.timestamp() * 1000) if end else None

    for record in records:
        record_id = record.get("recordId") or record.get("record_id")
        if record_id and record_id in seen_records:
            continue  # 分页边界可能重复返回同一条
        if record_id:
            seen_records.add(record_id)

        user_id = record.get("userId") or record.get("userid")
        kind = classify_record(record)
        if not user_id or kind is None:
            skipped += 1
            continue

        # 区间过滤按记录创建时间；没有创建时间的记录不丢，计入明细并单独提示
        created = record.get("gmtCreate")
        if created not in (None, "") and (start_ms is not None or end_ms is not None):
            try:
                created_ms = int(created)
            except (TypeError, ValueError):
                created_ms = None
            if created_ms is not None:
                if start_ms is not None and created_ms < start_ms:
                    continue
                if end_ms is not None and created_ms > end_ms:
                    continue

        hours = record_hours(record, hours_per_day)
        bucket = buckets.setdefault(user_id, {"init": 0.0, "grant": 0.0, "leave": 0.0})
        if kind == KIND_INIT:
            bucket["init"] += hours
        elif kind == KIND_GRANT:
            bucket["grant"] += hours
        elif kind == KIND_GRANT_NEG:
            bucket["grant"] -= hours
        elif kind == KIND_LEAVE:
            bucket["leave"] += hours
        elif kind == KIND_LEAVE_NEG:
            bucket["leave"] -= hours

        created_at = to_local(created, tz)
        details.append({
            "time": created_at.strftime("%Y-%m-%d %H:%M:%S") if created_at else "",
            "sort_key": created_at or datetime.min.replace(tzinfo=timezone.utc),
            "name": (index.get(user_id) or {}).get("name") or user_id,
            "user_id": user_id,
            "kind": KIND_LABELS.get(kind, kind),
            "hours": hours,
            "days": hours / hours_per_day if hours_per_day else 0.0,
            "reason": record.get("leaveReason") or "",
            "status": record.get("leaveStatus") or "",
        })

    rows = []
    for user_id, user in index.items():
        bucket = buckets.get(user_id, {"init": 0.0, "grant": 0.0, "leave": 0.0})
        quota = pick_quota(quotas.get(user_id) or [], year)
        if quota is not None:
            total_hours, used_hours = quota_hours(quota, hours_per_day)
            balance_hours = total_hours - used_hours
        else:
            balance_hours = bucket["init"] + bucket["grant"] - bucket["leave"]

        expected = bucket["init"] + bucket["grant"] - bucket["leave"]
        diff = balance_hours - expected

        if (not any(abs(v) > 0 for v in bucket.values())
                and abs(balance_hours) <= 0):
            continue  # 这个人在奖励假期上完全没有数据，不占表格

        rows.append({
            "user_id": user_id,
            "name": user.get("name") or user_id,
            "init_hours": bucket["init"],
            "grant_hours": bucket["grant"],
            "leave_hours": bucket["leave"],
            "balance_hours": balance_hours,
            "expected_hours": expected,
            "diff_hours": diff,
            "has_quota": quota is not None,
        })

    rows.sort(key=lambda row: (-row["grant_hours"], -row["balance_hours"], row["name"]))
    details.sort(key=lambda item: item["sort_key"], reverse=True)

    totals = {
        "init_hours": sum(r["init_hours"] for r in rows),
        "grant_hours": sum(r["grant_hours"] for r in rows),
        "leave_hours": sum(r["leave_hours"] for r in rows),
        "balance_hours": sum(r["balance_hours"] for r in rows),
    }

    return {
        "scanned": len(index),
        "rows": rows,
        "totals": totals,
        "details": details,
        "skipped": skipped,
        "mismatched": [r for r in rows if abs(r["diff_hours"]) > DIFF_TOLERANCE_HOURS],
        "hours_per_day": hours_per_day,
        "start": start,
        "end": end,
    }


# ======================================================================
# 展示
# ======================================================================
def fmt_days(hours: float, hours_per_day: float) -> str:
    """小时 -> 天的展示文本（最多两位小数，去掉多余的 0）。"""
    days = (hours / hours_per_day) if hours_per_day else 0.0
    text = f"{round(days, 2):.2f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _display_width(text: str) -> int:
    """东亚全角字符按 2 列宽计算，用于等宽对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def format_balance_markdown(result: dict, max_rows: int = 30) -> str:
    """渲染成机器人回复：标题 + 说明 + 等宽表格 + 合计 + 对账提示。"""
    hours_per_day = result["hours_per_day"]
    start, end = result["start"], result["end"]
    range_text = (
        f"{start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}"
        if start and end else "全部历史"
    )

    header = [
        "### 奖励假期余额一览",
        f"**统计区间**：{range_text}　**单位**：天（1 天 = {_num(hours_per_day)} 小时）",
        f"**范围**：扫描 {result['scanned']} 人，其中 {len(result['rows'])} 人有奖励假期数据",
    ]

    rows = result["rows"]
    if not rows:
        return "  \n".join(header + ["", "该范围内没有任何奖励假期数据。"])

    totals = result["totals"]
    body = [BALANCE_HEADERS]
    for row in rows[:max_rows]:
        body.append([
            row["name"],
            fmt_days(row["init_hours"], hours_per_day),
            fmt_days(row["grant_hours"], hours_per_day),
            fmt_days(row["leave_hours"], hours_per_day),
            fmt_days(row["balance_hours"], hours_per_day),
        ])
    body.append([
        "合计",
        fmt_days(totals["init_hours"], hours_per_day),
        fmt_days(totals["grant_hours"], hours_per_day),
        fmt_days(totals["leave_hours"], hours_per_day),
        fmt_days(totals["balance_hours"], hours_per_day),
    ])

    widths = [
        max(_display_width(line[i]) for line in body) for i in range(len(BALANCE_HEADERS))
    ]
    # 数字列右对齐，姓名列左对齐
    lines = []
    for index, line in enumerate(body):
        cells = []
        for col, cell in enumerate(line):
            cells.append(_pad(cell, widths[col]) if col == 0
                         else " " * max(0, widths[col] - _display_width(cell)) + cell)
        lines.append("  ".join(cells).rstrip())
        if index == 0:
            lines.append("-" * (_display_width(lines[-1])))

    blocks = header + ["", "```", "\n".join(lines), "```"]

    if len(rows) > max_rows:
        blocks.append(f"…… 另有 {len(rows) - max_rows} 人未显示，"
                      f"请用「导出余额」获取完整 Excel。")

    mismatched = result["mismatched"]
    if mismatched:
        names = "、".join(r["name"] for r in mismatched[:5])
        more = f" 等 {len(mismatched)} 人" if len(mismatched) > 5 else ""
        blocks.append(
            f"⚠ 对账不平：{names}{more} 的「余额」≠「初始+增加-请假」，"
            f"通常是初始化记录落在统计区间之外，详情见导出的 Excel。"
        )

    return "  \n".join(blocks)


def _num(value: float) -> str:
    return f"{value:g}"


# ======================================================================
# Excel
# ======================================================================
def build_balance_workbook(result: dict, meta: dict) -> bytes:
    """生成 xlsx（汇总 + 变更明细 两个 sheet），返回字节流。"""
    hours_per_day = result["hours_per_day"]
    workbook = Workbook()

    header_font = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="DDEBF7")

    def write_sheet(sheet, headers, rows, widths):
        sheet.append(headers)
        for cell in sheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for row in rows:
            sheet.append(row)
        sheet.freeze_panes = "A2"
        if rows:
            sheet.auto_filter.ref = sheet.dimensions
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width

    summary_rows = []
    for index, row in enumerate(result["rows"], start=1):
        diff = row["diff_hours"]
        summary_rows.append([
            index,
            row["name"],
            row["user_id"],
            round(row["init_hours"] / hours_per_day, 2),
            round(row["grant_hours"] / hours_per_day, 2),
            round(row["leave_hours"] / hours_per_day, 2),
            round(row["balance_hours"] / hours_per_day, 2),
            round(row["init_hours"], 2),
            round(row["grant_hours"], 2),
            round(row["leave_hours"], 2),
            round(row["balance_hours"], 2),
            "" if abs(diff) <= DIFF_TOLERANCE_HOURS
            else f"差 {diff / hours_per_day:+.2f} 天",
        ])

    sheet = workbook.active
    sheet.title = "汇总"
    write_sheet(
        sheet,
        ["序号", "姓名", "userId",
         "初始(天)", "增加(天)", "请假(天)", "余额(天)",
         "初始(小时)", "增加(小时)", "请假(小时)", "余额(小时)", "校验"],
        summary_rows,
        [6, 12, 26, 10, 10, 10, 10, 11, 11, 11, 11, 16],
    )

    totals = result["totals"]
    sheet.append([])
    sheet.append(["合计", "", "", 
                  round(totals["init_hours"] / hours_per_day, 2),
                  round(totals["grant_hours"] / hours_per_day, 2),
                  round(totals["leave_hours"] / hours_per_day, 2),
                  round(totals["balance_hours"] / hours_per_day, 2),
                  round(totals["init_hours"], 2),
                  round(totals["grant_hours"], 2),
                  round(totals["leave_hours"], 2),
                  round(totals["balance_hours"], 2), ""])
    sheet.append([])
    sheet.append(["统计区间", str(meta.get("range_text") or ""),
                  "单位", f"天（1 天 = {_num(hours_per_day)} 小时）"])
    sheet.append(["扫描人数", result["scanned"],
                  "有数据人数", len(result["rows"]),
                  "导出时间", str(meta.get("exported_at") or "")])
    sheet.append(["说明", "初始=区间内初始化记录；增加=发放记录；"
                         "请假=区间内已通过且未撤销的请假；余额=钉钉当前余额"])

    detail_sheet = workbook.create_sheet("变更明细")
    write_sheet(
        detail_sheet, DETAIL_HEADERS,
        [[item["time"], item["name"], item["user_id"], item["kind"],
          round(item["hours"], 2), round(item["days"], 2),
          item["reason"], item["status"]] for item in result["details"]],
        [20, 12, 26, 10, 12, 10, 32, 10],
    )

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
