# PyInstaller spec: one directory, three executables.
#
#     pyinstaller packaging/idmclone.spec --noconfirm
#
# Why three:
#   IDMClone.exe       windowed GUI - the one users launch
#   idmclone-cli.exe   console CLI, and the only way to register the browser
#                      host on a machine with no Python. The name has to differ
#                      from IDMClone.exe by more than case: Windows file names
#                      are case-insensitive, so "idmclone.exe" would silently
#                      overwrite the GUI in the output directory
#   idmclone-host.exe  console native-messaging host; Chrome speaks stdio, so
#                      it can never be the windowed exe
#
# One `COLLECT` means they share a single copy of Qt (~40 MB) instead of three.
# One-dir, not one-file: a one-file build unpacks to %TEMP% on every launch,
# which costs seconds of startup and reliably upsets antivirus heuristics.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_ROOT = SPEC_DIR.parent
ICON = SPEC_DIR / "idmclone.ico"

# yt-dlp loads its thousand extractors by name, so nothing static can see them.
HIDDEN = collect_submodules("yt_dlp.extractor") + [
    "yt_dlp",
    "cryptography.hazmat.primitives.ciphers",
]

# Qt modules the download manager never touches. Dropping them roughly halves
# the build; PySide6-Essentials has already left out the really big ones.
EXCLUDES = [
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtOpenGL", "PySide6.QtQml",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtSerialPort", "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "tkinter", "matplotlib", "numpy", "pytest", "PIL",
]

DATAS = [
    (str(PROJECT_ROOT / "extension"), "extension"),
    (str(PROJECT_ROOT / "README.md"), "."),
]

# Files PySide6 ships that a widgets-only application never opens. Together
# they are a quarter of the build.
BLOAT = (
    # Mesa's software OpenGL: only loaded by QtQuick / QOpenGLWidget.
    "opengl32sw.dll",
    # Qt's own .qm catalogues - our strings live in app/ui/i18n.py, and the
    # file/print dialogs we use are the native Windows ones.
    "pyside6/translations",
    "qt6/translations",
)


def trim(entries):
    """Drop `BLOAT` from a TOC, keeping PyInstaller's tuple shape."""
    kept = []
    for entry in entries:
        name = str(entry[0]).replace("\\", "/").lower()
        if any(pattern in name for pattern in BLOAT):
            continue
        kept.append(entry)
    if len(kept) != len(entries):
        print(f"[idmclone.spec] dropped {len(entries) - len(kept)} unused Qt files")
    return kept


def analysis(script: str) -> Analysis:
    return Analysis(
        [str(SPEC_DIR / script)],
        pathex=[str(PROJECT_ROOT)],
        binaries=[],
        datas=DATAS,
        hiddenimports=HIDDEN,
        hookspath=[],
        runtime_hooks=[],
        excludes=EXCLUDES,
        noarchive=False,
    )


gui_a = analysis("entry_gui.py")
cli_a = analysis("entry_cli.py")
host_a = analysis("entry_host.py")

# MERGE would let the CLI and host reuse the GUI's archive, but it makes them
# depend on IDMClone.exe being started first - not true for the host, which
# Chrome starts on its own. Three PYZ files cost a few MB and always work.
gui_pyz = PYZ(gui_a.pure, gui_a.zipped_data)
cli_pyz = PYZ(cli_a.pure, cli_a.zipped_data)
host_pyz = PYZ(host_a.pure, host_a.zipped_data)

gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name="IDMClone",
    console=False,          # windowed: no console flash when the tray starts it
    icon=str(ICON),
    disable_windowed_traceback=False,
)

cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    [],
    exclude_binaries=True,
    name="idmclone-cli",
    console=True,
    icon=str(ICON),
)

host_exe = EXE(
    host_pyz,
    host_a.scripts,
    [],
    exclude_binaries=True,
    name="idmclone-host",
    console=True,           # native messaging *is* stdio
    icon=str(ICON),
)

COLLECT(
    gui_exe, trim(gui_a.binaries), trim(gui_a.datas),
    cli_exe, trim(cli_a.binaries), trim(cli_a.datas),
    host_exe, trim(host_a.binaries), trim(host_a.datas),
    strip=False,
    upx=False,              # UPX-packed exes are what most AV engines flag
    name="IDMClone",
)
