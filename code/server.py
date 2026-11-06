"""MCP interface to the SilverBullet connector."""

import os
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field, StrictBool

from connector import ConnectorError, MAX_SEARCH_PAGES, SilverBulletClient


mcp = FastMCP("SilverBullet", host=os.environ.get("MCP_BIND_HOST", "127.0.0.1"), port=8000, stateless_http=True)
client = SilverBulletClient(os.environ.get("SILVERBULLET_URL", "http://127.0.0.1:3000"))

NonEmpty = Annotated[str, Field(min_length=1, max_length=1024, pattern=r"\S")]
OptionalFilter = Annotated[str | None, Field(min_length=1, max_length=1024, pattern=r"\S")]
DateFilter = Annotated[str | None, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]


def require_index():
    if not client.available():
        raise ConnectorError("SilverBullet index is not available")


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
def listar_paginas(tag: OptionalFilter = None, status: OptionalFilter = None,
                   type: OptionalFilter = None, key: OptionalFilter = None,
                   maturity: OptionalFilter = None, draft: StrictBool | None = None) -> dict:
    """List indexed page metadata with exact filters. Names are references."""
    require_index()
    return {"pages": client.pages(tag=tag, status=status, type=type, key=key,
                                  maturity=maturity, draft=draft)}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
def buscar_por_clave(key: NonEmpty) -> dict:
    """Find exact keys and report whether multiple indexed pages share the key."""
    require_index()
    return client.find_by_key(key)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
def notas_relacionadas(family: Literal["area", "project", "product", "topic"], key: NonEmpty) -> dict:
    """List pages directly tagged with the exact family/key tag, without inference."""
    require_index()
    return {"pages": client.related_notes(family, key)}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
def tareas_de_entidad(family: Literal["area", "project", "product", "topic"], key: NonEmpty) -> dict:
    """List explicitly unfinished tasks on pages with the exact family/key tag."""
    require_index()
    return {"tasks": client.entity_tasks(family, key)}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
def leer_pagina(name: NonEmpty) -> dict:
    """Read a page by its exact name; the name is its source reference."""
    require_index()
    return {"name": name, "content": client.page(name)}


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
def crear_pagina(title: str, content: str, metadata: dict | None = None) -> dict:
    """Create a page without overwriting, optionally prepending metadata as YAML frontmatter."""
    return {"name": client.create_page(title, content, metadata)}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
def buscar(term: NonEmpty, tag: OptionalFilter = None, status: OptionalFilter = None) -> dict:
    """Search page contents and task text; page filters apply to both result types."""
    require_index()
    candidates = client.pages(tag=tag, status=status)
    if len(candidates) > MAX_SEARCH_PAGES:
        raise ConnectorError("Search exceeds page count limit; narrow page filters")
    needle = term.casefold()
    pages = [page for page in candidates if needle in client.page(page["name"]).casefold()]
    tasks = client.tasks()
    if tag is not None or status is not None:
        names = {page["name"] for page in candidates}
        tasks = [task for task in tasks if task["page"] in names]
    return {"pages": pages, "tasks": [task for task in tasks if needle in str(task["text"] or "").casefold()]}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
def tareas_pendientes(state: OptionalFilter = None, due_from: DateFilter = None,
                      due_to: DateFilter = None) -> dict:
    """List explicitly unfinished tasks. Inclusive dates use only explicit ISO due metadata."""
    require_index()
    tasks = client.tasks(state=state, due_from=due_from, due_to=due_to)
    return {"tasks": [task for task in tasks if task["done"] is False]}


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
def resumen() -> dict:
    """Summarize the index and pending tasks due today in America/Bogota."""
    require_index()
    return client.summary()


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
