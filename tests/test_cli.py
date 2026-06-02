from __future__ import annotations

import json
import unittest

from ai_mime.cli import _mcp_initialize_response_looks_valid


class CliComputerServerReadinessTests(unittest.TestCase):
    def test_mcp_initialize_response_accepts_json_rpc_result(self) -> None:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
            }
        ).encode("utf-8")

        self.assertTrue(_mcp_initialize_response_looks_valid(body))

    def test_mcp_initialize_response_accepts_sse_json_rpc_result(self) -> None:
        body = (
            'event: message\n'
            'data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26"}}\n'
        ).encode("utf-8")

        self.assertTrue(_mcp_initialize_response_looks_valid(body))

    def test_mcp_initialize_response_rejects_non_mcp_json(self) -> None:
        self.assertFalse(_mcp_initialize_response_looks_valid(b'{"detail":"Not Found"}'))


if __name__ == "__main__":
    unittest.main()
