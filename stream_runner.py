#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
在独立线程中运行 DingTalkStreamClient，内置熔断重连。
"""

import asyncio
import logging
import threading
from typing import Optional


class StreamRunner:
    def __init__(self, client, cfg, logger: logging.Logger):
        self.client = client
        self.cfg = cfg
        self.logger = logger
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        failures = 0
        backoff = self.cfg.connection.reconnect_backoff
        max_failures = self.cfg.connection.max_consecutive_failures

        try:
            while not self._stop_event.is_set() and failures < max_failures:
                try:
                    self.logger.info("正在建立 Stream 长连接 ...")
                    self.client.start_forever()
                    self.logger.warning("Stream 连接已正常退出")
                    failures = 0
                    break
                except Exception as e:
                    failures += 1
                    wait = backoff[min(failures - 1, len(backoff) - 1)]
                    self.logger.error(
                        "Stream 连接异常（第 %d/%d 次）: %s，%d 秒后重试",
                        failures, max_failures, e, wait, exc_info=True,
                    )
                    if self._stop_event.wait(wait):
                        break

            if failures >= max_failures:
                self.logger.critical("连续 %d 次连接失败，停止重试", max_failures)
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self.logger.info("Stream 线程已退出")

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)