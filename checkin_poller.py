#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
外勤打卡轮询任务。

为什么用轮询而不是 Stream 事件：
  钉钉的 check_in 推送事件只带 {timeStamp, eventId, staffId}，没有地点、
  坐标和外勤标记，无法判断"是否落在指定地点的范围内"，所以改为定时轮询
  「获取打卡详情」接口（/attendance/listRecord），该接口会返回
  userLatitude / userLongitude / locationResult / outsideRemark。

一条打卡被记录的条件（需同时满足）：
  1. locationResult == "Outside"（外勤），可用 require_outside 关掉；
  2. 打卡坐标落在某条地点规则的半径内；
  3. 打卡时间落在该规则的时段内。

"同一次打卡只记录一次"由 record_id（钉钉考勤 ID）唯一索引 + upsert 保证。
"""

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

# 地球平均半径（米），用于 haversine 距离
EARTH_RADIUS_M = 6371008.8


# ======================================================================
# 纯函数：距离与时段
# ======================================================================
def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两个经纬度之间的大圆距离（米）。"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def parse_time_range(text: str) -> Tuple[int, int]:
    """把 "08:00-18:00" 解析成 (起始分钟, 结束分钟)。

    支持跨夜写法，例如 "22:00-06:00"（起始 > 结束表示跨过零点）。
    """
    raw = (text or "").strip()
    if "-" not in raw:
        raise ValueError(f"时段格式应为 HH:MM-HH:MM，收到: {text!r}")

    start_text, _, end_text = raw.partition("-")
    return _parse_hhmm(start_text, text), _parse_hhmm(end_text, text)


def _parse_hhmm(value: str, original: str) -> int:
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"时段格式应为 HH:MM-HH:MM，收到: {original!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时段取值超出范围，收到: {original!r}")
    return hour * 60 + minute


def _chunked(items: list, size: int) -> list:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ======================================================================
# 地点规则
# ======================================================================
class PlaceRule:
    """一条外勤地点规则：坐标 + 半径（米） + 生效时段。"""

    __slots__ = ("name", "longitude", "latitude", "radius",
                 "time_range_text", "start_minute", "end_minute")

    def __init__(self, cfg):
        self.name = cfg.name
        self.longitude = float(cfg.longitude)
        self.latitude = float(cfg.latitude)
        self.radius = float(cfg.radius)
        self.time_range_text = cfg.time_range
        self.start_minute, self.end_minute = parse_time_range(cfg.time_range)

        if not (-180.0 <= self.longitude <= 180.0):
            raise ValueError(f"地点 {self.name!r} 经度非法: {self.longitude}")
        if not (-90.0 <= self.latitude <= 90.0):
            raise ValueError(f"地点 {self.name!r} 纬度非法: {self.latitude}")
        if self.radius <= 0:
            raise ValueError(f"地点 {self.name!r} 半径必须大于 0")

    def covers_time(self, moment: datetime) -> bool:
        minute = moment.hour * 60 + moment.minute
        if self.start_minute <= self.end_minute:
            return self.start_minute <= minute <= self.end_minute
        # 跨夜时段，例如 22:00-06:00
        return minute >= self.start_minute or minute <= self.end_minute

    def distance_to(self, latitude: float, longitude: float) -> float:
        return haversine_meters(latitude, longitude, self.latitude, self.longitude)

    def __repr__(self) -> str:
        return (f"<PlaceRule {self.name!r} ({self.latitude},{self.longitude}) "
                f"r={self.radius}m {self.time_range_text}>")


# ======================================================================
# 轮询任务
# ======================================================================
class CheckinPoller:
    def __init__(self, cfg, api, store, logger: logging.Logger, user_store=None):
        """
        :param cfg: AppConfig.checkin
        :param api: DingTalkApiClient
        :param store: CheckinStore
        :param logger: 日志对象
        :param user_store: UserStore（可选，用于顺便刷新员工姓名缓存）
        """
        self.cfg = cfg
        self.api = api
        self.store = store
        self.logger = logger
        self.user_store = user_store
        self.tz = timezone(timedelta(hours=cfg.utc_offset_hours))
        self.rules: List[PlaceRule] = [PlaceRule(p) for p in cfg.places]

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._userids: List[str] = []
        self._userids_fetched_at = 0.0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self):
        self.logger.info(
            "外勤打卡轮询已启动 | 间隔=%ds | 部门=%s | 含子部门=%s | 地点规则=%d 条 | 回看=%d 天",
            self.cfg.poll_interval, self.cfg.department_ids,
            self.cfg.include_sub_departments, len(self.rules), self.cfg.lookback_days,
        )
        for rule in self.rules:
            self.logger.info("  规则: %s", rule)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="checkin-poller")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        if self.store is not None:
            self.store.close()
        self.logger.info("外勤打卡轮询已停止")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                self.logger.error("外勤打卡轮询异常: %s", e, exc_info=True)
            self._stop_event.wait(self.cfg.poll_interval)

    # ------------------------------------------------------------------
    # 一轮轮询
    # ------------------------------------------------------------------
    def poll_once(self) -> int:
        """执行一轮轮询，返回本次新增的记录数。"""
        if self.store is not None and not self.store.ping():
            self.logger.warning("跳过本轮轮询：MongoDB 不可用")
            return 0

        userids = self._get_userids()
        if not userids:
            self.logger.warning("未获取到任何员工 userId，跳过本轮轮询")
            return 0

        date_from, date_to = self._query_window()
        from_text = date_from.strftime("%Y-%m-%d %H:%M:%S")
        to_text = date_to.strftime("%Y-%m-%d %H:%M:%S")

        scanned = 0
        new_records = 0
        for chunk in _chunked(userids, self.api.MAX_RECORD_USER_IDS):
            try:
                records = self.api.list_attendance_records(chunk, from_text, to_text)
                self.logger.info(records)
            except Exception as e:
                self.logger.error(
                    "拉取打卡记录失败（%d 人），本轮跳过这批: %s", len(chunk), e
                )
                continue

            scanned += len(records)
            for record in records:
                matched = self.match(record)
                if matched is None:
                    continue
                rule, distance, check_time = matched
                doc = self._build_doc(record, rule, distance, check_time)
                try:
                    if self.store.insert_if_absent(doc):
                        new_records += 1
                        self.logger.info(
                            "记录外勤打卡 | 员工=%s | 地点=%s | 距离=%.0fm | 打卡时间=%s | 地址=%s | 备注=%s",
                            doc["user_id"], rule.name, distance,
                            check_time.strftime("%Y-%m-%d %H:%M:%S"),
                            doc.get("user_address") or "-",
                            doc.get("outside_remark") or "-",
                        )
                except Exception as e:
                    self.logger.error(
                        "外勤打卡落库失败 | 员工=%s | recordId=%s | %s",
                        doc.get("user_id"), doc.get("record_id"), e,
                    )

        self.logger.info(
            "本轮轮询完成 | 员工=%d | 扫描记录=%d | 新增=%d", len(userids), scanned, new_records
        )
        return new_records

    # ------------------------------------------------------------------
    # 员工列表（带缓存）
    # ------------------------------------------------------------------
    def _get_userids(self) -> List[str]:
        now = time.monotonic()
        if self._userids and now - self._userids_fetched_at < self.cfg.user_cache_ttl:
            return self._userids

        try:
            users = self.api.collect_department_users(
                self.cfg.department_ids, self.cfg.include_sub_departments
            )
        except Exception as e:
            self.logger.error("获取部门员工列表失败: %s", e)
            return self._userids

        userids = [item["user_id"] for item in users if item.get("user_id")]
        if not userids:
            self.logger.warning("部门员工列表为空，下轮重试")
            return self._userids

        self._userids = userids
        self._userids_fetched_at = now
        self.logger.info(
            "已刷新部门员工列表 | 部门=%s | 人数=%d", self.cfg.department_ids, len(userids)
        )

        # 顺带刷新姓名缓存，供查询页展示"到岗人"
        if self.user_store is not None:
            try:
                changed = self.user_store.upsert_users(users)
                self.logger.info("员工姓名缓存已更新 | 变更=%d 条", changed)
            except Exception as e:
                self.logger.warning("写入员工姓名缓存失败: %s", e)

        return self._userids

    # ------------------------------------------------------------------
    # 查询时间窗
    # ------------------------------------------------------------------
    def _query_window(self) -> Tuple[datetime, datetime]:
        now = datetime.now(self.tz)
        date_to = now
        date_from = (now - timedelta(days=self.cfg.lookback_days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        max_days = self.api.MAX_RECORD_RANGE_DAYS
        if (date_to - date_from) > timedelta(days=max_days):
            self.logger.warning(
                "回看窗口超过接口上限 %d 天，已自动收窄", max_days
            )
            date_from = date_to - timedelta(days=max_days)
        return date_from, date_to

    # ------------------------------------------------------------------
    # 规则匹配
    # ------------------------------------------------------------------
    def match(self, record: dict) -> Optional[Tuple[PlaceRule, float, datetime]]:
        """命中则返回 (规则, 距离米, 打卡时间)，否则 None。"""
        if self.cfg.require_outside:
            if (record.get("locationResult") or "") != "Outside":
                return None

        if self.cfg.exclude_invalid_records and (
            record.get("invalidRecordType") or record.get("invalidRecordMsg")
        ):
            return None

        try:
            latitude = float(record.get("userLatitude"))
            longitude = float(record.get("userLongitude"))
        except (TypeError, ValueError):
            # 考勤机打卡（ATM / DING_ATM）不返回经纬度，无法判定范围
            return None

        try:
            check_time = datetime.fromtimestamp(int(record["userCheckTime"]) / 1000, self.tz)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return None

        for rule in self.rules:
            if not rule.covers_time(check_time):
                continue
            distance = rule.distance_to(latitude, longitude)
            if distance <= rule.radius:
                return rule, distance, check_time
        return None

    # ------------------------------------------------------------------
    # 组装文档
    # ------------------------------------------------------------------
    @staticmethod
    def _build_doc(record: dict, rule: PlaceRule, distance: float,
                   check_time: datetime) -> dict:
        record_id = record.get("id")
        if record_id in (None, ""):
            # 兜底：钉钉没给考勤 ID 时用「员工+打卡时间+上下班」拼一个
            record_id = "{}_{}_{}".format(
                record.get("userId"), record.get("userCheckTime"), record.get("checkType")
            )
        record_id = str(record_id)

        now_utc = datetime.now(timezone.utc)
        return {
            "record_id": record_id,
            "user_id": record.get("userId"),
            # 统一按 UTC 存储，查询时再按时区换算
            "user_check_time": check_time.astimezone(timezone.utc),
            "work_date": _to_utc_datetime(record.get("workDate")),
            "check_type": record.get("checkType"),
            "time_result": record.get("timeResult"),
            "location_result": record.get("locationResult"),
            "is_legal": record.get("isLegal"),
            "source_type": record.get("sourceType"),
            "user_longitude": record.get("userLongitude"),
            "user_latitude": record.get("userLatitude"),
            "user_accuracy": record.get("userAccuracy"),
            "user_address": record.get("userAddress"),
            "outside_remark": record.get("outsideRemark"),
            "invalid_record_type": record.get("invalidRecordType"),
            "invalid_record_msg": record.get("invalidRecordMsg"),
            "plan_id": record.get("planId"),
            "group_id": record.get("groupId"),
            "device_id": record.get("deviceId"),
            # 命中的规则信息，便于回溯口径
            "matched_place": rule.name,
            "matched_place_longitude": rule.longitude,
            "matched_place_latitude": rule.latitude,
            "matched_radius": rule.radius,
            "distance_meters": round(distance, 1),
            "rule_time_range": rule.time_range_text,
            "created_at": now_utc,
            "raw": record,
        }


def _to_utc_datetime(milliseconds) -> Optional[datetime]:
    if milliseconds in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(milliseconds) / 1000, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
