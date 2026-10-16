#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
审批事件路由器。

为什么需要它：
  SDK 的 `DingTalkStreamClient.register_all_event_handler()` 是**单槽位覆盖**
  （源码里就是 `self.event_handler = handler`），注册第二个会把第一个顶掉。
  所以要接多个审批模板，必须先占住这个唯一的槽位，再在内部按 processCode 分发。

各业务处理器只需实现：
    process_code  -> 关注哪个审批模板
    async handle(data) -> 处理该模板的事件（data 就是事件的 data 字段）

事件类型 / type / result 的过滤由各处理器自己负责，路由器只按模板分发。
"""

import logging
from typing import Iterable, List

import dingtalk_stream
from dingtalk_stream import AckMessage


class ApprovalEventRouter(dingtalk_stream.EventHandler):
    EVENT_TYPE = "bpms_instance_change"

    def __init__(self, handlers: Iterable, logger: logging.Logger):
        super().__init__()
        self.logger = logger
        self._handlers = {}
        for handler in handlers:
            process_code = (getattr(handler, "process_code", "") or "").strip()
            if not process_code:
                self.logger.warning("处理器 %s 没有配置 process_code，已忽略", type(handler).__name__)
                continue
            if process_code in self._handlers:
                self.logger.warning(
                    "审批模板 %s 被重复注册，后注册的 %s 会覆盖 %s",
                    process_code, type(handler).__name__, type(self._handlers[process_code]).__name__,
                )
            self._handlers[process_code] = handler

        self.logger.info(
            "审批事件路由器已就绪 | 已注册模板: %s",
            ", ".join(f"{code}({type(h).__name__})" for code, h in self._handlers.items()) or "无",
        )

    @property
    def process_codes(self) -> List[str]:
        return list(self._handlers)

    async def process(self, event: dingtalk_stream.EventMessage):
        if event.headers.event_type != self.EVENT_TYPE:
            return AckMessage.STATUS_OK, "OK"

        data = event.data or {}
        process_code = (data.get("processCode") or "").strip()
        instance_id = data.get("processInstanceId") or ""
        handler = self._handlers.get(process_code)

        if handler is None:
            # 企业里其它审批模板的事件也会推过来，这里只记 debug，避免刷屏
            self.logger.debug(
                "未注册的审批模板，忽略 | processCode=%s | instanceId=%s",
                process_code, instance_id,
            )
            return AckMessage.STATUS_OK, "OK"

        try:
            await handler.handle(data)
        except Exception as e:
            # 单个模板的处理器出错，不能让整条事件链路挂掉
            self.logger.error(
                "审批处理器执行失败 | processCode=%s | instanceId=%s | %s",
                process_code, instance_id, e, exc_info=True,
            )
        return AckMessage.STATUS_OK, "OK"
