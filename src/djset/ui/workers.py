"""QThread workers.

All network and database work happens here, never on the Qt main thread —
enrichment of a large library takes minutes and must not freeze the window.
Results come back over signals.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

log = logging.getLogger(__name__)


class Worker(QObject):
    """Runs one callable off the main thread.

    The callable receives ``progress`` (a str -> None callback) and ``cancel``
    (a ``threading.Event``) as keyword arguments if it accepts them.
    """

    progressed = Signal(str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            result = self._fn(
                *self._args,
                progress=self.progressed.emit,
                cancel=self.cancel_event,
                **self._kwargs,
            )
        except TypeError as exc:
            # The callable does not take progress/cancel — call it plainly
            # rather than pretending the signature matched.
            if "progress" not in str(exc) and "cancel" not in str(exc):
                self.failed.emit(str(exc))
                return
            try:
                result = self._fn(*self._args, **self._kwargs)
            except Exception as inner:  # noqa: BLE001
                log.exception("worker failed")
                self.failed.emit(str(inner))
                return
        except Exception as exc:  # noqa: BLE001
            log.exception("worker failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


class TaskRunner:
    """Owns a QThread + Worker pair and keeps them alive for the run.

    Qt deletes a QThread that goes out of scope mid-run, which crashes the
    process; holding a reference here is what prevents that.
    """

    def __init__(self) -> None:
        self._thread: QThread | None = None
        self._worker: Worker | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_progress: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> None:
        if self.busy:
            raise RuntimeError("a task is already running")

        thread = QThread()
        worker = Worker(fn, *args, **kwargs)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        if on_progress:
            worker.progressed.connect(on_progress)

        def _cleanup() -> None:
            thread.quit()
            thread.wait()
            self._thread = None
            self._worker = None

        if on_done:
            worker.finished.connect(on_done)
        if on_error:
            worker.failed.connect(on_error)
        worker.finished.connect(lambda _=None: _cleanup())
        worker.failed.connect(lambda _=None: _cleanup())

        self._thread = thread
        self._worker = worker
        thread.start()

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
