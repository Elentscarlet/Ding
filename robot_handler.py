#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
外勤打卡查询机器人（钉钉 Stream 模式的机器人消息回调）。

交互（在单聊里直接说，或在群里 @机器人）：
  查询 [日期] [日期] [地点]   默认最近 N 天；日期支持 今天/昨天/前天
  导出 [同上]                生成 Excel 私发给提问的人（汇总 + 明细两个 sheet）
  地点                       列出已配置的外勤地点
  帮助                       使用说明

鉴权：只有 robot.admin_user_ids 白名单内的人可以使用。

为什么继承 AsyncChatbotHandler：
  查询/导出的耗时可能到秒级，而钉钉要求尽快回 ack，否则会重复推送同一条消息。
  AsyncChatbotHandler 会立刻回 ack，再在线程池里执行 process()。
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Sequence

from dingtalk_stream import AckMessage
from dingtalk_stream.chatbot import AsyncChatbotHandler, ChatbotMessage

from checkin_report import (build_result, build_workbook, export_row_groups,
                            format_summary_markdown)

# 只匹配 2026-10-13 / 2026-1-3 这类写法，后续再交给 strptime 校验
DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
RELATIVE_DAYS = {"今天": 0, "昨天": -1, "前天": -2}
EXPORT_WORDS = ("导出", "下载", "export", "excel")
QUERY_WORDS = ("查询", "查", "query", "记录")
HELP_WORDS = ("帮助", "help", "?", "？", "你好", "hello")
PLACE_WORDS = ("地点", "地点列表", "places")

HELP_TEXT = "  \n".join([
    "**外勤打卡记录查询**",
    "",
    "**查询 [日期] [日期] [地点]**",
    "例：查询 2026-10-13",
    "例：查询 2026-10-13 2026-10-14 新桥派出所",
    "日期可以写 今天 / 昨天 / 前天；不写日期默认最近 7 天。",
    "",
    "**导出 [同上]**：生成 Excel 私发给你（含汇总 + 明细两个 sheet）",
    "",
    "**地点**：查看已配置的外勤地点",
    "",
    "**帮助**：显示本说明",
])


# ======================================================================
# 指令解析（纯函数，可单测）
# ======================================================================
def parse_command(text: str, place_names: Sequence[str] = (),
                  today: Optional[date] = None) -> dict:
    """把聊天文本解析成 {action, from, to, place, note}。

    :param today: 用于解析"今天/昨天/前天"，测试时可固定。
    """
    today = today or datetime.now().date()
    tokens = [t for t in re.split(r"\s+", (text or "").strip())
              if t and not t.startswith("@")]
    if not tokens:
        return {"action": "help", "from": None, "to": None, "place": None, "note": None}

    head = tokens[0]
    if head in HELP_WORDS:
        return {"action": "help", "from": None, "to": None, "place": None, "note": None}
    if head in PLACE_WORDS:
        return {"action": "places", "from": None, "to": None, "place": None, "note": None}

    action = "query"
    if head in EXPORT_WORDS:
        action = "export"
        tokens = tokens[1:]
    elif head in QUERY_WORDS:
        tokens = tokens[1:]

    dates: List[str] = []
    others: List[str] = []
    for token in tokens:
        if token in RELATIVE_DAYS:
            moment = today + timedelta(days=RELATIVE_DAYS[token])
            dates.append(moment.strftime("%Y-%m-%d"))
            continue
        if DATE_RE.fullmatch(token):
            try:
                dates.append(datetime.strptime(token, "%Y-%m-%d").strftime("%Y-%m-%d"))
                continue
            except ValueError:
                pass
        others.append(token)

    from_text = to_text = None
    if dates:
        ordered = sorted(dates)
        from_text, to_text = ordered[0], ordered[-1]

    place: Optional[str] = None
    note: Optional[str] = None
    if others:
        keyword = " ".join(others)
        matched = match_place(keyword, place_names)
        if matched:
            place = matched
        else:
            note = f"没找到地点「{keyword}」，已按全部地点查询"

    return {"action": action, "from": from_text, "to": to_text,
            "place": place, "note": note}


def match_place(keyword: str, place_names: Sequence[str]) -> Optional[str]:
    """先精确匹配，再做双向包含匹配（「新桥」能命中「新桥派出所」）。"""
    key = (keyword or "").strip()
    if not key:
        return None
    for name in place_names:
        if name == key:
            return name
    for name in place_names:
        if key in name or name in key:
            return name
    return None


def render_place_list(places: Sequence) -> str:
    if not places:
        return "还没有配置任何外勤地点（config.yaml 里的 checkin.places）。"
    lines = ["**已配置的外勤地点**", ""]
    for place in places:
        lines.append(
            f"· **{place.name}** —— 半径 {int(place.radius)} 米，时段 {place.time_range}"
        )
    return "  \n".join(lines)


