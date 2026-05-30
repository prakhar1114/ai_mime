from __future__ import annotations

import asyncio
import base64
import importlib.machinery
import importlib.util
import json
import sys
from dataclasses import dataclass
from io import BytesIO
from types import ModuleType
from typing import Any

from fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from PIL import Image as PILImage


CUSTOM_TOOL_NAMES = (
    "computer_get_window_state",
    "computer_perform_action_and_get_state",
)


@dataclass(frozen=True)
class CustomToolInstallResult:
    installed: bool
    already_installed: bool
    tool_names: tuple[str, ...] = CUSTOM_TOOL_NAMES


def install_custom_tools() -> CustomToolInstallResult:
    """Install custom MCP tools before ``computer_server.main`` builds /mcp.

    A normal ``import computer_server.mcp_server`` first imports the parent package.
    That parent imports ``Server``, which imports ``computer_server.main``; main then
    creates the FastMCP app immediately. Loading the submodule by spec avoids that
    parent import and lets us patch ``create_mcp_server`` before main binds it.
    """
    mcp_server = _load_mcp_server_module_without_parent_import()
    original_create = getattr(
        mcp_server,
        "_ai_mime_original_create_mcp_server",
        mcp_server.create_mcp_server,
    )
    if getattr(mcp_server.create_mcp_server, "_ai_mime_custom_tools_installed", False):
        return CustomToolInstallResult(installed=True, already_installed=True)

    mcp_server._ai_mime_original_create_mcp_server = original_create

    def custom_create_mcp_server(*args: Any, **kwargs: Any) -> FastMCP:
        mcp = original_create(*args, **kwargs)

        async def _capture_screenshot_base64() -> str:
            _, automation_handler, _, _, _, _ = mcp_server._get_handlers()
            result = await automation_handler.screenshot()
            image_bytes = base64.b64decode(result["image_data"])
            img = PILImage.open(BytesIO(image_bytes))

            if (
                mcp_server._target_width
                and mcp_server._target_height
                and (mcp_server._scale_x != 1.0 or mcp_server._scale_y != 1.0)
            ):
                img = img.resize(
                    (mcp_server._target_width, mcp_server._target_height),
                    PILImage.Resampling.LANCZOS,
                )
            else:
                max_dimension = 1280
                width, height = img.size
                if width > max_dimension or height > max_dimension:
                    if width > height:
                        new_width = max_dimension
                        new_height = int(height * max_dimension / width)
                    else:
                        new_height = max_dimension
                        new_width = int(width * max_dimension / height)
                    img = img.resize((new_width, new_height), PILImage.Resampling.LANCZOS)

                    if not mcp_server._target_width:
                        mcp_server._configure_scaling(
                            target_width=new_width,
                            target_height=new_height,
                        )

            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            buffered = BytesIO()
            img.save(buffered, format="JPEG", quality=85, optimize=True)
            buffered.seek(0)
            data = buffered.getvalue()

            if len(data) > 900000:
                buffered = BytesIO()
                img.save(buffered, format="JPEG", quality=70, optimize=True)
                buffered.seek(0)
                data = buffered.getvalue()

            return base64.b64encode(data).decode("utf-8")

        @mcp.tool
        async def computer_get_window_state() -> list[Any]:
            """
            Combined state read: capture screenshot and accessibility tree together.

            Use this instead of separate ``computer_screenshot`` and
            ``computer_get_accessibility_tree`` calls when inspecting UI state.

            Returns:
                MCP content blocks: an image block and a text block containing AX JSON.
            """
            accessibility_handler, _, _, _, _, _ = mcp_server._get_handlers()
            tree = await accessibility_handler.get_accessibility_tree()
            screenshot_base64 = await _capture_screenshot_base64()

            return [
                ImageContent(type="image", data=screenshot_base64, mimeType="image/jpeg"),
                TextContent(
                    type="text",
                    text=f"Accessibility Tree:\n{json.dumps(tree, indent=2)}",
                ),
            ]

        @mcp.tool
        async def computer_perform_action_and_get_state(
            action_type: str,
            x: int | None = None,
            y: int | None = None,
            button: str = "left",
            text: str | None = None,
            key: str | None = None,
            keys: list[str] | None = None,
            scroll_x: int = 0,
            scroll_y: int = 0,
            post_delay_ms: int = 500,
        ) -> list[Any]:
            """
            Combined action + verification: perform a UI action, wait briefly, then
            return screenshot and accessibility tree.

            Use this instead of a granular action followed by separate screenshot or
            accessibility-tree calls when the action can be represented by these args.

            Args:
                action_type: click, double_click, move, type, press_key, hotkey, or scroll.
                x: X coordinate for pointer actions.
                y: Y coordinate for pointer actions.
                button: Mouse button for clicks.
                text: Text to type.
                key: Key to press.
                keys: Key combination for hotkeys.
                scroll_x: Horizontal scroll amount.
                scroll_y: Vertical scroll amount.
                post_delay_ms: UI settle delay in milliseconds.
            """
            _, automation_handler, _, _, _, _ = mcp_server._get_handlers()

            if action_type == "click":
                if x is None or y is None:
                    raise ValueError("Click requires x and y coordinates")
                ax, ay = mcp_server._scale_to_actual(x, y)
                if button == "right":
                    await automation_handler.right_click(ax, ay)
                else:
                    await automation_handler.left_click(ax, ay)
            elif action_type == "double_click":
                if x is None or y is None:
                    raise ValueError("Double-click requires x and y coordinates")
                ax, ay = mcp_server._scale_to_actual(x, y)
                await automation_handler.double_click(ax, ay)
            elif action_type == "move":
                if x is None or y is None:
                    raise ValueError("Move requires x and y coordinates")
                ax, ay = mcp_server._scale_to_actual(x, y)
                await automation_handler.move_cursor(ax, ay)
            elif action_type == "type":
                if text is None:
                    raise ValueError("Type requires a text string")
                await automation_handler.type_text(text)
            elif action_type == "press_key":
                if key is None:
                    raise ValueError("Press key requires a key name")
                await automation_handler.press_key(key)
            elif action_type == "hotkey":
                if not keys:
                    raise ValueError("Hotkey requires a list of keys")
                await automation_handler.hotkey(keys)
            elif action_type == "scroll":
                if x is not None and y is not None:
                    ax, ay = mcp_server._scale_to_actual(x, y)
                    await automation_handler.move_cursor(ax, ay)
                await automation_handler.scroll(scroll_x, scroll_y)
            else:
                raise ValueError(f"Unknown action_type: {action_type}")

            await asyncio.sleep(post_delay_ms / 1000.0)

            accessibility_handler, _, _, _, _, _ = mcp_server._get_handlers()
            tree = await accessibility_handler.get_accessibility_tree()
            screenshot_base64 = await _capture_screenshot_base64()

            return [
                ImageContent(type="image", data=screenshot_base64, mimeType="image/jpeg"),
                TextContent(
                    type="text",
                    text=f"Accessibility Tree:\n{json.dumps(tree, indent=2)}",
                ),
            ]

        return mcp

    custom_create_mcp_server._ai_mime_custom_tools_installed = True  # type: ignore[attr-defined]
    mcp_server.create_mcp_server = custom_create_mcp_server
    return CustomToolInstallResult(installed=True, already_installed=False)


def _load_mcp_server_module_without_parent_import() -> ModuleType:
    existing = sys.modules.get("computer_server.mcp_server")
    if existing is not None:
        return existing

    package_spec = importlib.machinery.PathFinder.find_spec("computer_server", sys.path)
    if package_spec is None or package_spec.submodule_search_locations is None:
        raise ImportError("Could not locate computer_server package")

    spec = importlib.machinery.PathFinder.find_spec(
        "computer_server.mcp_server",
        list(package_spec.submodule_search_locations),
    )
    if spec is None or spec.loader is None:
        raise ImportError("Could not locate computer_server.mcp_server")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module
