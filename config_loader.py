#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import re
from dataclasses import dataclass, field
from typing import List

import yaml


@dataclass
class DingTalkConfig:
    client_id: str
    client_secret: str


@dataclass
class ApprovalConfig:
    reward_leave_process_code: str
    reward_leave_code: str
    admin_user_id: str


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = ""
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5


@dataclass
class ConnectionConfig:
    max_consecutive_failures: int = 5
    reconnect_backoff: List[int] = field(default_factory=lambda: [2, 5, 10, 30, 60])


@dataclass
class CheckinPlaceConfig:
    """一条外勤地点规则：地点（经纬度） + 半径（米） + 生效时段。"""
    name: str
    longitude: float
    latitude: float
    radius: float
    time_range: str


@dataclass
class MongoConfig:
    uri: str = "mongodb://127.0.0.1:27017"
    database: str = "dingtalk"
    collection: str = "checkin_records"
    users_collection: str = "users"
    server_selection_timeout_ms: int = 3000


@dataclass
class CheckinConfig:
    """外勤打卡轮询配置。"""
    enabled: bool = False
    poll_interval: int = 6000            # 轮询间隔（秒）
    lookback_days: int = 1             # 每次回看天数（接口上限 7 天）
    utc_offset_hours: int = 8          # 时段判定所用时区偏移，东八区 = 8
    user_cache_ttl: int = 1800         # 部门员工列表缓存时长（秒）
    include_sub_departments: bool = True
    department_ids: List[int] = field(default_factory=list)
    require_outside: bool = True       # 仅记录外勤（locationResult=Outside）
    exclude_invalid_records: bool = False
    places: List[CheckinPlaceConfig] = field(default_factory=list)
    mongo: MongoConfig = field(default_factory=MongoConfig)


@dataclass
class RobotConfig:
    """外勤打卡查询机器人配置。"""
    enabled: bool = False
    admin_user_ids: List[str] = field(default_factory=list)
    robot_code: str = ""               # 留空则用 dingtalk.client_id
    default_range_days: int = 7        # 不写日期时默认查最近几天
    max_summary_rows: int = 30         # 聊天里最多展示多少条（地点/日期）
    export_limit: int = 20000          # 单次导出上限行数
    file_name_prefix: str = "外勤打卡记录"


@dataclass
class AppConfig:
    dingtalk: DingTalkConfig
    approval: ApprovalConfig
    logging: LoggingConfig
    connection: ConnectionConfig
    checkin: CheckinConfig = field(default_factory=CheckinConfig)
    robot: RobotConfig = field(default_factory=RobotConfig)


def _build_robot_config(raw: dict) -> RobotConfig:
    rb = raw.get("robot") or {}
    cfg = RobotConfig(
        enabled=bool(rb.get("enabled", False)),
        admin_user_ids=[str(u).strip() for u in (rb.get("admin_user_ids") or [])
                        if str(u).strip()],
        robot_code=str(rb.get("robot_code", "")).strip(),
        default_range_days=int(rb.get("default_range_days", 7)),
        max_summary_rows=int(rb.get("max_summary_rows", 30)),
        export_limit=int(rb.get("export_limit", 20000)),
        file_name_prefix=str(rb.get("file_name_prefix", "外勤打卡记录")).strip()
        or "外勤打卡记录",
    )

    if cfg.enabled:
        if not cfg.admin_user_ids:
            raise ValueError("robot.admin_user_ids 未配置（允许使用机器人的白名单 userId）")
        if cfg.default_range_days < 1:
            raise ValueError("robot.default_range_days 必须大于 0")
        if cfg.max_summary_rows < 1:
            raise ValueError("robot.max_summary_rows 必须大于 0")
        if cfg.export_limit < 1:
            raise ValueError("robot.export_limit 必须大于 0")

    return cfg


