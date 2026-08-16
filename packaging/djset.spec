# PyInstaller spec — one-folder Windows build of the desktop app.
#
# Build with:   pyinstaller packaging/djset.spec --noconfirm
# Output:       dist/djset/djset.exe
#
# One-folder rather than one-file: a one-file build unpacks Qt to a temp
# directory on every launch, which is slow and trips some antivirus. The app
# already writes its database to the OS app-data directory, so the program
# folder stays read-only and portable.

import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Paths in a spec resolve relative to the spec file, not the working directory,
# so anchor everything on the project root explicitly.
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 (PyInstaller global)

hiddenimports = [
    # keyring resolves its backend at runtime, so PyInstaller cannot see it.
    *collect_submodules("keyring.backends"),
    "win32ctypes.core",       # keyring's Windows backend
    "truststore",             # OS trust store, loaded lazily in djset.net
]

a = Analysis(
    [os.path.join(ROOT, "entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=[
        # schema.sql is read at runtime by djset.db, not imported.
        (os.path.join(ROOT, "src", "djset", "schema.sql"), "djset"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Qt ships far more than this app uses; dropping the unused modules keeps
    # the bundle to a sane size.
    excludes=[
        # This machine also has PyQt6 installed. PyInstaller aborts if it sees
        # two Qt bindings, so every binding except PySide6 is excluded here
        # rather than requiring a pristine build environment.
        "PyQt6", "PyQt5", "PySide2", "qtpy",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts",
        "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtBluetooth",
        "tkinter", "matplotlib", "numpy", "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="djset",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # no console window for a GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="djset",
)
