"""429 handling.

Regression: Spotify answered an over-quota artist pass with
``Retry-After: 85890`` (23.9 hours) and the retry wrapper slept on it. A CLI run
hung; in the UI it would have frozen a worker thread for a day. A Retry-After
longer than the cap is an answer, not a hiccup — report it, don't wait it out.
"""

from __future__ import annotations

import httpx
import pytest

from djset import net
from djset.net import MAX_RETRY_SLEEP_S, HttpError, RateLimited, RateLimiter


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, method, url, **kw):
        self.calls += 1
        r = self.responses.pop(0) if self.responses else self.responses
        if isinstance(r, Exception):
            raise r
        return r


def _resp(status, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return httpx.Response(status, headers=headers, text="{}",
                          request=httpx.Request("GET", "https://x.test/a"))


@pytest.fixture()
def no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(net.time, "sleep", lambda s: slept.append(s))
    return slept


def _install(monkeypatch, fake):
    monkeypatch.setattr(net, "client", lambda: fake)


def test_a_long_retry_after_raises_instead_of_sleeping(monkeypatch, no_sleep):
    fake = FakeClient([_resp(429, retry_after=85890)])
    _install(monkeypatch, fake)

    with pytest.raises(RateLimited) as exc:
        net.request("GET", "https://x.test/a")

    assert exc.value.retry_after == 85890
    assert "23.9 hours" in exc.value.human_delay()
    assert no_sleep == []  # never blocked
    assert fake.calls == 1  # and never retried into the same wall


def test_a_short_retry_after_is_still_honoured(monkeypatch, no_sleep):
    fake = FakeClient([_resp(429, retry_after=2), _resp(200)])
    _install(monkeypatch, fake)

    r = net.request("GET", "https://x.test/a")
    assert r.status_code == 200
    assert no_sleep and no_sleep[0] == pytest.approx(3.0)  # 2 + 1s margin


def test_the_cap_is_the_boundary(monkeypatch, no_sleep):
    _install(monkeypatch, FakeClient([_resp(429, retry_after=MAX_RETRY_SLEEP_S), _resp(200)]))
    assert net.request("GET", "https://x.test/a").status_code == 200

    _install(monkeypatch, FakeClient([_resp(429, retry_after=MAX_RETRY_SLEEP_S + 1)]))
    with pytest.raises(RateLimited):
        net.request("GET", "https://x.test/a")


def test_429_without_a_retry_after_uses_backoff(monkeypatch, no_sleep):
    _install(monkeypatch, FakeClient([_resp(429), _resp(200)]))
    assert net.request("GET", "https://x.test/a").status_code == 200
    assert len(no_sleep) == 1 and no_sleep[0] < 5


def test_rate_limited_message_is_reassuring_and_actionable():
    exc = RateLimited("https://api.spotify.com/v1/artists/x", 7200)
    msg = str(exc)
    assert "2.0 hours" in msg
    assert "resumes" in msg  # tells the user nothing was lost
    assert isinstance(exc, HttpError)  # existing handlers still catch it


def test_5xx_still_retries_normally(monkeypatch, no_sleep):
    _install(monkeypatch, FakeClient([_resp(503), _resp(200)]))
    assert net.request("GET", "https://x.test/a").status_code == 200


# ---------------------------------------------------------------------------


def test_rate_limiter_spaces_calls(monkeypatch):
    limiter = RateLimiter(per_hour=3600)  # 1/second
    assert limiter.min_interval == pytest.approx(1.0)

    slept: list[float] = []
    monkeypatch.setattr(net.time, "sleep", lambda s: slept.append(s))
    for _ in range(3):
        limiter.wait()
    # First call is free; subsequent ones are spaced.
    assert len([s for s in slept if s > 0]) >= 1


def test_rate_limiter_floor():
    assert RateLimiter(per_hour=0).min_interval > 0
