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

from PyInstaller.utils.hooks import copy_metadata

# ---------------------------------------------------------------------------
# Paths — SPECPATH is injected by PyInstaller (== dirname of this .spec file).
# ---------------------------------------------------------------------------
_repo = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821
_src = os.path.join(_repo, "src")
_uv_binary = os.environ.get("UV_BINARY_PATH")
if not _uv_binary or not os.path.isfile(_uv_binary):  # noqa: F821
    raise RuntimeError(
        "UV_BINARY_PATH must point to the uv binary. "
        "Run scripts/build.sh so it can resolve and export uv."
    )

# Make ai_mime importable during analysis.
sys.path.insert(0, _src)

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    scripts=[os.path.join(_src, "ai_mime", "cli.py")],
    pathex=[_src],
    binaries=[
        (_uv_binary, "bin"),
    ],
    datas=[
        # user_config.yml → bundle root (sys._MEIPASS root)
        (os.path.join(_repo, "user_config.yml"), "."),
        # Workflow-editor web assets
        (os.path.join(_src, "ai_mime", "editor", "web"), os.path.join("ai_mime", "editor", "web")),
        # Bundled browser-harness skill, linked into ~/.claude/skills during onboarding
        (os.path.join(_repo, "harness", "browser-harness"), os.path.join("harness", "browser-harness")),
        # Local packages needed by app-managed uv tool installs.
        (os.path.join(_repo, "packages", "llm-resolver"), os.path.join("packages", "llm-resolver")),
        # Menubar icons (resolved at runtime via get_bundled_resource)
        (os.path.join(_repo, "docs", "logo", "icon32.png"), os.path.join("docs", "logo")),
        (os.path.join(_repo, "docs", "logo", "icon60.png"), os.path.join("docs", "logo")),
        # Agent runner instructions (containing build_skill, replay, and example_skill)
        (os.path.join(_src, "ai_mime", "agent_runner", "instructions"), os.path.join("ai_mime", "agent_runner", "instructions")),
    ]
    + copy_metadata("fastmcp")
    + copy_metadata("mcp")
    + copy_metadata("cua-computer-server"),
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
        "ai_mime.reflect.schema_compiler",
        "ai_mime.reflect.runner",
        "ai_mime.screenshot",
        "ai_mime.editor",
        "ai_mime.editor.server",
        "ai_mime.overlay",
        "ai_mime.overlay.conversation_overlay",
        "ai_mime.overlay.ui_common",
        "ai_mime.agent_runner",
        "ai_mime.agent_runner.models",
        "ai_mime.agent_runner.runner",
        "ai_mime.agent_runner.chat",
        "ai_mime.agent_runner.skill_build_chat",
        "ai_mime.agent_runner.computer_use",
        "ai_mime.agent_runner.adapters",
        "ai_mime.agent_runner.adapters.claude_sdk",
        "ai_mime.agent_runner.adapters.codex_cli",
        "ai_mime.computer_server_custom",
        # --- CUA computer server / MCP ----------------------------------------
        "computer_server",
        "computer_server.main",
        "computer_server.mcp_server",
        "fastmcp",
        "mcp",
        "mcp.client",
        "mcp.client.session",
        "mcp.client.streamable_http",
        "mcp.types",
        # --- LLM / inference ------------------------------------------------
        "llm_resolver",
        "llm_resolver.codex",
        "litellm",
        "openai",
        "openai_codex",
        "openai_codex.types",
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
