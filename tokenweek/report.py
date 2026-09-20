"""Aggregate records and render the terminal report."""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import pricing
from .sources import Record

BAR = "▇"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}k"
    return str(n)


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "     —"
    if v >= 1000:
        return f"${v:,.0f}"
    if v >= 100:
        return f"${v:.0f}"
    return f"${v:.2f}"


def _short_project(path: str) -> str:
    if not path:
        return "(unknown)"
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home):]
    parts = path.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) > 2 else path


class Bucket:
    __slots__ = ("n", "input", "output", "cache_read", "cache_write", "cost", "uncached", "unpriced", "sessions", "title", "first", "last")

    def __init__(self):
        self.n = 0
        self.input = self.output = self.cache_read = self.cache_write = 0
        self.cost = 0.0
        self.uncached = 0.0
        self.unpriced = 0
        self.sessions: set[str] = set()
        self.title = ""
        self.first = self.last = None

    def add(self, r: Record):
        self.n += 1
        self.input += r.input
        self.output += r.output
        self.cache_read += r.cache_read
        self.cache_write += r.cache_write_5m + r.cache_write_1h
        c = r.cost
        if c is None:
            self.unpriced += r.total
        else:
            self.cost += c
            self.uncached += r.uncached_cost or 0.0
        self.sessions.add(r.session_id)
        if r.title:
            self.title = r.title
        if self.first is None or r.at < self.first:
            self.first = r.at
        if self.last is None or r.at > self.last:
            self.last = r.at

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


def group(records: list[Record], key) -> dict:
    out: dict = defaultdict(Bucket)
    for r in records:
        out[key(r)].add(r)
    return dict(out)


def summarise(records: list[Record], local_tz=None) -> dict:
    tz = local_tz or datetime.now().astimezone().tzinfo
    total = Bucket()
    for r in records:
        total.add(r)
    by_day = group(records, lambda r: r.at.astimezone(tz).strftime("%a %d"))
    by_model = group(records, lambda r: r.model)
    by_source = group(records, lambda r: r.source)
    by_project = group(records, lambda r: r.project)
    by_session = group(records, lambda r: (r.source, r.session_id))
    return {
        "total": total, "by_day": by_day, "by_model": by_model, "by_source": by_source,
        "by_project": by_project, "by_session": by_session,
    }


def _bar(value: float, maximum: float, width: int = 22) -> str:
    if maximum <= 0:
        return ""
    return BAR * max(1, round(width * value / maximum)) if value > 0 else ""


def _sort_key(b: Bucket):
    return (b.cost, b.total)


