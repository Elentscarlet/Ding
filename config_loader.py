#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
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
class AppConfig:
    dingtalk: DingTalkConfig
    approval: ApprovalConfig
    logging: LoggingConfig
    connection: ConnectionConfig


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

    return AppConfig(
        dingtalk=DingTalkConfig(client_id, client_secret),
        approval=approval_cfg,
        logging=logging_cfg,
        connection=conn_cfg,
    )