"""FastMCP server exposing three read-only knowledge tools.

Deliberately minimal: an agent gets search, context expansion, and discovery -
nothing that mutates the index. Tenant and ACL come from server configuration
(KB_MCP_API_KEY or the default tenant), never from tool arguments, so a
prompt-injected agent cannot widen its own access.
"""

from __future__ import annotations

import json
import os
from typing import Literal

from ..config import get_settings
from ..db import repo
from ..db.models import Document
from ..db.session import init_db, session_scope
from ..errors import KbError
from ..retrieval.pipeline import RetrievalRequest, get_retrieval_service
from .compat import build_server

mcp, MCP_FLAVOUR = build_server("kbsvc")


def _principal() -> tuple[str, list[str] | None]:
    """Resolve tenant + ACL from server-side configuration only."""
    settings = get_settings()
    raw_key = os.environ.get("KB_MCP_API_KEY", "")
    if raw_key:
        from ..api.auth import resolve_principal

        with session_scope() as session:
            principal = resolve_principal(session, raw_key)
        return principal.tenant_id, principal.acl_filter
    if settings.auth_required:
        raise KbError(
            "KB_MCP_API_KEY is required when auth_required is true",
            {"hint": "set KB_MCP_API_KEY or disable auth_required"},
        )
    return settings.default_tenant, None


def _format_results(payload: dict) -> str:
    results = payload.get("results", [])
    if not results:
        return "No matching passages found."

    lines: list[str] = []
    for index, item in enumerate(results, start=1):
        breadcrumb = " › ".join(item.get("heading_path") or []) or "(no heading)"
        page = f" p.{item['page']}" if item.get("page") else ""
        lines.append(
            f"[{index}] {item.get('title', '')} › {breadcrumb}{page}\n"
            f"    {item.get('snippet', '').strip()}\n"
            f"    cite: {item['document_id']}#{item['chunk_ordinal']}  "
            f"score={item.get('score')} rerank={item.get('rerank_score')}\n"
            f"    source: {item.get('source_uri', '')}"
        )
    return "\n\n".join(lines)


@mcp.tool()
def search_knowledge(
    query: str,
    top_k: int = 8,
    mode: Literal["hybrid", "dense", "sparse"] = "hybrid",
    source_ids: list[str] | None = None,
    document_ids: list[str] | None = None,
) -> str:
    """Search the knowledge base and return passages with verifiable citations.

    Each result carries `document_id#chunk_ordinal`; pass those to
    fetch_document_chunks to read surrounding context before answering.
    """
    tenant_id, acl = _principal()
    request = RetrievalRequest(
        query=query,
        tenant_id=tenant_id,
        top_k=max(1, min(top_k, 50)),
        mode=mode,
        acl=acl,
        source_ids=source_ids,
        document_ids=document_ids,
    )
    payload = get_retrieval_service().search(request).to_dict()
    return f"{_format_results(payload)}\n\n```json\n{json.dumps(payload, ensure_ascii=False)}\n```"


@mcp.tool()
def fetch_document_chunks(document_id: str, from_ordinal: int = 0, limit: int = 10) -> str:
    """Read consecutive chunks of one document, in order, for full context."""
    tenant_id, _ = _principal()
    with session_scope() as session:
        document = session.get(Document, document_id)
        if document is None or document.tenant_id != tenant_id or document.deleted_at:
            return f"Document {document_id} not found."
        chunks = repo.fetch_chunks(
            session,
            document_id=document_id,
            version_id=document.current_version_id,
            from_ordinal=max(from_ordinal, 0),
            limit=max(1, min(limit, 50)),
        )
        title = document.title
        blocks = [
            {
                "ordinal": chunk.ordinal,
                "heading_path": list(chunk.heading_path or []),
                "page": chunk.page_from,
                "text": chunk.text,
            }
            for chunk in chunks
        ]

    if not blocks:
        return f"No chunks at or after ordinal {from_ordinal} in {title}."
    rendered = "\n\n".join(
        f"--- #{b['ordinal']} {' › '.join(b['heading_path']) or '(no heading)'} ---\n{b['text']}"
        for b in blocks
    )
    return f"# {title}\n\n{rendered}"


@mcp.tool()
def list_sources() -> str:
    """List the knowledge sources visible to this server, with document counts."""
    tenant_id, _ = _principal()
    with session_scope() as session:
        sources = repo.list_sources(session, tenant_id)
        rows = [
            {"id": source.id, "name": source.name, "kind": source.kind, "uri": source.uri}
            for source in sources
        ]
    if not rows:
        return "No sources registered."
    listing = "\n".join(f"- {row['name']} ({row['kind']}) id={row['id']}" for row in rows)
    return f"{listing}\n\n```json\n{json.dumps(rows, ensure_ascii=False)}\n```"


def run(transport: str = "stdio", *, host: str = "", port: int = 0) -> None:
    init_db()
    if transport == "stdio":
        mcp.run(transport=transport)
        return
    # The HTTP transports default to 127.0.0.1:8000, and since mcp 2.0 they no
    # longer read the FASTMCP_* environment variables the v1 FastMCP honoured -
    # the bind address is a keyword argument now. A container that binds
    # loopback is unreachable from anywhere, so pass it explicitly rather than
    # relying on a convention that silently stopped applying.
    try:
        mcp.run(transport=transport, host=host or "127.0.0.1", port=port or 8000)
    except TypeError as exc:  # pragma: no cover - only on the mcp<2 shim path
        raise KbError(
            f"{MCP_FLAVOUR} does not accept host/port at run time; "
            "set FASTMCP_HOST and FASTMCP_PORT instead",
            {"transport": transport},
        ) from exc


if __name__ == "__main__":  # pragma: no cover
    run(os.environ.get("KB_MCP_TRANSPORT", "stdio"))
