#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
import logging.handlers
import os
import sys


def setup_logger(cfg) -> logging.Logger:
    logger = logging.getLogger("dingtalk_stream_event")
    logger.setLevel(getattr(logging, cfg.logging.level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if cfg.logging.file:
        os.makedirs(os.path.dirname(cfg.logging.file) or ".", exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            cfg.logging.file,
            maxBytes=cfg.logging.max_bytes,
            backupCount=cfg.logging.backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger