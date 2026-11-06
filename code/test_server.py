import json
import os
import unittest
from unittest.mock import Mock, patch

from starlette.testclient import TestClient

import server
from connector import ConnectorError


class MCPServerTests(unittest.TestCase):
    def test_server_uses_configured_bind_host(self):
        self.assertEqual(server.mcp.settings.host, os.environ.get("MCP_BIND_HOST", "127.0.0.1"))

    @classmethod
    def setUpClass(cls):
        cls.http = TestClient(server.mcp.streamable_http_app(), base_url="http://127.0.0.1:8000")
        cls.http.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.http.__exit__(None, None, None)

    def setUp(self):
        self.upstream = Mock()
        self.upstream.available.return_value = True
        self.client_patch = patch.object(server, "client", self.upstream)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.headers = {"Accept": "application/json, text/event-stream"}
        response = self.rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        })
        self.assertEqual(response["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(response["result"]["serverInfo"]["name"], "SilverBullet")
        self.assertIn("tools", response["result"]["capabilities"])
        self.headers["MCP-Protocol-Version"] = response["result"]["protocolVersion"]
        notification = self.http.post("/mcp", headers=self.headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        self.assertEqual(notification.status_code, 202)

    def rpc(self, method, params=None):
        response = self.http.post("/mcp", headers=self.headers, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
        })
        self.assertEqual(response.status_code, 200, response.text)
        if response.headers["content-type"].startswith("text/event-stream"):
            data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
            self.assertEqual(len(data), 1, response.text)
            result = json.loads(data[0])
        else:
            result = response.json()
        self.assertEqual(result["jsonrpc"], "2.0")
        self.assertEqual(result["id"], 1)
        return result

    def call_tool(self, name, arguments=None):
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}})

    def test_list_tools_contract_and_no_generic_tool(self):
        result = self.rpc("tools/list")["result"]
        tools = {tool["name"]: tool for tool in result["tools"]}
        self.assertEqual(set(tools), {"listar_paginas", "leer_pagina", "buscar", "tareas_pendientes", "resumen",
                                      "buscar_por_clave", "notas_relacionadas", "tareas_de_entidad", "crear_pagina",
                                      "leer_pagina_con_revision", "actualizar_pagina"})
        for name, tool in tools.items():
            if name == "crear_pagina":
                self.assertFalse(tool["annotations"]["readOnlyHint"])
                self.assertFalse(tool["annotations"]["destructiveHint"])
                self.assertFalse(tool["annotations"]["idempotentHint"])
                self.assertEqual(tool["inputSchema"]["required"], ["title", "content"])
                self.assertIn("metadata", tool["inputSchema"]["properties"])
                continue
            if name == "actualizar_pagina":
                self.assertFalse(tool["annotations"]["readOnlyHint"])
                self.assertTrue(tool["annotations"]["destructiveHint"])
                self.assertEqual(tool["inputSchema"]["required"], ["name", "content", "revision"])
                continue
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
            self.assertEqual(tool["inputSchema"]["type"], "object")
        self.assertEqual(tools["leer_pagina"]["inputSchema"]["required"], ["name"])
        self.assertEqual(tools["buscar"]["inputSchema"]["required"], ["term"])
        self.upstream.assert_not_called()
        self.upstream.available.assert_not_called()

    def test_read_revision_and_update_tools(self):
        self.upstream.page.return_value = {"name": "A", "content": "old", "revision": '"abc"'}
        self.upstream.update_page.return_value = "A"
        result = self.call_tool("leer_pagina_con_revision", {"name": "A"})["result"]
        self.assertEqual(json.loads(result["content"][0]["text"]), self.upstream.page.return_value)
        self.upstream.page.assert_called_once_with("A", with_revision=True)
        result = self.call_tool("actualizar_pagina", {"name": "A", "content": "new", "revision": '"abc"'})["result"]
        self.assertEqual(json.loads(result["content"][0]["text"]), {"name": "A"})
        self.upstream.update_page.assert_called_once_with("A", "new", '"abc"')
        self.upstream.update_page.side_effect = ConnectorError("conflict")
        result = self.call_tool("actualizar_pagina", {"name": "A", "content": "new", "revision": '"abc"'})["result"]
        self.assertTrue(result["isError"])
        self.upstream.available.assert_not_called()

    def test_create_page_tool(self):
        title, content = 'Nota "é"', 'texto\n---\ndraft: false'
        self.upstream.create_page.return_value = title
        result = self.call_tool("crear_pagina", {"title": title, "content": content})["result"]
        self.assertFalse(result.get("isError", False))
        self.assertEqual(json.loads(result["content"][0]["text"]), {"name": title})
        self.upstream.create_page.assert_called_once_with(title, content, None)
        self.upstream.available.assert_not_called()
        self.upstream.create_page.side_effect = ConnectorError("Page already exists (conflict)")
        result = self.call_tool("crear_pagina", {"title": title, "content": content})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("conflict", result["content"][0]["text"])

    def test_create_page_tool_metadata(self):
        self.upstream.create_page.return_value = "A"
        metadata = {"draft": False, "tags": ["a", 2, None]}
        result = self.call_tool("crear_pagina", {"title": "A", "content": "é", "metadata": metadata})["result"]
        self.assertFalse(result.get("isError", False))
        self.upstream.create_page.assert_called_once_with("A", "é", metadata)

    def test_create_page_invalid_arguments(self):
        for arguments in ({}, {"title": "A"}, {"title": 42, "content": ""},
                          {"title": "A", "content": 42}, {"title": "A", "content": "", "metadata": []}):
            with self.subTest(arguments=arguments):
                self.assertTrue(self.call_tool("crear_pagina", arguments)["result"]["isError"])
        self.upstream.create_page.assert_not_called()

    def test_call_tool_returns_result(self):
        self.upstream.pages.return_value = [{"name": "Nota", "tags": ["x"], "status": "open"}]
        result = self.call_tool("listar_paginas", {"tag": "x", "status": "open"})["result"]
        self.assertFalse(result.get("isError", False))
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(json.loads(result["content"][0]["text"]), {"pages": self.upstream.pages.return_value})
        self.upstream.available.assert_called_once_with()
        self.upstream.pages.assert_called_once_with(tag="x", status="open", type=None, key=None,
                                                    maturity=None, draft=None)

    def test_new_tools_and_filters(self):
        self.upstream.find_by_key.return_value = {"key": "K", "pages": [], "duplicates": False}
        self.upstream.related_notes.return_value = [{"name": "A"}]
        self.upstream.entity_tasks.return_value = [{"page": "A", "ref": "A@1", "done": False}]
        cases = [("buscar_por_clave", {"key": "K"}, self.upstream.find_by_key.return_value),
                 ("notas_relacionadas", {"family": "project", "key": "K"}, {"pages": self.upstream.related_notes.return_value}),
                 ("tareas_de_entidad", {"family": "project", "key": "K"}, {"tasks": self.upstream.entity_tasks.return_value})]
        for name, arguments, expected in cases:
            with self.subTest(name=name):
                result = self.call_tool(name, arguments)["result"]
                self.assertFalse(result.get("isError", False))
                self.assertEqual(json.loads(result["content"][0]["text"]), expected)
        self.upstream.related_notes.assert_called_once_with("project", "K")
        self.upstream.entity_tasks.assert_called_once_with("project", "K")
        self.upstream.pages.return_value = []
        self.call_tool("listar_paginas", {"type": "project", "key": "K", "maturity": "seed", "draft": False})
        self.upstream.pages.assert_called_once_with(tag=None, status=None, type="project", key="K", maturity="seed", draft=False)

    def test_invalid_arguments_do_not_reach_upstream(self):
        cases = [("leer_pagina", {}), ("leer_pagina", {"name": " "}),
                 ("leer_pagina", {"name": 42}), ("buscar", {"term": ""}),
                 ("tareas_pendientes", {"due_from": "2026-1-01"}),
                 ("listar_paginas", {"draft": "false"}), ("buscar_por_clave", {"key": " "}),
                 ("notas_relacionadas", {"family": "other", "key": "K"}),
                 ("tareas_de_entidad", {"family": "project", "key": ""})]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                result = self.call_tool(name, arguments)["result"]
                self.assertTrue(result["isError"])
                self.assertTrue(result["content"])
        self.upstream.available.assert_not_called()

    def test_unknown_tool_and_upstream_error(self):
        result = self.call_tool("ejecutar_lua", {"code": "return 1"})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("ejecutar_lua", result["content"][0]["text"])
        self.upstream.available.assert_not_called()
        self.upstream.page.side_effect = ConnectorError("upstream failed")
        result = self.call_tool("leer_pagina", {"name": "Nota"})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("upstream failed", result["content"][0]["text"])
        self.upstream.page.assert_called_once_with("Nota")

    def test_index_unavailable_stops_each_tool(self):
        self.upstream.available.return_value = False
        cases = [("listar_paginas", {}), ("leer_pagina", {"name": "Nota"}),
                 ("buscar", {"term": "texto"}), ("tareas_pendientes", {}), ("resumen", {}),
                 ("buscar_por_clave", {"key": "K"}), ("notas_relacionadas", {"family": "area", "key": "K"}),
                 ("tareas_de_entidad", {"family": "area", "key": "K"})]
        for name, arguments in cases:
            with self.subTest(name=name):
                result = self.call_tool(name, arguments)["result"]
                self.assertTrue(result["isError"])
                self.assertIn("index is not available", result["content"][0]["text"])
        self.assertEqual(self.upstream.available.call_count, len(cases))
        for method in (self.upstream.pages, self.upstream.page, self.upstream.tasks, self.upstream.summary):
            method.assert_not_called()

    def test_unknown_jsonrpc_method(self):
        response = self.rpc("unknown/method")
        self.assertEqual(response["error"]["code"], -32602)
        self.upstream.available.assert_not_called()


if __name__ == "__main__":
    unittest.main()
