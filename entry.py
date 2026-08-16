"""Frozen-app entry point.

PyInstaller needs a real script rather than a console_scripts entry point.
Opening the window is the packaged app's only job; the CLI stays available
through `python -m djset.cli` in a development install.
"""

from __future__ import annotations

import sys


def main() -> int:
    from djset.ui import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
