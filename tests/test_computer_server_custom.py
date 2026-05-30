from __future__ import annotations

import asyncio
import base64
import sys
import types
import unittest
from io import BytesIO
from unittest.mock import patch

from PIL import Image

from ai_mime.computer_server_custom import install_custom_tools


class FakeFastMCP:
    def __init__(self, name: str = "original", instructions: str = "") -> None:
        self.name = name
        self.instructions = instructions
        self.registered_tools = {}

    def tool(self, func):
        self.registered_tools[func.__name__] = func
        return func


def _jpeg_b64() -> str:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="JPEG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _fake_mcp_modules():
    fake_mcp_server = types.ModuleType("computer_server.mcp_server")
    fake_package = types.ModuleType("computer_server")
    fake_package.__path__ = []
    fake_package.mcp_server = fake_mcp_server

    class Accessibility:
        async def get_accessibility_tree(self):
            return {"success": True, "windows": [{"id": 1, "name": "Fake"}]}

    class Automation:
        def __init__(self) -> None:
            self.actions: list[tuple[str, tuple]] = []

        async def screenshot(self):
            return {"image_data": _jpeg_b64()}

        async def left_click(self, x, y):
            self.actions.append(("left_click", (x, y)))
            return {"success": True}

        async def type_text(self, text):
            self.actions.append(("type_text", (text,)))
            return {"success": True}

    automation = Automation()

    def create_mcp_server() -> FakeFastMCP:
        return FakeFastMCP()

    fake_mcp_server.create_mcp_server = create_mcp_server
    fake_mcp_server._get_handlers = lambda: (
        Accessibility(),
        automation,
        object(),
        object(),
        object(),
        object(),
    )
    fake_mcp_server._target_width = None
    fake_mcp_server._target_height = None
    fake_mcp_server._scale_x = 1.0
    fake_mcp_server._scale_y = 1.0
    fake_mcp_server._configure_scaling = lambda **kwargs: None
    fake_mcp_server._scale_to_actual = lambda x, y: (x, y)
    return fake_package, fake_mcp_server, automation


class TestComputerServerCustom(unittest.TestCase):
    def test_install_custom_tools_registers_mcp_tools(self) -> None:
        fake_package, fake_mcp_server, _ = _fake_mcp_modules()

        with patch.dict(
            sys.modules,
            {"computer_server": fake_package, "computer_server.mcp_server": fake_mcp_server},
        ):
            result = install_custom_tools()
            server = fake_mcp_server.create_mcp_server()

        self.assertTrue(result.installed)
        self.assertFalse(result.already_installed)
        self.assertIn("computer_get_window_state", server.registered_tools)
        self.assertIn("computer_perform_action_and_get_state", server.registered_tools)

    def test_install_custom_tools_is_idempotent(self) -> None:
        fake_package, fake_mcp_server, _ = _fake_mcp_modules()

        with patch.dict(
            sys.modules,
            {"computer_server": fake_package, "computer_server.mcp_server": fake_mcp_server},
        ):
            first = install_custom_tools()
            wrapped = fake_mcp_server.create_mcp_server
            second = install_custom_tools()

        self.assertTrue(first.installed)
        self.assertTrue(second.already_installed)
        self.assertIs(fake_mcp_server.create_mcp_server, wrapped)

    def test_import_order_uses_wrapped_create_for_main_import(self) -> None:
        fake_package, fake_mcp_server, _ = _fake_mcp_modules()
        fake_main = types.ModuleType("computer_server.main")

        with patch.dict(
            sys.modules,
            {"computer_server": fake_package, "computer_server.mcp_server": fake_mcp_server},
        ):
            install_custom_tools()
            # Simulate computer_server.main's `from .mcp_server import create_mcp_server`.
            fake_main.create_mcp_server = sys.modules[
                "computer_server.mcp_server"
            ].create_mcp_server
            server = fake_main.create_mcp_server()

        self.assertIn("computer_get_window_state", server.registered_tools)
        self.assertIn("computer_perform_action_and_get_state", server.registered_tools)

    def test_get_window_state_returns_image_and_accessibility_text(self) -> None:
        fake_package, fake_mcp_server, _ = _fake_mcp_modules()

        with patch.dict(
            sys.modules,
            {"computer_server": fake_package, "computer_server.mcp_server": fake_mcp_server},
        ):
            install_custom_tools()
            server = fake_mcp_server.create_mcp_server()
            result = asyncio.run(server.registered_tools["computer_get_window_state"]())

        self.assertEqual([item.type for item in result], ["image", "text"])
        self.assertEqual(result[0].mimeType, "image/jpeg")
        self.assertIn("Accessibility Tree:", result[1].text)
        self.assertIn('"name": "Fake"', result[1].text)

    def test_perform_action_and_get_state_executes_action_and_returns_state(self) -> None:
        fake_package, fake_mcp_server, automation = _fake_mcp_modules()

        with patch.dict(
            sys.modules,
            {"computer_server": fake_package, "computer_server.mcp_server": fake_mcp_server},
        ):
            install_custom_tools()
            server = fake_mcp_server.create_mcp_server()
            result = asyncio.run(
                server.registered_tools["computer_perform_action_and_get_state"](
                    action_type="type",
                    text="hello",
                    post_delay_ms=0,
                )
            )

        self.assertEqual(automation.actions, [("type_text", ("hello",))])
        self.assertEqual([item.type for item in result], ["image", "text"])

    def test_invalid_action_arguments_raise_tool_error(self) -> None:
        fake_package, fake_mcp_server, _ = _fake_mcp_modules()

        with patch.dict(
            sys.modules,
            {"computer_server": fake_package, "computer_server.mcp_server": fake_mcp_server},
        ):
            install_custom_tools()
            server = fake_mcp_server.create_mcp_server()
            with self.assertRaises(ValueError):
                asyncio.run(
                    server.registered_tools["computer_perform_action_and_get_state"](
                        action_type="click",
                        post_delay_ms=0,
                    )
                )


if __name__ == "__main__":
    unittest.main()
