# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for AI Mime — macOS .app bundle
#
# Run from repo root:
#   pyinstaller scripts/pyinstaller.spec --clean
#
# Prerequisites:
#   pip install pyinstaller
#   bash scripts/create_icns.sh   ← produces AppIcon.icns at repo root

import sys  # os is already injected by PyInstaller

# ---------------------------------------------------------------------------
# Paths — SPECPATH is injected by PyInstaller (== dirname of this .spec file).
# ---------------------------------------------------------------------------
_repo = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821
_src = os.path.join(_repo, "src")

# Make ai_mime importable during analysis.
sys.path.insert(0, _src)

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    scripts=[os.path.join(_src, "ai_mime", "cli.py")],
    pathex=[_src],
    binaries=[],
    datas=[
        # user_config.yml → bundle root (sys._MEIPASS root)
        (os.path.join(_repo, "user_config.yml"), "."),
        # Workflow-editor web assets
        (os.path.join(_src, "ai_mime", "editor", "web"), os.path.join("ai_mime", "editor", "web")),
        # Menubar icons (resolved at runtime via get_bundled_resource)
        (os.path.join(_repo, "docs", "logo", "icon32.png"), os.path.join("docs", "logo")),
        (os.path.join(_repo, "docs", "logo", "icon60.png"), os.path.join("docs", "logo")),
    ],
    hiddenimports=[
        # --- Cocoa / AppKit stack -------------------------------------------
        "objc",
        "AppKit",
        "Foundation",
        "Cocoa",
        "Quartz",
        "ApplicationServices",
        "rumps",
        # --- pynput macOS backend (spawned child re-imports) ----------------
        "pynput",
        "pynput._backend",
        "pynput._backend.darwin",
        "pynput._backend.darwin._utils",
        "pynput._backend.darwin.keyboard",
        "pynput._backend.darwin.mouse",
        # --- mss macOS backend ----------------------------------------------
        "mss",
        "mss._darwin",
        # --- Pillow ---------------------------------------------------------
        "PIL",
        "PIL.Image",
        # --- sounddevice ----------------------------------------------------
        "sounddevice",
        # --- ai_mime sub-packages (spawn re-imports everything) -------------
        "ai_mime",
        "ai_mime.app",
        "ai_mime.app_data",
        "ai_mime.cli",
        "ai_mime.onboarding",
        "ai_mime.permissions",
        "ai_mime.user_config",
        "ai_mime.record",
        "ai_mime.record.capture",
        "ai_mime.record.recorder_process",
        "ai_mime.record.storage",
        "ai_mime.record.overlay_ui",
        "ai_mime.reflect",
        "ai_mime.reflect.workflow",
        "ai_mime.reflect.schema_utils",
        "ai_mime.replay",
        "ai_mime.replay.catalog",
        "ai_mime.replay.engine",
        "ai_mime.replay.grounding",
        "ai_mime.replay.os_executor",
        "ai_mime.replay.overlay_ui",
        "ai_mime.screenshot",
        "ai_mime.editor",
        "ai_mime.editor.server",
        # --- LLM / inference ------------------------------------------------
        "litellm",
        "instructor",
        # --- Observability --------------------------------------------------
        "lmnr",
        # --- Editor server (FastAPI + uvicorn) ------------------------------
        "fastapi",
        "uvicorn",
    ],
    excludes=[
        "tkinter",
        "PyQt5",
        "PyQt6",
    ],
    hookspath=[os.path.join(_repo, "hooks")],
    norecursedirs=[],
    debug=[],
    optimize=0,
)

# ---------------------------------------------------------------------------
# Python archive (bytecode zip)
# ---------------------------------------------------------------------------
pyz = PYZ(a.pure, a.zipped_data, debug=False)

# ---------------------------------------------------------------------------
# Executable
# ---------------------------------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    name="ai_mime",
    debug=False,
    exclude_binaries=True,  # onedir mode: EXE goes to workpath; COLLECT assembles dist
    strip=False,
    upx=False,
    console=False,  # GUI / Agent app — no terminal window
    icon=os.path.join(_repo, "AppIcon.icns"),
)

# ---------------------------------------------------------------------------
# Collect (directory mode)
# ---------------------------------------------------------------------------
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ai_mime",
)

# ---------------------------------------------------------------------------
# macOS .app bundle
# ---------------------------------------------------------------------------
app = BUNDLE(
    coll,
    name="AI Mime.app",
    icon=os.path.join(_repo, "AppIcon.icns"),
    bundle_identifier="com.aimime.app",
    info_plist={
        # Agent / menubar-only: no Dock icon, no splash window.
        # "LSUIType": "Agent",
        # "LSBackgroundOnly": "true",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleName": "AI Mime",
        "CFBundleExecutable": "ai_mime",
        "NSAppleScriptEnabled": False,
    },
)
