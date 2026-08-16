"""PySide6 UI. Imported lazily so the headless CLI never needs Qt installed."""

from __future__ import annotations

import sys


def run() -> int:
    from PySide6.QtWidgets import QApplication

    from ..logging_setup import setup_logging
    from .main_window import MainWindow

    setup_logging()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("djset")
    window = MainWindow()
    window.show()
    return app.exec()


__all__ = ["run"]
