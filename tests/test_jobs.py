"""One long job at a time, observable while it runs — step W4.

Sync and enrich both write to the same SQLite file, and a full enrichment pass
takes about eight hours. The point of running them here is that the thread
outlives the request: every attempt to run enrichment from a shell died when
the shell did.
"""

from __future__ import annotations

import threading
import time

import pytest

from djset.web.jobs import JobRunner


def _blocker(release: threading.Event):
    """A job that waits, so the test can observe it mid-flight."""
    def work(progress, cancel):
        progress(1, 10, "first")
        release.wait(timeout=5)
        return "finished"
    return work


def test_a_job_reports_progress_while_it_runs():
    runner = JobRunner()
    release = threading.Event()
    started, _ = runner.start("demo", _blocker(release))
    assert started

    for _ in range(50):                      # let the thread reach progress()
        if runner.state.done == 1:
            break
        time.sleep(0.02)

    assert runner.state.running is True
    assert (runner.state.done, runner.state.total) == (1, 10)
    assert runner.state.label == "first"
    assert runner.state.as_dict()["percent"] == 10.0

    release.set()
    for _ in range(50):
        if not runner.busy:
            break
        time.sleep(0.02)
    assert runner.state.summary == "finished"
    assert runner.state.running is False


def test_a_second_job_is_refused_with_the_running_one_s_state():
    """Not queued. Two writers on one SQLite file, and two clients sharing
    MusicBrainz's one request per second, is how you get blocked."""
    runner = JobRunner()
    release = threading.Event()
    runner.start("enrich", _blocker(release))

    started, state = runner.start("sync", lambda p, c: "never")
    assert started is False
    assert state.kind == "enrich"          # says which, rather than just "no"

    release.set()


def test_a_job_can_run_once_the_previous_one_is_done():
    runner = JobRunner()
    runner.start("first", lambda p, c: "a")
    for _ in range(50):
        if not runner.busy:
            break
        time.sleep(0.02)

    started, _ = runner.start("second", lambda p, c: "b")
    assert started is True


def test_cancelling_is_cooperative_and_recorded():
    runner = JobRunner()
    seen = []

    def work(progress, cancel):
        for i in range(200):
            if cancel.is_set():
                return f"stopped at {i}"
            progress(i, 200, "")
            seen.append(i)
            time.sleep(0.01)
        return "ran to the end"

    runner.start("enrich", work)
    for _ in range(50):
        if runner.state.done > 2:
            break
        time.sleep(0.02)

    assert runner.cancel() is True
    for _ in range(100):
        if not runner.busy:
            break
        time.sleep(0.02)

    assert runner.state.cancelled is True
    assert runner.state.summary.startswith("stopped at")
    assert len(seen) < 200                 # it really did stop early


def test_cancelling_nothing_is_not_an_error():
    assert JobRunner().cancel() is False


def test_a_failing_job_is_recorded_rather_than_crashing_the_server():
    runner = JobRunner()
    runner.start("enrich", lambda p, c: (_ for _ in ()).throw(RuntimeError("boom")))
    for _ in range(50):
        if not runner.busy:
            break
        time.sleep(0.02)

    assert runner.state.running is False
    assert "RuntimeError: boom" in runner.state.error
    assert runner.state.summary is None


def test_the_library_is_reloaded_once_a_job_finishes():
    """A finished job has moved the database on; a held library would go stale."""
    runner = JobRunner()
    reloads = []
    runner.on_finished = lambda: reloads.append(1)

    runner.start("sync", lambda p, c: "done")
    for _ in range(50):
        if not runner.busy:
            break
        time.sleep(0.02)
    assert reloads == [1]


def test_a_broken_reload_does_not_lose_the_job_result():
    runner = JobRunner()
    runner.on_finished = lambda: (_ for _ in ()).throw(RuntimeError("reload failed"))

    runner.start("sync", lambda p, c: "the work itself was fine")
    for _ in range(50):
        if not runner.busy:
            break
        time.sleep(0.02)
    assert runner.state.summary == "the work itself was fine"
    assert runner.state.error is None


def test_an_eta_appears_once_there_is_a_rate_to_measure():
    """These jobs run for hours; without an ETA a long pass and a stuck one
    look identical."""
    runner = JobRunner()
    assert runner.state.as_dict()["eta_seconds"] is None   # idle

    release = threading.Event()
    runner.start("enrich", _blocker(release))
    for _ in range(50):
        if runner.state.done == 1:
            break
        time.sleep(0.02)
    time.sleep(0.1)                                        # let elapsed grow

    eta = runner.state.eta_seconds
    assert eta is not None and eta > 0
    release.set()
