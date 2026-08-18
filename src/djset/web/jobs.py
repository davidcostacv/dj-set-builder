"""Long jobs — sync and enrich — run behind the HTTP layer.

One slot, not a queue. Both jobs write to the same SQLite file, and a full
enrichment pass takes about eight hours; running two at once would mean two
writers contending for the lock and two processes sharing MusicBrainz's one
request per second, which is how you get blocked rather than how you go
faster. A second start is refused with the running job's state rather than
silently queued, so the caller can say what is already happening.

The thread outliving the request is the point. Every attempt to run enrichment
from a shell died when the shell did — four times, three mechanisms. Held by a
server process it simply keeps going, and progress survives regardless because
`enrich_tracks` commits every result as it lands.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

# (progress_callback, cancel_event) -> a short human summary
JobFn = Callable[[Callable[[int, int, str], None], threading.Event], str]


@dataclass
class JobState:
    kind: str = ""
    running: bool = False
    done: int = 0
    total: int = 0
    label: str = ""
    started_at: float = 0.0
    finished_at: float | None = None
    error: str | None = None
    summary: str | None = None
    cancelled: bool = False

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return end - self.started_at if self.started_at else 0.0

    @property
    def eta_seconds(self) -> float | None:
        """Seconds remaining at the rate observed so far.

        Worth showing precisely because these jobs are long: without it, an
        eight-hour pass and a stuck one look identical.
        """
        if not self.running or self.done <= 0 or self.total <= 0:
            return None
        rate = self.done / self.elapsed if self.elapsed > 0 else 0
        return (self.total - self.done) / rate if rate > 0 else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "running": self.running,
            "done": self.done,
            "total": self.total,
            "label": self.label,
            "elapsed": round(self.elapsed, 1),
            "eta_seconds": round(e) if (e := self.eta_seconds) is not None else None,
            "error": self.error,
            "summary": self.summary,
            "cancelled": self.cancelled,
            "percent": round(100 * self.done / self.total, 1) if self.total else 0.0,
        }


class JobRunner:
    """One job at a time, observable while it runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.state = JobState()
        self.on_finished: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, kind: str, fn: JobFn) -> tuple[bool, JobState]:
        """Begin a job. Returns ``(started, state)``.

        ``started`` is False when one is already running — the caller gets the
        running job's state so it can say which, rather than a bare refusal.
        """
        with self._lock:
            if self.busy:
                return False, self.state

            self._cancel = threading.Event()
            self.state = JobState(kind=kind, running=True, started_at=time.time())

            def progress(done: int, total: int, label: str = "") -> None:
                self.state.done = done
                self.state.total = total
                if label:
                    self.state.label = label

            def run() -> None:
                try:
                    summary = fn(progress, self._cancel)
                    self.state.summary = summary
                except Exception as exc:  # a failed job must not kill the server
                    log.exception("job %s failed", kind)
                    self.state.error = f"{type(exc).__name__}: {exc}"
                finally:
                    self.state.running = False
                    self.state.finished_at = time.time()
                    self.state.cancelled = self._cancel.is_set()
                    if self.on_finished is not None:
                        try:
                            self.on_finished()
                        except Exception:
                            log.exception("post-job reload failed")

            self._thread = threading.Thread(target=run, name=f"djset-{kind}", daemon=True)
            self._thread.start()
            return True, self.state

    def cancel(self) -> bool:
        """Ask the job to stop. Cooperative: it stops at the next checkpoint."""
        if not self.busy:
            return False
        self._cancel.set()
        return True


runner = JobRunner()
