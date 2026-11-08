#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
收入支出明细：字段解析、按汇总、聊天展示、Excel 导出。

两个审批模板（支出申请 / 存入）的实例详情结构一样，只是字段名不同，
所以字段名走配置（cashflow.templates[].date_field / amount_field / reason_field），
留空则按组件类型兜底（第一个 DDDateField / NumberField / TextField）。

金额内部按 float 元存储；展示保留两位小数。
"""

import io
import unicodedata
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
)

SUMMARY_HEADERS = ["月份", "收入", "支出", "结余"]
DETAIL_HEADERS = ["日期", "类型", "金额", "事由", "发起人"]
EXCEL_DETAIL_HEADERS = ["日期", "类型", "金额", "事由", "发起人",
                        "审批单号", "标题", "状态"]


# ======================================================================
# 字段解析
# ======================================================================
def parse_amount(value) -> Optional[float]:
    """表单金额 -> float 元；解析不了返回 None。

    只认纯数值（允许千分位、货币符号、单位"元"），
    不像 parse_number 那样做前缀匹配，避免把日期串当金额。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    for ch in (",", "，", "￥", "¥", "元", " "):
        text = text.replace(ch, "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_biz_date(value, tz: timezone) -> Optional[str]:
    """表单日期 -> "YYYY-MM-DD"；解析不了返回 None。"""
    if value is None or isinstance(value, bool):
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        number = int(text)
        if number > 10 ** 11:            # 毫秒时间戳
            return datetime.fromtimestamp(number / 1000, tz).strftime("%Y-%m-%d")
        if number > 10 ** 8:             # 秒时间戳
            return datetime.fromtimestamp(number, tz).strftime("%Y-%m-%d")

    if "T" in text:                       # ISO 形式
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if moment.tzinfo is not None:
                moment = moment.astimezone(tz)
            return moment.strftime("%Y-%m-%d")
        except ValueError:
            pass

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def ms_to_date(milliseconds, tz: timezone) -> Optional[str]:
    if milliseconds in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(milliseconds) / 1000, tz).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def pick_component(components: Sequence[dict], name: str, component_type: str):
    """按字段名找组件；找不到再按组件类型兜底。"""
    comps = [c for c in (components or []) if isinstance(c, dict)]

    if name:
        for comp in comps:
            for key in ("name", "bizAlias", "id"):
                if (comp.get(key) or "").strip() == name:
                    return comp
    if component_type:
        for comp in comps:
            if (comp.get("componentType") or "").strip() == component_type:
                return comp
    return None


def extract_instance(template, detail: dict, instance_id: str,
                     tz: timezone) -> dict:
    """把一单审批实例解析成缓存记录。

    :param template: AppConfig.cashflow.templates 里的一个模板配置
    """
    components = detail.get("formComponentValues") or []

    date_comp = pick_component(components, template.date_field, "DDDateField")
    amount_comp = pick_component(components, template.amount_field, "NumberField")
    reason_comp = pick_component(components, template.reason_field, "TextField")

    biz_date = parse_biz_date(date_comp.get("value") if date_comp else None, tz)
    create_date = ms_to_date(detail.get("createTime"), tz)
    # 表单没填日期就退回发起日期，避免这笔记录在区间查询里丢失
    used_date = biz_date or create_date

    amount = parse_amount(amount_comp.get("value") if amount_comp else None)
    reason = (reason_comp.get("value") if reason_comp else "") or ""

    return {
        "instance_id": instance_id,
        "process_code": template.process_code,
        "kind": template.kind,
        "label": template.label,
        "biz_date": used_date or "",
        "biz_date_from_form": bool(biz_date),
        "create_date": create_date or "",
        "amount": amount if amount is not None else 0.0,
        "amount_missing": amount is None,
        "reason": str(reason).strip(),
        "originator_user_id": detail.get("originatorUserId") or "",
        "title": detail.get("title") or "",
        "status": (detail.get("status") or "").upper(),
        "result": (detail.get("result") or "").lower(),
        "finish_time": detail.get("finishTime"),
        "form": {
            (comp.get("name") or comp.get("id") or ""): comp.get("value")
            for comp in components if isinstance(comp, dict)
        },
        "fetched_at": datetime.now(timezone.utc),
    }


def is_approved(record: dict) -> bool:
    """只有「审批完成 + 同意」的才算进账。"""
    return record.get("status") == "COMPLETED" and record.get("result") == "agree"


# ======================================================================
# 汇总
# ======================================================================
def aggregate(records: Sequence[dict]) -> dict:
    """按月份汇总收入/支出/结余。records 需已按业务日期过滤。"""
    months: dict = {}
    for record in records:
        if not is_approved(record):
            continue
        month = (record.get("biz_date") or "")[:7] or "未知月份"
        bucket = months.setdefault(
            month, {"income": 0.0, "expense": 0.0,
                    "income_count": 0, "expense_count": 0},
        )
        amount = abs(float(record.get("amount") or 0))
        if record.get("kind") == "income":
            bucket["income"] += amount
            bucket["income_count"] += 1
        else:
            bucket["expense"] += amount
            bucket["expense_count"] += 1

    rows = []
    for month in sorted(months):
        bucket = months[month]
        rows.append({
            "month": month,
            "income": round(bucket["income"], 2),
            "expense": round(bucket["expense"], 2),
            "net": round(bucket["income"] - bucket["expense"], 2),
            "income_count": bucket["income_count"],
            "expense_count": bucket["expense_count"],
        })

    return {
        "rows": rows,
        "totals": {
            "income": round(sum(r["income"] for r in rows), 2),
            "expense": round(sum(r["expense"] for r in rows), 2),
            "net": round(sum(r["net"] for r in rows), 2),
            "income_count": sum(r["income_count"] for r in rows),
            "expense_count": sum(r["expense_count"] for r in rows),
        },
    }


