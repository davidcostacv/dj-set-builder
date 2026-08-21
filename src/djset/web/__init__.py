"""Web interface — the browser front end and the HTTP layer beneath it.

Imported lazily by the CLI so the rest of the app stays usable without
fastapi installed.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = False,
    proxy_headers: bool = False,
    forwarded_allow_ips: str = "127.0.0.1",
) -> int:
    """Run the server.

    ``proxy_headers`` matters more than it looks. Behind a load balancer that
    terminates TLS, the app sees a plain-http request to an internal address —
    so it builds ``http://0.0.0.0:8000/auth/callback`` as its redirect URI and
    Spotify rejects it, with an error that does not say why. Honouring
    X-Forwarded-Proto and X-Forwarded-Host makes the derived URI the public one.

    It is off by default because those headers are trivially forged by whoever
    can reach the socket. Enable it only when something you trust sits in
    front, and set ``forwarded_allow_ips`` to that something.
    """
    import uvicorn

    sockets = _loopback_sockets(port) if _is_loopback(host) else None
    config = uvicorn.Config(
        "djset.web.app:app" if reload else _app(),
        host=host,
        port=port,
        reload=reload,
        log_level="info",
        proxy_headers=proxy_headers,
        forwarded_allow_ips=forwarded_allow_ips,
        # One worker, deliberately. The job runner holds a single slot in
        # process memory and the pending OAuth flows live there too; a second
        # worker would run a second enrichment against the same SQLite file and
        # lose every callback that landed on the other process.
        workers=1,
    )
    if sockets is not None and not reload:
        uvicorn.Server(config).run(sockets=sockets)
    else:
        # --reload re-executes this module in a child process, which cannot
        # inherit sockets opened here.
        uvicorn.Server(config).run()
    return 0


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


def _loopback_sockets(port: int):
    """Listen on IPv4 *and* IPv6 loopback, or None to let uvicorn bind.

    Windows resolves ``localhost`` to ``::1`` before ``127.0.0.1``. Bound to
    one stack only, the app answers ``http://127.0.0.1:8000`` and refuses
    ``http://localhost:8000`` — and browsers retry the other family on a
    timer, so it works *sometimes*, which is worse to diagnose than never.
    curl hides it too, because it falls back immediately.

    Two sockets rather than a dual-stack wildcard: ``::`` with IPV6_V6ONLY off
    would take both families, but it also listens on every interface, and a
    local app has no business being reachable from the network.
    """
    import socket

    opened = []
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            sock.bind((addr, port))
            sock.listen(2048)
            sock.set_inheritable(True)
            opened.append(sock)
        except OSError as exc:
            # A host without IPv6 is fine; losing IPv4 is not.
            if family == socket.AF_INET:
                for s in opened:
                    s.close()
                raise
            log.debug("no IPv6 loopback (%s); IPv4 only", exc)
    return opened or None


def _app():
    from .app import app

    return app
