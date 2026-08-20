"""Web interface — the browser front end and the HTTP layer beneath it.

Imported lazily by the CLI so the rest of the app stays usable without
fastapi installed.
"""

from __future__ import annotations


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

    uvicorn.run(
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
    return 0


def _app():
    from .app import app

    return app