# ======================================================================
# 聊天展示
# ======================================================================
def fmt_money(value: float) -> str:
    return f"{value:,.2f}"


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _render_table(headers: List[str], rows: List[List[str]]) -> str:
    """等宽表格：首列左对齐，其余右对齐。"""
    body = [headers] + rows
    widths = [max(_display_width(line[i]) for line in body)
              for i in range(len(headers))]

    lines = []
    for index, line in enumerate(body):
        cells = [
            _pad(cell, widths[col]) if col == 0
            else " " * max(0, widths[col] - _display_width(cell)) + cell
            for col, cell in enumerate(line)
        ]
        lines.append("  ".join(cells).rstrip())
        if index == 0:
            lines.append("-" * _display_width(lines[-1]))
    return "\n".join(lines)


def format_summary_markdown(result: dict, max_rows: int = 12,
                            money_unit: str = "元") -> str:
    """按月汇总的机器人回复。"""
    summary = result["summary"]
    rows = summary["rows"]
    totals = summary["totals"]

    header = [
        "### 收支明细汇总",
        f"**区间**：{result['range_text']}　**单位**：{money_unit}",
        f"**笔数**：收入 {totals['income_count']} 笔 / 支出 {totals['expense_count']} 笔"
        f"　**结余**：{fmt_money(totals['net'])} {money_unit}",
    ]

    if result.get("errors"):
        header.append("⚠ " + "；".join(result["errors"]))
    if result.get("cache_note"):
        header.append(result["cache_note"])

    if not rows:
        return "  \n".join(header + ["", "该区间内没有已审批完成的收支记录。"])

    table_rows = [
        [row["month"], fmt_money(row["income"]),
         fmt_money(row["expense"]), fmt_money(row["net"])]
        for row in rows[:max_rows]
    ]
    table_rows.append(["合计", fmt_money(totals["income"]),
                       fmt_money(totals["expense"]), fmt_money(totals["net"])])

    blocks = header + ["", "```", _render_table(SUMMARY_HEADERS, table_rows), "```"]

    if len(rows) > max_rows:
        blocks.append(f"…… 另有 {len(rows) - max_rows} 个月未显示，请用「导出收支」看全量。")
    blocks.append("完整明细：发「收支明细」，导出 Excel：发「导出收支」。")
    return "  \n".join(blocks)


def format_detail_markdown(records: Sequence[dict], max_rows: int = 30,
                           range_text: str = "", money_unit: str = "元",
                           reason_width: int = 18) -> str:
    """逐条明细的机器人回复。"""
    header = [
        "### 收支明细",
        f"**区间**：{range_text}　**单位**：{money_unit}",
        f"**笔数**：{len(records)} 笔",
    ]
    if not records:
        return "  \n".join(header + ["", "该区间内没有已审批完成的收支记录。"])

    rows = []
    for record in records[:max_rows]:
        reason = (record.get("reason") or "").replace("\n", " ")
        if len(reason) > reason_width:
            reason = reason[:reason_width] + "…"
        rows.append([
            record.get("biz_date") or "-",
            record.get("label") or ("收入" if record.get("kind") == "income" else "支出"),
            fmt_money(float(record.get("amount") or 0)),
            reason or "-",
            record.get("originator_name") or record.get("originator_user_id") or "-",
        ])

    blocks = header + ["", "```", _render_table(DETAIL_HEADERS, rows), "```"]
    if len(records) > max_rows:
        blocks.append(f"…… 另有 {len(records) - max_rows} 笔未显示，请用「导出收支」看全量。")
    return "  \n".join(blocks)


# ======================================================================
# Excel
# ======================================================================
def build_cashflow_workbook(result: dict, meta: dict) -> bytes:
    """生成 xlsx（按月汇总 + 逐条明细）。"""
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

    summary = result["summary"]
    sheet = workbook.active
    sheet.title = "汇总"
    write_sheet(
        sheet,
        ["月份", "收入", "支出", "结余", "收入笔数", "支出笔数"],
        [[row["month"], row["income"], row["expense"], row["net"],
          row["income_count"], row["expense_count"]]
         for row in summary["rows"]],
        [12, 14, 14, 14, 10, 10],
    )

    totals = summary["totals"]
    sheet.append([])
    sheet.append(["合计", totals["income"], totals["expense"], totals["net"],
                  totals["income_count"], totals["expense_count"]])
    sheet.append([])
    sheet.append(["区间", str(meta.get("range_text") or ""),
                  "单位", str(meta.get("money_unit") or "元")])
    sheet.append(["导出时间", str(meta.get("exported_at") or ""),
                  "笔数", len(result["records"])])
    if meta.get("cache_note"):
        sheet.append(["缓存", str(meta["cache_note"])])

    detail_sheet = workbook.create_sheet("明细")
    write_sheet(
        detail_sheet, EXCEL_DETAIL_HEADERS,
        [[record.get("biz_date") or "",
          record.get("label") or "",
          float(record.get("amount") or 0),
          record.get("reason") or "",
          record.get("originator_name") or record.get("originator_user_id") or "",
          record.get("instance_id") or "",
          record.get("title") or "",
          record.get("status") or ""]
         for record in result["records"]],
        [12, 8, 12, 40, 12, 36, 40, 12],
    )

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
