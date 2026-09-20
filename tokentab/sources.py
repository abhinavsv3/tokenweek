"""Readers for each coding agent's local session store.

Every reader yields Record objects with the same shape, so the report never
needs to know which tool produced a message. Readers are pure functions of a
root directory, which is what makes them testable without touching $HOME.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import pricing


@dataclass
class Record:
    source: str            # "claude-code" | "codex" | "opencode"
    session_id: str
    project: str           # working directory, as recorded by the tool
    model: str
    at: datetime           # UTC
    input: int = 0         # uncached input tokens
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    title: str = ""        # session title if the tool recorded one
    meta: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write_5m + self.cache_write_1h

    @property
    def cost(self) -> float | None:
        return pricing.cost(self.model, input_tokens=self.input, output_tokens=self.output,
                            cache_read=self.cache_read, cache_write_5m=self.cache_write_5m,
                            cache_write_1h=self.cache_write_1h)

    @property
    def uncached_cost(self) -> float | None:
        return pricing.uncached_cost(self.model, input_tokens=self.input, output_tokens=self.output,
                                     cache_read=self.cache_read, cache_write_5m=self.cache_write_5m,
                                     cache_write_1h=self.cache_write_1h)


def _parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value > 1e12:  # milliseconds
            value = value / 1000
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _jsonl(path: Path) -> Iterator[dict]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


# --------------------------------------------------------------------------- Claude Code

def default_claude_root() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"


def read_claude_code(root: Path | None = None, since: datetime | None = None) -> Iterator[Record]:
    """Claude Code writes one JSONL per session under ~/.claude/projects/<slug>/.

    The same API response appears as several rows (one per content block), all
    carrying the same message id and identical usage. Count each id once.
    """
    root = root or default_claude_root()
    if not root.is_dir():
        return
    for path in root.rglob("*.jsonl"):
        if since is not None:
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < since:
                    continue
            except OSError:
                continue
        seen: set[str] = set()
        title = ""
        for row in _jsonl(path):
            kind = row.get("type")
            if kind == "custom-title":
                title = row.get("customTitle") or title
                continue
            if kind != "assistant":
                continue
            msg = row.get("message") or {}
            usage = msg.get("usage")
            if not usage:
                continue
            mid = msg.get("id") or row.get("requestId") or row.get("uuid")
            if mid in seen:
                continue
            seen.add(mid)
            model = msg.get("model") or "unknown"
            if model.startswith("<"):  # <synthetic> rows carry no real usage
                continue
            at = _parse_ts(row.get("timestamp"))
            if at is None or (since is not None and at < since):
                continue
            creation = usage.get("cache_creation") or {}
            write_5m = creation.get("ephemeral_5m_input_tokens")
            write_1h = creation.get("ephemeral_1h_input_tokens")
            if write_5m is None and write_1h is None:
                write_5m, write_1h = usage.get("cache_creation_input_tokens") or 0, 0
            yield Record(
                source="claude-code",
                session_id=row.get("sessionId") or path.stem,
                project=row.get("cwd") or path.parent.name,
                model=model,
                at=at,
                input=usage.get("input_tokens") or 0,
                output=usage.get("output_tokens") or 0,
                cache_read=usage.get("cache_read_input_tokens") or 0,
                cache_write_5m=write_5m or 0,
                cache_write_1h=write_1h or 0,
                title=title,
                meta={"effort": row.get("effort"), "entrypoint": row.get("entrypoint")},
            )


# --------------------------------------------------------------------------- Codex

def default_codex_root() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"


def read_codex(root: Path | None = None, since: datetime | None = None) -> Iterator[Record]:
    """Codex CLI writes rollout-*.jsonl per session, nested by date.

    Usage arrives as `token_count` events whose `info.total_token_usage` is
    cumulative for the session, so each event's contribution is the delta from
    the previous one. OpenAI counts cached input inside input_tokens.
    """
    root = root or default_codex_root()
    if not root.is_dir():
        return
    for path in root.rglob("rollout-*.jsonl"):
        if since is not None:
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < since:
                    continue
            except OSError:
                continue
        session_id = path.stem
        cwd = ""
        model = "unknown"
        prev = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        for row in _jsonl(path):
            kind = row.get("type")
            payload = row.get("payload") or {}
            if kind == "session_meta":
                session_id = payload.get("id") or session_id
                cwd = payload.get("cwd") or cwd
                continue
            if kind == "turn_context":
                model = payload.get("model") or model
                cwd = payload.get("cwd") or cwd
                continue
            if kind != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            total = info.get("total_token_usage")
            if not total:
                continue
            delta = {k: max(0, (total.get(k) or 0) - prev[k]) for k in prev}
            prev = {k: total.get(k) or 0 for k in prev}
            if not any(delta.values()):
                continue
            at = _parse_ts(row.get("timestamp"))
            if at is None or (since is not None and at < since):
                continue
            yield Record(
                source="codex",
                session_id=session_id,
                project=cwd,
                model=model,
                at=at,
                input=delta["input_tokens"] - delta["cached_input_tokens"],
                output=delta["output_tokens"],
                cache_read=delta["cached_input_tokens"],
            )


# --------------------------------------------------------------------------- opencode

def default_opencode_db() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "opencode" / "opencode.db"


def read_opencode(db: Path | None = None, since: datetime | None = None) -> Iterator[Record]:
    """opencode keeps messages as JSON blobs in SQLite, with tokens already split."""
    db = db or default_opencode_db()
    if not db.is_file():
        return
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT m.id, m.session_id, m.time_created, m.data, s.title, s.directory "
            "FROM message m LEFT JOIN session s ON s.id = m.session_id "
            "WHERE m.time_created >= ?",
            (int(since.timestamp() * 1000) if since else 0,),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return
    for r in rows:
        try:
            data = json.loads(r["data"])
        except (TypeError, json.JSONDecodeError):
            continue
        if data.get("role") != "assistant":
            continue
        tokens = data.get("tokens") or {}
        cache = tokens.get("cache") or {}
        if not tokens:
            continue
        at = _parse_ts(r["time_created"])
        if at is None:
            continue
        model = f"{data.get('providerID') or '?'}:{data.get('modelID') or '?'}"
        yield Record(
            source="opencode",
            session_id=r["session_id"],
            project=(data.get("path") or {}).get("cwd") or r["directory"] or "",
            model=model,
            at=at,
            input=tokens.get("input") or 0,
            output=(tokens.get("output") or 0) + (tokens.get("reasoning") or 0),
            cache_read=cache.get("read") or 0,
            cache_write_5m=cache.get("write") or 0,
            title=r["title"] or "",
            meta={"tool_cost": data.get("cost")},
        )


READERS = {
    "claude-code": read_claude_code,
    "codex": read_codex,
    "opencode": read_opencode,
}


def read_all(since: datetime | None = None, sources: list[str] | None = None) -> list[Record]:
    out: list[Record] = []
    for name, reader in READERS.items():
        if sources and name not in sources:
            continue
        out.extend(reader(since=since))
    out.sort(key=lambda r: r.at)
    return out