def render(records: list[Record], *, days: int | None, since: datetime, until: datetime,
           top: int = 8, sections: list[str] | None = None, color: bool = True) -> str:
    s = summarise(records)
    t: Bucket = s["total"]
    dim = (lambda x: f"\033[2m{x}\033[0m") if color else (lambda x: x)
    bold = (lambda x: f"\033[1m{x}\033[0m") if color else (lambda x: x)
    lines: list[str] = []
    if days is None:  # all time
        start = records[0].at if records else since
        span = f"{start.astimezone().strftime('%b %d %Y')} → {until.astimezone().strftime('%b %d %Y')}"
        lines.append(bold("tokenweek") + dim(f"  ·  all time  ·  {span}"))
    else:
        span = f"{since.astimezone().strftime('%b %d')} → {until.astimezone().strftime('%b %d')}"
        lines.append(bold("tokenweek") + dim(f"  ·  last {days} days  ·  {span}"))
    lines.append("")
    if not records:
        lines.append("  No agent sessions found in this window.")
        lines.append(dim("  Looked in ~/.claude/projects, ~/.codex/sessions, ~/.local/share/opencode/opencode.db"))
        return "\n".join(lines)

    tools = ", ".join(sorted(s["by_source"]))
    lines.append(f"  {t.n:,} messages  ·  {len(t.sessions):,} sessions  ·  {tools}")
    lines.append("")

    # Token / cost table
    rows = [
        ("input", t.input), ("cache write", t.cache_write),
        ("cache read", t.cache_read), ("output", t.output),
    ]
    priced = [r for r in records if r.cost is not None]
    cost_parts = {
        "input": sum(pricing.cost(r.model, input_tokens=r.input) or 0 for r in priced),
        "cache write": sum(pricing.cost(r.model, cache_write_5m=r.cache_write_5m, cache_write_1h=r.cache_write_1h) or 0 for r in priced),
        "cache read": sum(pricing.cost(r.model, cache_read=r.cache_read) or 0 for r in priced),
        "output": sum(pricing.cost(r.model, output_tokens=r.output) or 0 for r in priced),
    }
    lines.append(f"  {'TOKENS':<14}{'':>9}     {'API-EQUIVALENT':>14}")
    for name, n in rows:
        lines.append(f"  {name:<14}{_fmt_tokens(n):>9}     {_fmt_money(cost_parts[name]):>14}")
    lines.append("  " + "─" * 42)
    lines.append(bold(f"  {'total':<14}{_fmt_tokens(t.total):>9}     {_fmt_money(t.cost):>14}"))
    lines.append("")

    all_input = t.input + t.cache_read + t.cache_write
    if all_input and t.cache_read:
        pct = 100 * t.cache_read / all_input
        saved = t.uncached - t.cost
        lines.append(f"  Cache reads were {pct:.0f}% of input. Without caching this would have "
                     f"been {_fmt_money(t.uncached).strip()}. Caching saved {_fmt_money(saved).strip()}.")
        lines.append("")

    want = set(sections or ["day", "model", "project", "session"])

    def section(title: str, buckets: dict, label, limit: int | None = None, extra=None):
        lines.append(bold(f"  {title}"))
        items = sorted(buckets.items(), key=lambda kv: _sort_key(kv[1]), reverse=True)
        if limit:
            items = items[:limit]
        maximum = max((b.cost for _, b in items), default=0)
        for key, b in items:
            share = f"{100 * b.cost / t.cost:>3.0f}%" if t.cost and b.cost else "   —"
            tail = extra(key, b) if extra else ""
            bar = _bar(b.cost, maximum) if b.cost else ""
            amount = _fmt_money(b.cost) if b.cost else _fmt_tokens(b.total) + " tok"
            lines.append(f"  {label(key, b):<30} {bar:<22} {amount:>9} {dim(share)}{tail}")
        lines.append("")

    if "day" in want:
        # keep chronological order for days
        lines.append(bold("  BY DAY"))
        days_sorted = sorted(s["by_day"].items(), key=lambda kv: kv[1].first)
        maximum = max((b.cost for _, b in days_sorted), default=0)
        for key, b in days_sorted:
            bar = _bar(b.cost, maximum) if b.cost else ""
            amount = _fmt_money(b.cost) if b.cost else _fmt_tokens(b.total) + " tok"
            lines.append(f"  {key:<30} {bar:<22} {amount:>9} {dim(f'{b.n:,} msgs')}")
        lines.append("")
    if "model" in want:
        section("BY MODEL", s["by_model"], lambda k, b: k[:30], extra=lambda k, b: dim(f"  {b.n:,} msgs"))
    if "tool" in want or len(s["by_source"]) > 1:
        section("BY TOOL", s["by_source"], lambda k, b: k)
    if "project" in want:
        section(f"BY PROJECT  (top {top})", s["by_project"], lambda k, b: _short_project(k)[-30:], limit=top)
    if "session" in want:
        def label(k, b):
            title = b.title or "(untitled)"
            return title[:29] + ('…' if len(title) > 29 else '')
        session_project = {(r.source, r.session_id): r.project for r in records}

        def extra(k, b):
            hrs = ((b.last - b.first).total_seconds() / 3600) if b.first and b.last else 0
            return dim(f"  {b.n:,} msgs · {hrs:.1f}h · {_short_project(session_project[k])}")
        section(f"BY SESSION  (top {top})", s["by_session"], label, limit=top, extra=extra)

    unpriced = {k: b for k, b in s["by_model"].items() if not pricing.is_priced(k)}
    if unpriced:
        names = ", ".join(f"{k} ({_fmt_tokens(b.total)} tok)" for k, b in unpriced.items())
        lines.append(dim(f"  Not priced (no list price known): {names}"))
    lines.append(dim(f"  API-equivalent uses list prices as of {pricing.PRICES_AS_OF}. On a subscription? "
                     f"This is what the same usage would cost by the token."))
    return "\n".join(lines)


def to_json(records: list[Record], *, since: datetime, until: datetime) -> dict:
    s = summarise(records)

    def b2d(b: Bucket, key=None):
        d = {"messages": b.n, "sessions": len(b.sessions), "input": b.input, "output": b.output,
             "cache_read": b.cache_read, "cache_write": b.cache_write, "total_tokens": b.total,
             "cost_usd": round(b.cost, 4), "uncached_cost_usd": round(b.uncached, 4),
             "unpriced_tokens": b.unpriced}
        if b.title:
            d["title"] = b.title
        return d

    return {
        "since": since.isoformat(), "until": until.isoformat(),
        "prices_as_of": pricing.PRICES_AS_OF,
        "total": b2d(s["total"]),
        "by_day": {k: b2d(v) for k, v in sorted(s["by_day"].items(), key=lambda kv: kv[1].first)},
        "by_model": {k: b2d(v) for k, v in s["by_model"].items()},
        "by_tool": {k: b2d(v) for k, v in s["by_source"].items()},
        "by_project": {k: b2d(v) for k, v in s["by_project"].items()},
        "by_session": {f"{k[0]}:{k[1]}": b2d(v) for k, v in s["by_session"].items()},
    }
