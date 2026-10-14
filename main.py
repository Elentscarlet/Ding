#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
钉钉 Stream 事件订阅服务 - 主入口
"""

import os
import signal
import threading
import time

import dingtalk_stream

from api import DingTalkApiClient
from checkin_poller import CheckinPoller
from config_loader import load_config
from db import CheckinStore, UserStore
from handlers import ApprovalEventHandler
from logger_setup import setup_logger
from stream_runner import StreamRunner
from web_server import WebServer


def main():
    # 1. 加载配置
    config_path = os.environ.get("DINGTALK_CONFIG", "config.yaml")
    cfg = load_config(config_path)
    logger = setup_logger(cfg)

    logger.info("配置加载完成 | config=%s", os.path.abspath(config_path))
    logger.info("Client ID: %s***", cfg.dingtalk.client_id[:6])
    logger.info("奖励假期 processCode: %s", cfg.approval.reward_leave_process_code)
    logger.info("奖励假期 leaveCode: %s", cfg.approval.reward_leave_code)

    # 2. 初始化钉钉 API 客户端
    api = DingTalkApiClient(
        client_id=cfg.dingtalk.client_id,
        client_secret=cfg.dingtalk.client_secret,
        admin_user_id=cfg.approval.admin_user_id,
        logger=logger,
    )

    # 3. 初始化 Stream 客户端
    credential = dingtalk_stream.Credential(
        cfg.dingtalk.client_id, cfg.dingtalk.client_secret
    )
    client = dingtalk_stream.DingTalkStreamClient(credential, logger=logger)

    # 4. 注册事件处理器
    client.register_all_event_handler(
        ApprovalEventHandler(cfg.approval, api, logger)
    )

    # 5. 准备依赖 MongoDB 的组件（构造失败会直接退出，不会留下半启动状态）
    checkin_store = None
    user_store = None
    checkin_poller = None
    web_server = None

    if cfg.checkin.enabled or cfg.web.enabled:
        checkin_store = CheckinStore(cfg.checkin.mongo, logger)
        user_store = UserStore(cfg.checkin.mongo, logger)

    if cfg.checkin.enabled:
        checkin_poller = CheckinPoller(
            cfg.checkin, api, checkin_store, logger, user_store
        )
    else:
        logger.info("外勤打卡轮询未启用（checkin.enabled = false）")

    if cfg.web.enabled:
        web_server = WebServer(
            cfg.web, api, checkin_store, user_store,
            cfg.checkin.utc_offset_hours, logger,
        )
    else:
        logger.info("打卡查询页未启用（web.enabled = false）")

    # 6. 启动 Stream 运行器、轮询任务与查询页
    runner = StreamRunner(client, cfg, logger)
    runner.start()
    if checkin_poller is not None:
        checkin_poller.start()
    if web_server is not None:
        web_server.start()

    # 7. 等待退出信号
    stop_event = threading.Event()

    def _shutdown(signum, _frame):
        logger.info("收到信号 %s，正在优雅关闭 ...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while not stop_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
        if checkin_poller is not None:
            checkin_poller.stop()
        if web_server is not None:
            web_server.stop()
        logger.info("程序已退出")


if __name__ == "__main__":
    main()