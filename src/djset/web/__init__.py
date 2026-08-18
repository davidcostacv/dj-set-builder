"""Web interface — the browser front end and the HTTP layer beneath it.

Imported lazily by the CLI so the rest of the app stays usable without
fastapi installed.
"""

from __future__ import annotations


def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> int:
    import uvicorn

    uvicorn.run(
        "djset.web.app:app" if reload else _app(),
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
    return 0


def _app():
    from .app import app

    return app
