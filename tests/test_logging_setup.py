"""Log rotation must not take the run down with it.

Windows locks open files, so a rollover fails whenever a second djset process
has the log open — which is the normal case here, since running the UI while a
CLI `enrich` works through the library in the background is the intended
workflow, and a long pass is what pushes the file past maxBytes in the first
place. Stock RotatingFileHandler responds by printing a traceback for every
record from then on, which buried the output of a real enrichment run.
"""

from __future__ import annotations

import logging

import pytest

from djset.logging_setup import _SharedRotatingFileHandler


@pytest.fixture
def handler(tmp_path):
    h = _SharedRotatingFileHandler(tmp_path / "djset.log", maxBytes=200, backupCount=2)
    h.setFormatter(logging.Formatter("%(message)s"))
    yield h
    h.close()


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("djset", logging.INFO, __file__, 1, msg, None, None)


def test_a_locked_rollover_does_not_stop_logging(handler, monkeypatch):
    monkeypatch.setattr(
        handler,
        "rotate",
        lambda src, dst: (_ for _ in ()).throw(PermissionError("locked by another process")),
    )

    for i in range(40):
        handler.emit(_record(f"line {i} " + "x" * 40))

    handler.flush()
    written = handler.baseFilename
    with open(written, encoding="utf-8") as fh:
        body = fh.read()

    # Rotation was lost, but every record still landed.
    assert "line 0" in body
    assert "line 39" in body


def test_the_stream_is_reopened_if_rollover_closed_it(handler, monkeypatch):
    def close_then_fail(src, dst):
        raise OSError("cannot rename")

    monkeypatch.setattr(handler, "rotate", close_then_fail)
    handler.stream.close()
    handler.stream = None

    handler.doRollover()

    assert handler.stream is not None
    handler.emit(_record("still alive"))
    handler.flush()
    with open(handler.baseFilename, encoding="utf-8") as fh:
        assert "still alive" in fh.read()


def test_rotation_still_happens_when_nothing_is_holding_the_file(handler, tmp_path):
    for i in range(40):
        handler.emit(_record(f"line {i} " + "x" * 40))
    handler.flush()

    # The single-process path is unchanged: backups are produced as normal.
    assert (tmp_path / "djset.log.1").exists()


def test_a_non_os_error_is_not_swallowed(handler, monkeypatch):
    """Only the locking failure is tolerated. A programming error must surface."""
    monkeypatch.setattr(
        handler, "rotate", lambda src, dst: (_ for _ in ()).throw(ValueError("bug"))
    )
    with pytest.raises(ValueError):
        handler.doRollover()