# ======================================================================
# 机器人处理器
# ======================================================================
class CheckinRobotHandler(AsyncChatbotHandler):
    def __init__(self, cfg, api, checkin_store, user_store, tz: timezone,
                 places: Sequence, logger: logging.Logger):
        """
        :param cfg: AppConfig.robot
        :param api: DingTalkApiClient
        :param checkin_store: CheckinStore
        :param user_store: UserStore（可为 None）
        :param tz: 展示用时区
        :param places: AppConfig.checkin.places
        :param logger: 日志对象
        """
        super().__init__(max_workers=4)
        self.cfg = cfg
        self.api = api
        self.checkin_store = checkin_store
        self.user_store = user_store
        self.tz = tz
        self.places = list(places or [])
        self.place_names = [p.name for p in self.places]
        self.logger = logger

    # ------------------------------------------------------------------
    # 入口（注意：AsyncChatbotHandler 要求 process 不是 async）
    # ------------------------------------------------------------------
    def process(self, callback):
        try:
            incoming = ChatbotMessage.from_dict(callback.data)
        except Exception as e:
            self.logger.error("解析机器人消息失败: %s", e, exc_info=True)
            return AckMessage.STATUS_OK, "OK"

        try:
            self.handle(incoming)
        except Exception as e:
            self.logger.error("处理机器人消息失败: %s", e, exc_info=True)
            self._reply(incoming, "处理失败", f"处理出错了：{e}")
        return AckMessage.STATUS_OK, "OK"

    def handle(self, incoming: ChatbotMessage):
        user_id = incoming.sender_staff_id
        text = (incoming.text.content if incoming.text else "") or ""

        if not self.is_allowed(user_id):
            self.logger.warning(
                "非白名单用户使用机器人 | userId=%s | 昵称=%s | 内容=%r",
                user_id, incoming.sender_nick, text,
            )
            self._reply(incoming, "无权限", "你不在查看白名单内，请联系管理员。")
            return

        command = parse_command(text, self.place_names, datetime.now(self.tz).date())
        self.logger.info(
            "机器人指令 | userId=%s | 原文=%r | 解析=%s", user_id, text, command
        )

        action = command["action"]
        if action == "help":
            self._reply(incoming, "使用说明", HELP_TEXT)
        elif action == "places":
            self._reply(incoming, "已配置地点", render_place_list(self.places))
        elif action == "export":
            self.export(incoming, command)
        else:
            self.query(incoming, command)

    def is_allowed(self, user_id: Optional[str]) -> bool:
        return bool(user_id) and user_id in (self.cfg.admin_user_ids or [])

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def _fetch(self, command: dict) -> dict:
        return build_result(
            self.checkin_store, self.user_store, self.tz,
            command.get("from"), command.get("to"), command.get("place"),
            default_days=self.cfg.default_range_days,
            limit=self.cfg.export_limit,
        )

    def query(self, incoming: ChatbotMessage, command: dict):
        try:
            result = self._fetch(command)
        except Exception as e:
            self.logger.error("查询打卡记录失败: %s", e, exc_info=True)
            self._reply(incoming, "查询失败", f"读取数据库失败：{e}  \n请确认 MongoDB 已启动。")
            return

        body = format_summary_markdown(result, max_rows=self.cfg.max_summary_rows)
        if command.get("note"):
            body = f"（{command['note']}）  \n{body}"
        self._reply(incoming, "外勤打卡记录", body)

    # ------------------------------------------------------------------
    # 导出（文件私发给提问的人）
    # ------------------------------------------------------------------
    def export(self, incoming: ChatbotMessage, command: dict):
        user_id = incoming.sender_staff_id
        try:
            result = self._fetch(command)
        except Exception as e:
            self.logger.error("导出前查询失败: %s", e, exc_info=True)
            self._reply(incoming, "导出失败", f"读取数据库失败：{e}  \n请确认 MongoDB 已启动。")
            return

        if not result["summary"]:
            self._reply(incoming, "导出",
                        f"{result['from']} ~ {result['to']} 没有外勤打卡记录，无需导出。")
            return

        summary_rows, detail_rows = export_row_groups(result)
        filename = f"{self.cfg.file_name_prefix}_{result['from']}_{result['to']}.xlsx"
        payload = build_workbook(summary_rows, detail_rows, {
            "range_text": f"{result['from']} ~ {result['to']}",
            "place": result["place"] or "全部",
            "exported_at": datetime.now(self.tz).strftime("%Y-%m-%d %H:%M:%S"),
        })

        try:
            self.api.send_file_to_user(
                user_id, payload, filename,
                file_type="xlsx", robot_code=self.cfg.robot_code,
            )
        except Exception as e:
            self.logger.error("发送导出文件失败 | userId=%s | %s", user_id, e, exc_info=True)
            self._reply(
                incoming, "导出失败",
                f"表格已生成，但发送文件失败：{e}  \n"
                "如果提示没有有效的单聊会话，请先在钉钉里打开机器人会话发一句话，再重试「导出」。",
            )
            return

        self.logger.info(
            "导出已发送 | userId=%s | 文件=%s | 汇总=%d 行 | 明细=%d 行 | %d 字节",
            user_id, filename, len(summary_rows), len(detail_rows), len(payload),
        )
        self._reply(
            incoming, "导出完成",
            f"已把 **{filename}** 私发给你。  \n"
            f"汇总 {len(summary_rows)} 条（地点/日期）｜明细 {len(detail_rows)} 次打卡。  \n"
            "没收到的话，先在钉钉里和机器人单聊一次再试。",
        )

    # ------------------------------------------------------------------
    def _reply(self, incoming: ChatbotMessage, title: str, text: str):
        """回复到当前会话；sessionWebhook 可能已过期，失败只记日志。"""
        try:
            self.reply_markdown(title, text, incoming)
        except Exception as e:
            self.logger.error("机器人回复失败: %s", e, exc_info=True)
