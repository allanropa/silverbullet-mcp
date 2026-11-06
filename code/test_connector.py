import datetime as dt
import io
import json
import unittest
import urllib.error
import yaml
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import connector


class Response:
    def __init__(self, value, headers=None):
        self.value = value
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size=-1):
        return self.value[:size] if size >= 0 else self.value


class Opener:
    def __init__(self, results):
        self.results = list(results)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, Response) else Response(result if isinstance(result, bytes) else json.dumps(result).encode())


def wrapped(result):
    return {"result": result}


class ConnectorTests(unittest.TestCase):
    def client(self, results):
        opener = Opener(results)
        return connector.SilverBulletClient("http://example.test:3000", opener=opener), opener

    def test_runtime_contract_and_projection(self):
        client, opener = self.client([wrapped(True), wrapped([{"name": "A", "tags": ["x"], "secret": "no"}]),
                                      wrapped([{"text": "2026-11-06", "due": "2026-11-07", "extra": 1}])])
        self.assertTrue(client.available())
        self.assertEqual(client.pages(), [{"name": "A", "title": None, "type": None, "key": None,
                                           "status": None, "maturity": None, "draft": None,
                                           "tags": ["x"], "lastModified": None}])
        task = client.tasks()[0]
        self.assertEqual(task["due"], "2026-11-07")
        self.assertNotIn("extra", task)
        self.assertEqual([r.data.decode() for r, _ in opener.requests], list(connector.EXPRESSIONS.values()))
        self.assertTrue(all(r.method == "POST" and not r.data.startswith(b"return ") for r, _ in opener.requests))

    def test_page_url_is_encoded_and_get_only(self):
        client, opener = self.client(["contenido".encode()])
        self.assertEqual(client.page("Carpeta/Nota #1?"), "contenido")
        request, _ = opener.requests[0]
        self.assertEqual(request.full_url, "http://example.test:3000/.fs/Carpeta/Nota%20%231%3F.md")
        self.assertEqual(request.method, "GET")
        self.assertIsNone(request.data)

    def test_page_rejects_unsafe_names_without_request(self):
        client, opener = self.client([])
        for name in ("", "/a", "a/", "a//b", ".", "../a", "a/../b", "a/./b", "a\\b", "%2e%2e/a", "a\nb"):
            with self.subTest(name=name), self.assertRaises(connector.ConnectorError):
                client.page(name)
        self.assertEqual(opener.requests, [])

    def test_create_page_contract_and_exact_unicode(self):
        title = 'Nota "café" 🐱'
        content = '---\ndraft: false\n---\nContenido Ω\r\n'
        client, opener = self.client([b""])
        self.assertEqual(client.create_page(title, content), title)
        request, timeout = opener.requests[0]
        self.assertEqual(timeout, 10)
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.full_url, "http://example.test:3000/.fs/Nota%20%22caf%C3%A9%22%20%F0%9F%90%B1.md")
        self.assertEqual(request.get_header("If-none-match"), "*")
        self.assertEqual(request.get_header("X-sync-mode"), "true")
        self.assertEqual(request.get_header("Content-type"), "text/markdown; charset=utf-8")
        self.assertEqual(request.data, content.encode("utf-8"))

    def test_create_page_exact_empty_and_plain_content(self):
        for content in ("", "Texto sin frontmatter\n", "\ufeffcontenido\r\n"):
            with self.subTest(content=content):
                client, opener = self.client([b""])
                self.assertEqual(client.create_page("Title", content), "Title")
                self.assertEqual(opener.requests[0][0].data, content.encode("utf-8"))

    def test_create_page_metadata_serialization_and_injection(self):
        metadata = {"title": 'café\n---\ninjected: true', "draft": False,
                    "count": 2, "ratio": 1.5, "empty": None,
                    "tags": ["x\ny", True, 3, None], "_safe-key": "\"\\"}
        client, opener = self.client([b""])
        content = "Cuerpo Ω\r\n"
        client.create_page("Title", content, metadata)
        result = opener.requests[0][0].data.decode("utf-8")
        frontmatter, body = result[4:].split("\n---\n", 1)
        self.assertEqual(yaml.safe_load(frontmatter), metadata)
        self.assertEqual(body, content)
        self.assertIn("draft: false\n", result)
        self.assertNotIn('"draft"', result)
        self.assertEqual(opener.requests[0][0].get_header("If-none-match"), "*")

    def test_metadata_plain_and_ambiguous_strings(self):
        metadata = {"title": "Nota café", "status": "true", "date": "2026-11-06", "tags": ["project/foo", "null"]}
        client, opener = self.client([b""])
        client.create_page("Title", "", metadata)
        data = opener.requests[0][0].data.decode()
        self.assertIn("title: Nota café\n", data)
        self.assertEqual(yaml.safe_load(data[4:-4]), metadata)

    def test_page_revision_and_update(self):
        client, opener = self.client([Response(b"original", {"ETag": '"sha256:abc"'}), b""])
        page = client.page("Carpeta/Nota #1", with_revision=True)
        self.assertEqual(page, {"name": "Carpeta/Nota #1", "content": "original", "revision": '"sha256:abc"'})
        self.assertEqual(client.update_page(page["name"], "---\ntitle: café\n---\n", page["revision"]), page["name"])
        request, timeout = opener.requests[1]
        self.assertEqual(request.full_url, "http://example.test:3000/.fs/Carpeta/Nota%20%231.md")
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.get_header("If-match"), page["revision"])
        self.assertEqual(request.get_header("X-sync-mode"), "true")
        self.assertEqual(request.data, "---\ntitle: café\n---\n".encode())
        self.assertEqual(timeout, 10)

    def test_update_validation_and_conflicts(self):
        client, opener = self.client([])
        for name, content, revision in [("../x", "", '"a"'), ("A", None, '"a"'),
                                        ("A", "\ud800", '"a"'), ("A", "x" * (connector.MAX_PAGE_BYTES + 1), '"a"'),
                                        ("A", "", "*"), ("A", "", 'W/"a"'), ("A", "", '"a\nb"')]:
            with self.subTest(name=name, revision=revision), self.assertRaises(connector.ConnectorError):
                client.update_page(name, content, revision)
        self.assertEqual(opener.requests, [])
        for headers in ({}, {"ETag": 'W/"a"'}):
            with self.assertRaisesRegex(connector.ConnectorError, "revision"):
                self.client([Response(b"x", headers)])[0].page("A", with_revision=True)
        error = urllib.error.HTTPError("http://example.test", 412, "error", {}, None)
        with self.assertRaisesRegex(connector.ConnectorError, "conflict"):
            self.client([error])[0].update_page("A", "new", '"old"')

    def test_create_page_empty_metadata_preserves_existing_frontmatter(self):
        for metadata in (None, {}):
            client, opener = self.client([b""])
            content = "\ufeff---\r\nx: 1\r\n---\r\n"
            client.create_page("Title", content, metadata)
            self.assertEqual(opener.requests[0][0].data, content.encode("utf-8"))

    def test_create_page_metadata_validation_without_requests(self):
        client, opener = self.client([])
        bad = [[], "x", 0, {1: "x"}, {"": 1}, {"bad\nkey": 1}, {"a: b": 1},
               {"__proto__": 1}, {"constructor": 1}, {"prototype": 1}, {"x" * 129: 1},
               {"x": {}}, {"x": [[1]]}, {"x": [dict(a=1)]}, {"x": float("nan")},
               {"x": [float("inf")]}, {"x": "\ud800"}, {"x": (1, 2)}]
        for metadata in bad:
            with self.subTest(metadata=metadata), self.assertRaises(connector.ConnectorError):
                client.create_page("Title", "", metadata)
        for content in ("---", "---\nkey: value\n---\n", "\ufeff---\r\n", "---  \r"):
            with self.subTest(content=content), self.assertRaisesRegex(connector.ConnectorError, "frontmatter"):
                client.create_page("Title", content, {"x": 1})
        with patch.object(connector, "MAX_PAGE_BYTES", 20):
            with self.assertRaisesRegex(connector.ConnectorError, "size limit"):
                client.create_page("Title", "1234567890", {"x": "abc"})
        self.assertEqual(opener.requests, [])

    def test_create_page_rejects_invalid_input_without_request(self):
        client, opener = self.client([])
        for title in (None, "", " ", " a", "a ", ".hidden", "..", "a/b", "a\\b", "%2f",
                      "https://example.test", "a?b", "a#b", "a\nb", "a\x00b", "\ud800",
                      "é" * (connector.MAX_TITLE_BYTES // 2 + 1)):
            with self.subTest(title=title), self.assertRaises(connector.ConnectorError):
                client.create_page(title, "content")
        for content in (None, 123, "\ud800", "x" * (connector.MAX_PAGE_BYTES + 1)):
            with self.subTest(content=type(content)), self.assertRaises(connector.ConnectorError):
                client.create_page("Title", content)
        self.assertEqual(opener.requests, [])

    def test_create_page_limits_and_errors(self):
        client, opener = self.client([b""])
        self.assertEqual(client.create_page("é" * (connector.MAX_TITLE_BYTES // 2),
                                             "x" * connector.MAX_PAGE_BYTES),
                         "é" * (connector.MAX_TITLE_BYTES // 2))
        self.assertEqual(len(opener.requests), 1)
        for code, message in ((412, "conflict"), (403, "request failed")):
            error = urllib.error.HTTPError("http://example.test", code, "error", {}, None)
            client, opener = self.client([error])
            with self.subTest(code=code), self.assertRaisesRegex(connector.ConnectorError, message):
                client.create_page("Title", "")
            self.assertEqual(len(opener.requests), 1)
        with self.assertRaisesRegex(connector.ConnectorError, "request failed"):
            self.client([urllib.error.URLError("offline")])[0].create_page("Title", "")

    def test_search_content_not_name_and_no_interpolation(self):
        term = 'x"]] return dangerous() --'
        client, opener = self.client([wrapped([{"name": "One"}, {"name": "Two"}]),
                                      term.encode(), b"nothing", wrapped([{"text": term, "done": False}])])
        result = client.search(term)
        self.assertEqual([p["name"] for p in result["pages"]], ["One"])
        self.assertEqual(len(result["tasks"]), 1)
        self.assertTrue(all(term.encode() not in r.data for r, _ in opener.requests if r.data))
        with self.assertRaises(connector.ConnectorError):
            client.search("  ")

    def test_page_filters_and_search_limit(self):
        records = [{"name": "A", "tags": ["x"], "status": "open"},
                   {"name": "B", "tags": ["xy"], "status": "open"},
                   {"name": "C", "tags": ["x"], "status": "closed"}]
        client, _ = self.client([wrapped(records)])
        self.assertEqual([p["name"] for p in client.pages("x", "open")], ["A"])
        client, opener = self.client([wrapped(records), b"needle", wrapped([])])
        self.assertEqual(len(client.search("needle", "x", "open")["pages"]), 1)
        self.assertEqual(len(opener.requests), 3)
        client, opener = self.client([wrapped([{"name": str(i)} for i in range(connector.MAX_SEARCH_PAGES + 1)])])
        with self.assertRaisesRegex(connector.ConnectorError, "page count limit"):
            client.search("needle")
        self.assertEqual(len(opener.requests), 1)

    def test_task_state_and_inclusive_due_range(self):
        records = [{"state": "todo", "due": "2026-11-06"},
                   {"state": "todo", "due": "2026-11-07"},
                   {"state": "done", "due": "2026-11-06"},
                   {"state": "todo", "text": "2026-11-06"},
                   {"state": "todo", "due": "2026-11-06T00:00:00Z"},
                   {"state": "todo", "due": "2026-02-30"}]
        client, _ = self.client([wrapped(records)])
        tasks = client.tasks("todo", "2026-11-06", "2026-11-07")
        self.assertEqual([t["due"] for t in tasks], ["2026-11-06", "2026-11-07"])
        for start, end in (("2026-2-01", None), ("2026-02-30", None), ("2026-12-01", "2026-11-01")):
            with self.subTest(start=start), self.assertRaises(connector.ConnectorError):
                client.tasks(due_from=start, due_to=end)

    def test_metadata_filters_and_empty_tags(self):
        records = [dict(name="A", title="Title", type="project", key="K", status="open",
                        maturity="seed", draft=False, tags=["topic/K"], lastModified=123, secret=1),
                   dict(name="B", type="project", key="K", status="open", maturity="seed", draft=0, tags={}),
                   dict(name="C", type="project", key="K2", status="open", maturity="seed", draft=False)]
        client, _ = self.client([wrapped(records), wrapped(records)])
        pages = client.pages(tag="topic/K", status="open", type="project", key="K", maturity="seed", draft=False)
        self.assertEqual(pages, [{k: records[0][k] for k in connector.PAGE_FIELDS}])
        self.assertEqual([p["name"] for p in client.pages(type="project", key="K", draft=False)], ["A"])
        client, _ = self.client([wrapped(records)])
        self.assertEqual(client.pages()[1]["tags"], [])

    def test_key_duplicates_and_exact_relationships(self):
        records = [{"name": "A", "key": "K", "tags": ["project/K"]},
                   {"name": "B", "key": "K", "tags": ["project/K", "other"]},
                   {"name": "C", "key": "K2", "tags": ["project/K2"]},
                   {"name": "D", "tags": {}}, {"name": "E", "tags": ["area/K"]}]
        tasks = [{"page": "A", "ref": "A@1", "done": False, "text": "2026-11-06"},
                 {"page": "B", "ref": "B@1", "done": False, "due": "2026-11-07"},
                 {"page": "A", "done": True}, {"page": "A", "done": 0},
                 {"page": "A"}, {"page": "C", "done": False}, {"done": False}]
        client, opener = self.client([wrapped(records), wrapped(records), wrapped(records), wrapped(records), wrapped(tasks)])
        self.assertTrue(client.find_by_key("K")["duplicates"])
        self.assertFalse(client.find_by_key("missing")["duplicates"])
        self.assertEqual([p["name"] for p in client.related_notes("project", "K")], ["A", "B"])
        result = client.entity_tasks("project", "K")
        self.assertEqual([t["ref"] for t in result], ["A@1", "B@1"])
        self.assertEqual([t["due"] for t in result], [None, "2026-11-07"])
        self.assertTrue(all(r.data.decode() in connector.EXPRESSIONS.values() for r, _ in opener.requests))

    def test_new_validation_and_cli(self):
        client, opener = self.client([])
        for call in (lambda: client.pages(draft=0), lambda: client.pages(key=" "),
                     lambda: client.find_by_key(""), lambda: client.related_notes("bad", "K"),
                     lambda: client.entity_tasks("project", " ")):
            with self.assertRaises(connector.ConnectorError):
                call()
        self.assertEqual(opener.requests, [])
        with patch.object(connector, "SilverBulletClient") as factory, redirect_stdout(io.StringIO()):
            factory.return_value.pages.return_value = []
            self.assertEqual(connector.main(["pages", "--type", "project", "--draft", "false"]), 0)
            factory.return_value.pages.assert_called_once_with(None, None, "project", None, None, False)

    def test_summary_uses_only_due_metadata(self):
        client, _ = self.client([wrapped([{"done": False, "due": "2026-11-06"},
                                          {"done": False, "text": "2026-11-06"},
                                          {"done": True, "due": "2026-11-06"},
                                          {"due": "2026-11-06"}]), wrapped([{"name": "A"}])])
        result = client.summary(dt.date(2026, 11, 6))
        self.assertEqual((result["pages_count"], result["tasks_count"], result["pending_count"]), (1, 4, 2))
        self.assertEqual(len(result["pending_due_today"]), 1)
        self.assertEqual(result["timezone"], "America/Bogota")

    def test_errors_and_size_limits(self):
        for url in ("file:///tmp/a", "http://u:p@example.test", "http://example.test/path", "http://example.test:bad"):
            with self.subTest(url=url), self.assertRaises(connector.ConnectorError):
                connector.SilverBulletClient(url)
        for response in (b"not json", {"error": "bad"}, [1], wrapped([1]), wrapped({})):
            with self.subTest(response=response), self.assertRaises(connector.ConnectorError):
                self.client([response])[0].pages()
        for response in (urllib.error.URLError("offline"), b"\xff", b"x" * (connector.MAX_PAGE_BYTES + 1)):
            with self.subTest(response=type(response)), self.assertRaises(connector.ConnectorError):
                self.client([response])[0].page("A")
        with patch.object(connector, "MAX_RUNTIME_BYTES", 5), self.assertRaises(connector.ConnectorError):
            self.client([wrapped(True)])[0].available()
        with self.assertRaises(connector.ConnectorError):
            self.client([wrapped(1)])[0].available()
        with self.assertRaises(connector.ConnectorError):
            connector.SilverBulletClient("http://example.test", float("nan"))

    def test_cli_errors_are_json(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = connector.main(["--url", "file:///tmp/a", "pages"])
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("error", json.loads(stderr.getvalue()))


if __name__ == "__main__":
    unittest.main()
