"""Logging: timestamped file log in the app data dir, plus console."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import log_path

_configured = False


def setup_logging(verbose: bool = False) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fh = RotatingFileHandler(
        log_path(), maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(ch)

    # httpx logs every request at INFO; too chatty for a multi-thousand-track pass.
    logging.getLogger("httpx").setLevel(logging.WARNING)
