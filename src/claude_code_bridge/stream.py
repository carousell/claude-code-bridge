"""Parsing of `claude --output-format stream-json` output.

The stream is newline-delimited JSON, but it is not a clean contract: alongside the events we
care about it carries hook lifecycle events, rate-limit notices, and whatever the CLI decides to
add next. Anything unrecognised or malformed is skipped rather than raised — a task must not fail
because of a line we did not expect.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


def parse_line(line: str) -> dict[str, Any] | None:
    """Decode one stream line, or return None if it is blank, truncated, or not a JSON object."""
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        log.debug("skipping non-JSON stream line: %.200s", line)
        return None
    if not isinstance(event, dict):
        log.debug("skipping non-object stream event: %.200s", line)
        return None
    return event


@dataclass
class StreamState:
    """Fields accumulated from a run's event stream as it arrives."""

    session_id: str | None = None
    result_event: dict[str, Any] | None = None
    event_count: int = 0
    unparsable_lines: int = 0
    _seen_types: set[str] = field(default_factory=set)

    def ingest(self, event: dict[str, Any]) -> None:
        self.event_count += 1
        event_type = event.get("type")
        if isinstance(event_type, str):
            self._seen_types.add(event_type)

        session_id = event.get("session_id")
        if self.session_id is None and isinstance(session_id, str) and session_id:
            self.session_id = session_id

        if event_type == "result":
            self.result_event = event

    @property
    def summary(self) -> str | None:
        """The agent's closing message, present once the run has produced its result event."""
        if self.result_event is None:
            return None
        text = self.result_event.get("result")
        return text if isinstance(text, str) else None

    @property
    def is_error(self) -> bool | None:
        if self.result_event is None:
            return None
        return bool(self.result_event.get("is_error"))

    @property
    def total_cost_usd(self) -> float | None:
        if self.result_event is None:
            return None
        cost = self.result_event.get("total_cost_usd")
        return float(cost) if isinstance(cost, (int, float)) else None

    @property
    def num_turns(self) -> int | None:
        if self.result_event is None:
            return None
        turns = self.result_event.get("num_turns")
        return int(turns) if isinstance(turns, int) else None

    @property
    def subtype(self) -> str | None:
        if self.result_event is None:
            return None
        subtype = self.result_event.get("subtype")
        return subtype if isinstance(subtype, str) else None

    @property
    def permission_denials(self) -> list[dict[str, Any]]:
        """Tool calls the deny rules blocked — the audit trail for the commit/push boundary."""
        if self.result_event is None:
            return []
        denials = self.result_event.get("permission_denials")
        return denials if isinstance(denials, list) else []