_TIME_RANGE_RE = re.compile(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$")


def _build_checkin_config(raw: dict) -> CheckinConfig:
    ck = raw.get("checkin") or {}
    mg = ck.get("mongo") or {}

    places: List[CheckinPlaceConfig] = []
    for item in ck.get("places") or []:
        name = str(item.get("name", "")).strip()
        if not name:
            raise ValueError("checkin.places[].name 未配置")
        try:
            place = CheckinPlaceConfig(
                name=name,
                longitude=float(item["longitude"]),
                latitude=float(item["latitude"]),
                radius=float(item["radius"]),
                time_range=str(item.get("time_range", "")).strip(),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(
                f"checkin.places[{name}] 的 longitude/latitude/radius 必须为数字: {e}"
            ) from e

        if not _TIME_RANGE_RE.match(place.time_range):
            raise ValueError(
                f"checkin.places[{name}].time_range 格式应为 HH:MM-HH:MM，"
                f"当前为 {place.time_range!r}"
            )
        if not (-180.0 <= place.longitude <= 180.0):
            raise ValueError(f"checkin.places[{name}].longitude 超出范围")
        if not (-90.0 <= place.latitude <= 90.0):
            raise ValueError(f"checkin.places[{name}].latitude 超出范围")
        if place.radius <= 0:
            raise ValueError(f"checkin.places[{name}].radius 必须大于 0")
        places.append(place)

    cfg = CheckinConfig(
        enabled=bool(ck.get("enabled", False)),
        poll_interval=int(ck.get("poll_interval", 6000)),
        lookback_days=int(ck.get("lookback_days", 1)),
        utc_offset_hours=int(ck.get("utc_offset_hours", 8)),
        user_cache_ttl=int(ck.get("user_cache_ttl", 1800)),
        include_sub_departments=bool(ck.get("include_sub_departments", True)),
        department_ids=[int(d) for d in (ck.get("department_ids") or [])],
        require_outside=bool(ck.get("require_outside", True)),
        exclude_invalid_records=bool(ck.get("exclude_invalid_records", False)),
        places=places,
        mongo=MongoConfig(
            uri=os.environ.get("MONGODB_URI") or mg.get("uri", "mongodb://127.0.0.1:27017"),
            database=mg.get("database", "dingtalk"),
            collection=mg.get("collection", "checkin_records"),
            server_selection_timeout_ms=int(mg.get("server_selection_timeout_ms", 3000)),
        ),
    )

    if cfg.enabled:
        if not cfg.department_ids:
            raise ValueError("checkin.department_ids 未配置（根部门传 1）")
        if not cfg.places:
            raise ValueError("checkin.places 未配置")
        if cfg.poll_interval < 10:
            raise ValueError("checkin.poll_interval 太小，建议不小于 10 秒")
        if cfg.lookback_days < 0:
            raise ValueError("checkin.lookback_days 不能为负")
        if not (0 <= cfg.utc_offset_hours <= 14):
            raise ValueError("checkin.utc_offset_hours 取值非法")

    return cfg


def load_config(path: str = "config.yaml") -> AppConfig:
    if not os.path.exists(path):
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    dt = raw.get("dingtalk", {})
    client_id = os.environ.get("DINGTALK_CLIENT_ID") or dt.get("client_id", "")
    client_secret = os.environ.get("DINGTALK_CLIENT_SECRET") or dt.get("client_secret", "")
    if not client_id or client_id.startswith("your_"):
        raise ValueError("client_id 未配置")
    if not client_secret or client_secret.startswith("your_"):
        raise ValueError("client_secret 未配置")

    ap = raw.get("approval", {})
    approval_cfg = ApprovalConfig(
        reward_leave_process_code=ap.get("reward_leave_process_code", ""),
        reward_leave_code=ap.get("reward_leave_code", ""),
        admin_user_id=ap.get("admin_user_id", ""),
    )
    if not approval_cfg.reward_leave_process_code:
        raise ValueError("approval.reward_leave_process_code 未配置")
    if not approval_cfg.reward_leave_code:
        raise ValueError("approval.reward_leave_code 未配置")
    if not approval_cfg.admin_user_id:
        raise ValueError("approval.admin_user_id 未配置")

    lg = raw.get("logging", {})
    logging_cfg = LoggingConfig(
        level=lg.get("level", "INFO"),
        file=lg.get("file", ""),
        max_bytes=int(lg.get("max_bytes", 10 * 1024 * 1024)),
        backup_count=int(lg.get("backup_count", 5)),
    )

    cn = raw.get("connection", {})
    conn_cfg = ConnectionConfig(
        max_consecutive_failures=int(cn.get("max_consecutive_failures", 5)),
        reconnect_backoff=list(cn.get("reconnect_backoff", [2, 5, 10, 30, 60])),
    )

    checkin_cfg = _build_checkin_config(raw)
    robot_cfg = _build_robot_config(raw)

    return AppConfig(
        dingtalk=DingTalkConfig(client_id, client_secret),
        approval=approval_cfg,
        logging=logging_cfg,
        connection=conn_cfg,
        checkin=checkin_cfg,
        robot=robot_cfg,
    )