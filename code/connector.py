"""Fixed-operation client for the SilverBullet APIs."""

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo


EXPRESSIONS = {
    "available": "index.isAvailable()",
    "pages": "query[[from p = index.contentPages()]]",
    "tasks": "query[[from t = index.tasks()]]",
}
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
MAX_PAGE_BYTES = 1024 * 1024
MAX_SEARCH_PAGES = 100
MAX_RUNTIME_BYTES = 10 * 1024 * 1024
MAX_TITLE_BYTES = 240
PAGE_FIELDS = ("name", "title", "type", "key", "status", "maturity", "draft", "tags", "lastModified")
ENTITY_FAMILIES = frozenset(("area", "project", "product", "topic"))


class ConnectorError(Exception):
    """An upstream, configuration, or response error safe to report to callers."""


def iso_date(value):
    """Accept only a valid ISO calendar date, not timestamps or inferred dates."""
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        return None


class SilverBulletClient:
    def __init__(self, base_url, timeout=10, opener=None):
        parsed = urllib.parse.urlsplit(base_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ConnectorError("Invalid SilverBullet URL") from exc
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or (port is not None and not 1 <= port <= 65535)):
            raise ConnectorError("SilverBullet URL must be an HTTP(S) origin without credentials")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ConnectorError("Timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(_NoRedirect())

    def _request(self, operation):
        request = urllib.request.Request(
            self.base_url + "/.runtime/lua", data=EXPRESSIONS[operation].encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read(MAX_RUNTIME_BYTES + 1)
            if len(payload) > MAX_RUNTIME_BYTES:
                raise ConnectorError("SilverBullet runtime response exceeds size limit")
            result = json.loads(payload)
            if not isinstance(result, dict) or "result" not in result:
                raise ConnectorError("Unexpected SilverBullet runtime envelope")
            return result["result"]
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ConnectorError("SilverBullet request failed") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorError("SilverBullet returned invalid JSON") from exc

    def available(self):
        result = self._request("available")
        if not isinstance(result, bool):
            raise ConnectorError("Unexpected index availability response")
        return result

    def _records(self, operation):
        result = self._request(operation)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise ConnectorError("Unexpected index response for " + operation)
        return result

    def pages(self, tag=None, status=None, type=None, key=None, maturity=None, draft=None):
        for value in (tag, status, type, key, maturity):
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 1024):
                raise ConnectorError("Page filters must be nonempty strings")
        if draft is not None and not isinstance(draft, bool):
            raise ConnectorError("Draft filter must be a boolean")
        return [{field: ([] if field == "tags" and page.get(field) == {} else page.get(field))
                 for field in PAGE_FIELDS}
                for page in self._records("pages")
                if (tag is None or (isinstance(page.get("tags"), list) and tag in page["tags"]))
                and (status is None or page.get("status") == status)
                and (type is None or page.get("type") == type)
                and (key is None or page.get("key") == key)
                and (maturity is None or page.get("maturity") == maturity)
                and (draft is None or page.get("draft") is draft)]

    def find_by_key(self, key):
        if not isinstance(key, str) or not key.strip() or len(key) > 1024:
            raise ConnectorError("Key must be a nonempty string")
        pages = self.pages(key=key)
        return {"key": key, "pages": pages, "duplicates": len(pages) > 1}

    def related_notes(self, family, key):
        if not isinstance(family, str) or family not in ENTITY_FAMILIES:
            raise ConnectorError("Invalid entity family")
        if not isinstance(key, str) or not key.strip() or len(key) > 1024:
            raise ConnectorError("Key must be a nonempty string")
        return self.pages(tag=family + "/" + key)

    def entity_tasks(self, family, key):
        pages = self.related_notes(family, key)
        names = {page["name"] for page in pages if isinstance(page.get("name"), str)}
        return [task for task in self.tasks() if task["done"] is False and task["page"] in names]

    def tasks(self, state=None, due_from=None, due_to=None):
        for value in (due_from, due_to):
            if value is not None and iso_date(value) is None:
                raise ConnectorError("Due range must use valid YYYY-MM-DD dates")
        if due_from is not None and due_to is not None and due_from > due_to:
            raise ConnectorError("Due range start must not exceed end")
        tasks = []
        for task in self._records("tasks"):
            item = {key: task.get(key) for key in ("done", "state", "text", "page", "ref")}
            item["due"] = iso_date(task.get("due"))
            if state is not None and item["state"] != state:
                continue
            if (due_from is not None or due_to is not None) and item["due"] is None:
                continue
            if due_from is not None and item["due"] < due_from:
                continue
            if due_to is not None and item["due"] > due_to:
                continue
            tasks.append(item)
        return tasks

    def page(self, name):
        if (not isinstance(name, str) or not name or "\\" in name or "%" in name
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or any(part in ("", ".", "..") for part in name.split("/"))):
            raise ConnectorError("Invalid page name")
        url = self.base_url + "/.fs/" + urllib.parse.quote(name, safe="/") + ".md"
        request = urllib.request.Request(url, headers={"Accept": "text/markdown"}, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = response.read(MAX_PAGE_BYTES + 1)
            if len(payload) > MAX_PAGE_BYTES:
                raise ConnectorError("Page exceeds size limit")
            return payload.decode("utf-8")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ConnectorError("SilverBullet page request failed") from exc
        except UnicodeDecodeError as exc:
            raise ConnectorError("SilverBullet page is not UTF-8") from exc

    def create_page(self, title, content, metadata=None):
        """Create a page without overwriting, optionally adding YAML metadata."""
        if (not isinstance(title, str) or not title or title != title.strip()
                or title.startswith(".") or any(c in '/\\%:?#' or unicodedata.category(c).startswith("C")
                                                for c in title)):
            raise ConnectorError("Invalid page title")
        if not isinstance(content, str):
            raise ConnectorError("Page content must be a string")
        try:
            title_bytes = title.encode("utf-8")
            content_bytes = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ConnectorError("Page must contain valid Unicode") from exc
        if len(title_bytes) > MAX_TITLE_BYTES or len(content_bytes) > MAX_PAGE_BYTES:
            raise ConnectorError("Page title or content exceeds size limit")
        if metadata is not None and not isinstance(metadata, dict):
            raise ConnectorError("Metadata must be an object")
        if metadata:
            if re.match(r"\A\ufeff?---(?:[ \t]*(?:\r?\n|\r|\Z))", content):
                raise ConnectorError("Page content already starts with frontmatter")
            lines = []
            size = len(content_bytes) + 8
            for key, value in metadata.items():
                if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,127}", key)
                        or key in ("__proto__", "prototype", "constructor")):
                    raise ConnectorError("Invalid metadata key")
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if (type(item) not in (str, bool, int, float, type(None))
                            or (isinstance(item, float) and not math.isfinite(item))):
                        raise ConnectorError("Invalid metadata value")
                    if isinstance(item, str):
                        try:
                            item.encode("utf-8")
                        except UnicodeEncodeError as exc:
                            raise ConnectorError("Metadata must contain valid Unicode") from exc
                try:
                    line = json.dumps(key, ensure_ascii=True) + ": " + json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n"
                except (ValueError, OverflowError) as exc:
                    raise ConnectorError("Invalid metadata value") from exc
                size += len(line)
                if size > MAX_PAGE_BYTES:
                    raise ConnectorError("Page title or content exceeds size limit")
                lines.append(line)
            content_bytes = ("---\n" + "".join(lines) + "---\n").encode("ascii") + content_bytes
        url = self.base_url + "/.fs/" + urllib.parse.quote(title, safe="") + ".md"
        request = urllib.request.Request(
            url, data=content_bytes, method="PUT",
            headers={"Content-Type": "text/markdown; charset=utf-8", "X-Sync-Mode": "true",
                     "If-None-Match": "*"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 412:
                raise ConnectorError("Page already exists (conflict)") from exc
            raise ConnectorError("SilverBullet page creation request failed") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ConnectorError("SilverBullet page creation request failed") from exc
        return title

    def search(self, term, tag=None, status=None):
        if not isinstance(term, str) or not term.strip():
            raise ConnectorError("Search term must not be empty")
        needle = term.casefold()
        candidates = self.pages(tag=tag, status=status)
        if len(candidates) > MAX_SEARCH_PAGES:
            raise ConnectorError("Search exceeds page count limit; narrow page filters")
        pages = []
        for page in candidates:
            if needle in self.page(page["name"]).casefold():
                pages.append(page)
        tasks = [t for t in self.tasks() if needle in str(t.get("text") or "").casefold()]
        return {"pages": pages, "tasks": tasks}

    def summary(self, today=None):
        today = today or dt.datetime.now(ZoneInfo("America/Bogota")).date()
        tasks = self.tasks()
        # Only an explicit false is safely identifiable as unfinished.
        pending = [task for task in tasks if task["done"] is False]
        date = today.isoformat()
        return {
            "timezone": "America/Bogota", "date": date,
            "pages_count": len(self.pages()), "tasks_count": len(tasks),
            "pending_count": len(pending),
            "pending_due_today": [t for t in pending if t["due"] == date],
        }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only SilverBullet index client")
    parser.add_argument("--url", default=os.environ.get("SILVERBULLET_URL", "http://127.0.0.1:3000"))
    parser.add_argument("--timeout", type=float, default=10)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for name in ("available", "summary"):
        subparsers.add_parser(name)
    pages = subparsers.add_parser("pages")
    pages.add_argument("--tag")
    pages.add_argument("--status")
    pages.add_argument("--type")
    pages.add_argument("--key")
    pages.add_argument("--maturity")
    pages.add_argument("--draft", choices=("true", "false"))
    key_parser = subparsers.add_parser("find-by-key")
    key_parser.add_argument("key")
    for name in ("related-notes", "entity-tasks"):
        entity = subparsers.add_parser(name)
        entity.add_argument("family", choices=sorted(ENTITY_FAMILIES))
        entity.add_argument("key")
    page = subparsers.add_parser("page")
    page.add_argument("name")
    tasks = subparsers.add_parser("tasks")
    tasks.add_argument("--state")
    tasks.add_argument("--due-from")
    tasks.add_argument("--due-to")
    search = subparsers.add_parser("search")
    search.add_argument("term")
    search.add_argument("--tag")
    search.add_argument("--status")
    args = parser.parse_args(argv)
    try:
        client = SilverBulletClient(args.url, args.timeout)
        if args.operation == "search":
            result = client.search(args.term, args.tag, args.status)
        elif args.operation == "pages":
            result = client.pages(args.tag, args.status, args.type, args.key, args.maturity,
                                  None if args.draft is None else args.draft == "true")
        elif args.operation == "find-by-key":
            result = client.find_by_key(args.key)
        elif args.operation == "related-notes":
            result = client.related_notes(args.family, args.key)
        elif args.operation == "entity-tasks":
            result = client.entity_tasks(args.family, args.key)
        elif args.operation == "page":
            result = client.page(args.name)
        elif args.operation == "tasks":
            result = client.tasks(args.state, args.due_from, args.due_to)
        else:
            result = getattr(client, args.operation)()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ConnectorError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
