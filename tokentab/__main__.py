"""tokentab CLI."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from . import __version__, report, sources


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="tokentab",
        description="Where did my tokens go this week? Reads Claude Code, Codex and opencode "
                    "session logs from your own disk. No accounts, no network.",
    )
    p.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    p.add_argument("--since", help="start date YYYY-MM-DD, overrides --days")
    p.add_argument("--all", action="store_true", help="every session ever recorded")
    p.add_argument("--by", action="append", choices=["day", "model", "project", "session", "tool"],
                   help="show only these sections (repeatable)")
    p.add_argument("--top", type=int, default=8, help="rows per ranked section (default 8)")
    p.add_argument("--source", action="append", choices=sorted(sources.READERS),
                   help="restrict to one tool (repeatable)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--version", action="version", version=f"tokentab {__version__}")
    a = p.parse_args(argv)

    until = datetime.now(timezone.utc)
    if a.all:
        since = datetime(2020, 1, 1, tzinfo=timezone.utc)
        days = None
    elif a.since:
        since = datetime.fromisoformat(a.since).astimezone(timezone.utc) if "T" in a.since else \
            datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(timezone.utc)
        days = max(1, (until - since).days)
    else:
        days = a.days
        since = until - timedelta(days=days)

    records = sources.read_all(since=since, sources=a.source)
    if a.json:
        json.dump(report.to_json(records, since=since, until=until), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    color = not a.no_color and sys.stdout.isatty()
    print(report.render(records, days=days, since=since, until=until, top=a.top,
                        sections=a.by, color=color))
    return 0


if __name__ == "__main__":
    sys.exit(main())
