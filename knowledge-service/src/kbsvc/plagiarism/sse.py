"""Server-sent events for check progress.

Reads the persisted event table. There is no in-process queue: the worker runs
in a *different process* from the API, so anything held in memory here would
never see its events. The table is the only thing both sides share, and it is
therefore the source of truth.

Reconnection is exact rather than approximate. `Last-Event-ID` carries the last
id the client actually received, and replay resumes strictly after it - so a
reconnect neither drops an event nor repeats one.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator

from sqlalchemy.orm import Session

from ..config import Settings
from . import repository as repo
from .models import PlagCheck
from .types import TERMINAL_STAGES, CheckStage

logger = logging.getLogger(__name__)


def format_event(*, event_id: int | None, stage: str, data: dict) -> str:
    """One SSE frame.

    Keepalives carry no id on purpose: an id would advance the client's
    `Last-Event-ID` past real events it has not seen, and the next reconnect
    would skip them.
    """
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {stage}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False, default=str)}")
    return "\n".join(lines) + "\n\n"


def stream_check_events(
    session_factory,
    *,
    check_id: str,
    tenant_id: str,
    creator_key_id: str,
    settings: Settings,
    last_event_id: int | None = None,
) -> Iterator[str]:
    """Yield SSE frames until the check reaches a terminal state.

    Takes a session *factory*, not a session: this generator outlives the
    request handler's transaction, and holding one open for the life of a
    long-running stream would pin a pooled connection for minutes.

    Disconnecting only stops this generator. It never cancels the check - a
    caller must be able to close the page and come back.
    """
    cursor = last_event_id
    deadline = time.monotonic() + settings.plag_sse_max_seconds
    last_emit = time.monotonic()
    saw_terminal = False

    while not saw_terminal and time.monotonic() < deadline:
        with session_factory() as session:
            if not _owns(session, check_id, tenant_id, creator_key_id):
                # Same answer as elsewhere: not-yours is indistinguishable from
                # does-not-exist.
                return
            events = repo.replay_events(session, check_id=check_id, after_id=cursor)
            frames = []
            for event in events:
                cursor = event.id
                stage = CheckStage(event.stage)
                frames.append(
                    format_event(
                        event_id=event.id,
                        stage=str(stage),
                        data={
                            "check_id": check_id,
                            "status": event.status,
                            "progress": event.progress,
                            "detail": event.detail or {},
                            "created_at": event.created_at,
                        },
                    )
                )
                if stage in TERMINAL_STAGES:
                    saw_terminal = True
                    break

        for frame in frames:
            yield frame
            last_emit = time.monotonic()

        if saw_terminal:
            return

        # Something must reach the client quickly, or it cannot tell "still
        # working" from "connection is dead".
        if time.monotonic() - last_emit >= settings.plag_sse_keepalive_interval:
            yield format_event(
                event_id=None,
                stage=str(CheckStage.KEEPALIVE),
                data={"check_id": check_id},
            )
            last_emit = time.monotonic()

        time.sleep(settings.plag_sse_poll_interval)


def _owns(session: Session, check_id: str, tenant_id: str, creator_key_id: str) -> bool:
    check = session.get(PlagCheck, check_id)
    return (
        check is not None
        and check.tenant_id == tenant_id
        and check.creator_key_id == creator_key_id
    )
