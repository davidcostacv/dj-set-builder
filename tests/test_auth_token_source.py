"""Where a refresh token lives — step W6.

A container has no OS keyring, so the store that works on a laptop leaves a
server unable to hold a login at all. The environment is the way out: the
hosting platform's secret store handles encryption at rest, which this app has
always insisted on and would otherwise have had to reimplement.
"""

from __future__ import annotations

import pytest

from djset.spotify.auth import TokenStore


def test_the_environment_supplies_a_token_where_there_is_no_keyring(monkeypatch):
    monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "  from-the-platform  ")
    store = TokenStore()

    assert store.env_supplied is True
    assert store.load_refresh() == "from-the-platform"   # trimmed


def test_the_environment_wins_over_the_keyring(monkeypatch):
    monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "from-the-platform")
    monkeypatch.setattr(
        "djset.spotify.auth.keyring.get_password", lambda *a: "from-the-keyring"
    )
    assert TokenStore().load_refresh() == "from-the-platform"


def test_an_empty_environment_value_is_not_a_token(monkeypatch):
    """Platforms hand back "" for an unset secret, which must not read as one."""
    monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "   ")
    monkeypatch.setattr(
        "djset.spotify.auth.keyring.get_password", lambda *a: "from-the-keyring"
    )
    store = TokenStore()
    assert store.env_supplied is False
    assert store.load_refresh() == "from-the-keyring"


def test_saving_over_an_env_token_warns_rather_than_pretending(monkeypatch, caplog):
    """There is nothing to write to. The operator must not believe a fresh
    authorization survived a restart when it did not."""
    monkeypatch.setenv("SPOTIFY_REFRESH_TOKEN", "from-the-platform")
    wrote = []
    monkeypatch.setattr(
        "djset.spotify.auth.keyring.set_password",
        lambda *a: wrote.append(a),
    )

    with caplog.at_level("WARNING"):
        TokenStore().save_refresh("a-newer-token")

    assert wrote == []                                   # nothing written
    assert "not persisted" in caplog.text


def test_a_missing_keyring_names_the_way_out(monkeypatch):
    """The error a server hits must say what to do about it."""
    from keyring.errors import KeyringError

    monkeypatch.delenv("SPOTIFY_REFRESH_TOKEN", raising=False)

    def no_backend(*a):
        raise KeyringError("no backend")

    monkeypatch.setattr("djset.spotify.auth.keyring.get_password", no_backend)

    from djset.spotify.auth import AuthError

    with pytest.raises(AuthError) as exc:
        TokenStore().load_refresh()
    assert "SPOTIFY_REFRESH_TOKEN" in str(exc.value)
    assert "djset token" in str(exc.value)
