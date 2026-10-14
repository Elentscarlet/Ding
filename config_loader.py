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
class WebConfig:
    """打卡记录查询页（钉钉工作台 H5）配置。"""
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    auth_mode: str = "dingtalk"        # dingtalk = 免登+白名单；none = 不鉴权（仅本机调试）
    corp_id: str = ""
    agent_id: str = ""
    admin_user_ids: List[str] = field(default_factory=list)
    session_secret: str = ""
    session_ttl: int = 8 * 3600        # 会话有效期（秒）
    export_limit: int = 20000          # 单次导出行数上限


@dataclass
class AppConfig:
    dingtalk: DingTalkConfig
    approval: ApprovalConfig
    logging: LoggingConfig
    connection: ConnectionConfig
    checkin: CheckinConfig = field(default_factory=CheckinConfig)
    web: WebConfig = field(default_factory=WebConfig)


def _build_web_config(raw: dict) -> WebConfig:
    wb = raw.get("web") or {}
    cfg = WebConfig(
        enabled=bool(wb.get("enabled", False)),
        host=str(wb.get("host", "0.0.0.0")),
        port=int(wb.get("port", 8080)),
        auth_mode=str(wb.get("auth_mode", "dingtalk")).strip().lower(),
        corp_id=str(wb.get("corp_id", "")).strip(),
        agent_id=str(wb.get("agent_id", "")).strip(),
        admin_user_ids=[str(u).strip() for u in (wb.get("admin_user_ids") or []) if str(u).strip()],
        session_secret=os.environ.get("WEB_SESSION_SECRET") or str(wb.get("session_secret", "")),
        session_ttl=int(wb.get("session_ttl", 8 * 3600)),
        export_limit=int(wb.get("export_limit", 20000)),
    )

    if cfg.enabled:
        if cfg.auth_mode not in ("dingtalk", "none"):
            raise ValueError("web.auth_mode 只能是 dingtalk 或 none")
        if not (1 <= cfg.port <= 65535):
            raise ValueError("web.port 取值非法")
        if cfg.session_ttl <= 0:
            raise ValueError("web.session_ttl 必须大于 0")
        if cfg.auth_mode == "dingtalk":
            if not cfg.corp_id:
                raise ValueError("web.corp_id 未配置（钉钉企业 corpId）")
            if not cfg.agent_id:
                raise ValueError("web.agent_id 未配置（应用的 AgentId）")
            if not cfg.admin_user_ids:
                raise ValueError("web.admin_user_ids 未配置（允许查看记录的白名单 userId）")

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
    web_cfg = _build_web_config(raw)

    return AppConfig(
        dingtalk=DingTalkConfig(client_id, client_secret),
        approval=approval_cfg,
        logging=logging_cfg,
        connection=conn_cfg,
        checkin=checkin_cfg,
        web=web_cfg,
    )