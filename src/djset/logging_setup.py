"""Logging: timestamped file log in the app data dir, plus console."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import log_path

_configured = False


class _SharedRotatingFileHandler(RotatingFileHandler):
    """A rotating handler that survives a second process holding the log open.

    Windows takes a mandatory lock on open files, so ``os.replace`` during
    rollover raises ``PermissionError`` whenever another djset process has the
    log open. That is not an exotic case — running the UI while a CLI ``enrich``
    grinds through the library in the background is the intended workflow, and
    a long pass is exactly what pushes the file past ``maxBytes``.

    Stock behaviour is to print a rollover traceback for *every* subsequent
    record, which buried the real output of an enrichment run. Losing rotation
    costs a larger log file; losing the output costs the run's legibility.
    """

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            # Keep writing to the current file. Another process will rotate it,
            # or the next single-process run will.
            if self.stream is None:
                self.stream = self._open()


def setup_logging(verbose: bool = False) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fh = _SharedRotatingFileHandler(
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
