import dingtalk_stream
import logging
from dingtalk_stream import AckMessage

from api import DingTalkApiClient


class CheckinEventHandler(dingtalk_stream.EventHandler):
    EVENT_TYPE = "check_in"

    def __init__(self, approval_cfg, checkin_api: DingTalkApiClient, logger: logging.Logger):
        super().__init__()
        self.cfg = approval_cfg
        self.api = checkin_api
        self.logger = logger

    async def process(self, event):
        if event.headers.event_type != self.EVENT_TYPE:
            return AckMessage.STATUS_OK, "OK"

        data = event.data

        # 推送事件内容过少，启用轮询查询，不走stream

        self.logger.info("data: %s", data)
        # 匹配外勤任务、推送公示 ...
        return AckMessage.STATUS_OK, "OK"
