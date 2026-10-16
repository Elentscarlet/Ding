#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

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
class ApprovalRuleConfig:
    """一条审批提交校验规则。"""
    field: str                      # 定位组件：name / id / bizAlias / componentType
    type: str                       # number_range | date_compare | ext_value_compare
    message: str = ""               # 自定义未通过提示；留空则自动生成
    min: Optional[float] = None     # number_range 下限（可只填一个）
    max: Optional[float] = None     # number_range 上限
    op: str = ""                    # 比较符 gt/ge/lt/le/eq/ne
    compare_field: str = ""         # date_compare 的对比字段；填 @now 表示当前时间
    left_key: str = ""              # ext_value_compare：组件 extValue 里的左值 key
    right_key: str = ""             # ext_value_compare：组件 extValue 里的右值 key
    tolerance: float = 0.0          # ext_value_compare 数值比较容差
    ignore_right: List[str] = field(default_factory=list)
    # ignore_right：把这些右值视为"未自定义"并跳过比较；留空 = 严格比较（默认）


@dataclass
class ApprovalCheckConfig:
    """审批提交校验（提交时校验，未通过直接驳回）。"""
    enabled: bool = False
    process_code: str = ""
    actioner_user_id: str = ""      # 驳回操作人：仅当任务里取不到 userId 时的兜底
    remark_prefix: str = "系统校验未通过："
    task_wait_retries: int = 5      # 待办任务未生成时的重试次数
    task_wait_interval: float = 1.0 # 每次重试间隔（秒）
    on_missing_field: str = "skip"  # 表单里找不到字段时：skip | reject
    dry_run: bool = False           # true = 只记录不驳回（上线观察用）
    rules: List[ApprovalRuleConfig] = field(default_factory=list)


@dataclass
class AppConfig:
    dingtalk: DingTalkConfig
    approval: ApprovalConfig
    logging: LoggingConfig
    connection: ConnectionConfig
    checkin: CheckinConfig = field(default_factory=CheckinConfig)
    robot: RobotConfig = field(default_factory=RobotConfig)
    approval_check: ApprovalCheckConfig = field(default_factory=ApprovalCheckConfig)


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


_RULE_TYPES = ("number_range", "date_compare", "ext_value_compare")
_COMPARE_OPS = ("gt", "ge", "lt", "le", "eq", "ne")
_ON_MISSING = ("skip", "reject")


def _optional_float(value, field: str, key: str):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"approval_check.rules[{field}].{key} 必须是数字: {value!r}") from e


def _build_approval_check_config(raw: dict) -> ApprovalCheckConfig:
    ac = raw.get("approval_check") or {}

    rules: List[ApprovalRuleConfig] = []
    for item in ac.get("rules") or []:
        item_field = str(item.get("field", "")).strip()
        rule_type = str(item.get("type", "")).strip()
        if not item_field:
            raise ValueError("approval_check.rules[].field 未配置")
        if rule_type not in _RULE_TYPES:
            raise ValueError(
                f"approval_check.rules[{item_field}].type 只支持 {_RULE_TYPES}，"
                f"当前为 {rule_type!r}"
            )

        rule = ApprovalRuleConfig(
            field=item_field,
            type=rule_type,
            message=str(item.get("message", "")).strip(),
            min=_optional_float(item.get("min"), item_field, "min"),
            max=_optional_float(item.get("max"), item_field, "max"),
            op=str(item.get("op", "")).strip().lower(),
            compare_field=str(item.get("compare_field", "")).strip(),
            left_key=str(item.get("left_key", "")).strip(),
            right_key=str(item.get("right_key", "")).strip(),
            tolerance=_optional_float(item.get("tolerance"), item_field, "tolerance") or 0.0,
            ignore_right=[str(v).strip() for v in (item.get("ignore_right") or [])
                          if str(v).strip() != ""],
        )

        if rule_type == "number_range":
            if rule.min is None and rule.max is None:
                raise ValueError(
                    f"approval_check.rules[{item_field}] 至少需要 min 或 max 之一"
                )
            if rule.min is not None and rule.max is not None and rule.min > rule.max:
                raise ValueError(f"approval_check.rules[{item_field}] 的 min 不能大于 max")
        elif rule_type == "date_compare":
            if rule.op not in _COMPARE_OPS:
                raise ValueError(
                    f"approval_check.rules[{item_field}].op 只支持 {_COMPARE_OPS}，"
                    f"当前为 {rule.op!r}"
                )
            if not rule.compare_field:
                raise ValueError(
                    f"approval_check.rules[{item_field}].compare_field 未配置"
                )
        else:   # ext_value_compare
            if not rule.left_key or not rule.right_key:
                raise ValueError(
                    f"approval_check.rules[{item_field}] 需要同时配置 left_key 与 right_key"
                )
            if not rule.op:
                rule.op = "eq"          # 一致性比对默认等值
            if rule.op not in _COMPARE_OPS:
                raise ValueError(
                    f"approval_check.rules[{item_field}].op 只支持 {_COMPARE_OPS}，"
                    f"当前为 {rule.op!r}"
                )
            if rule.tolerance < 0:
                raise ValueError(f"approval_check.rules[{item_field}].tolerance 不能为负")
        rules.append(rule)

    cfg = ApprovalCheckConfig(
        enabled=bool(ac.get("enabled", False)),
        process_code=str(ac.get("process_code", "")).strip(),
        actioner_user_id=str(ac.get("actioner_user_id", "")).strip(),
        remark_prefix=str(ac.get("remark_prefix", "系统校验未通过：")),
        task_wait_retries=int(ac.get("task_wait_retries", 5)),
        task_wait_interval=float(ac.get("task_wait_interval", 1.0)),
        on_missing_field=str(ac.get("on_missing_field", "skip")).strip().lower(),
        dry_run=bool(ac.get("dry_run", False)),
        rules=rules,
    )

    if cfg.enabled:
        if not cfg.process_code:
            raise ValueError("approval_check.process_code 未配置")
        # actioner_user_id 不再是必填：默认用当前待办任务的审批人，
        # 这里只是任务里取不到 userId 时的兜底。
        if not cfg.rules:
            raise ValueError("approval_check.rules 未配置")
        if cfg.task_wait_retries < 1:
            raise ValueError("approval_check.task_wait_retries 必须大于 0")
        if cfg.task_wait_interval < 0:
            raise ValueError("approval_check.task_wait_interval 不能为负")
        if cfg.on_missing_field not in _ON_MISSING:
            raise ValueError(f"approval_check.on_missing_field 只支持 {_ON_MISSING}")

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
    approval_check_cfg = _build_approval_check_config(raw)

    return AppConfig(
        dingtalk=DingTalkConfig(client_id, client_secret),
        approval=approval_cfg,
        logging=logging_cfg,
        connection=conn_cfg,
        checkin=checkin_cfg,
        robot=robot_cfg,
        approval_check=approval_check_cfg,
    )