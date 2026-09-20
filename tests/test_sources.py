"""Each reader against a synthetic store, plus the arithmetic that must not drift."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tokenweek import pricing, report, sources  # noqa: E402


def _cc_row(mid, model="claude-opus-5", ts="2026-09-18T10:00:00.000Z", **usage):
    base = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0}
    base.update(usage)
    return {"type": "assistant", "timestamp": ts, "sessionId": "s1", "cwd": "/home/me/proj",
            "message": {"id": mid, "model": model, "usage": base}}


class TestClaudeCode:
    def test_dedupes_rows_that_share_a_message_id(self, tmp_path):
        d = tmp_path / "projects" / "-home-me-proj"
        d.mkdir(parents=True)
        rows = [
            _cc_row("m1", input_tokens=10, output_tokens=5, cache_read_input_tokens=100),
            _cc_row("m1", input_tokens=10, output_tokens=5, cache_read_input_tokens=100),  # second content block
            _cc_row("m2", output_tokens=7),
            {"type": "custom-title", "customTitle": "Fix the widget"},
            {"type": "user", "message": {"role": "user", "content": "hi"}},
        ]
        (d / "s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        recs = list(sources.read_claude_code(tmp_path / "projects"))
        assert len(recs) == 2
        assert sum(r.output for r in recs) == 12
        assert recs[0].project == "/home/me/proj"

    def test_splits_cache_writes_by_ttl_when_present(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        row = _cc_row("m1", cache_creation_input_tokens=300)
        row["message"]["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200}
        (d / "s.jsonl").write_text(json.dumps(row) + "\n")
        r = next(sources.read_claude_code(d))
        assert (r.cache_write_5m, r.cache_write_1h) == (100, 200)

    def test_falls_back_to_5m_when_no_breakdown(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        (d / "s.jsonl").write_text(json.dumps(_cc_row("m1", cache_creation_input_tokens=300)) + "\n")
        r = next(sources.read_claude_code(d))
        assert (r.cache_write_5m, r.cache_write_1h) == (300, 0)

    def test_skips_synthetic_and_respects_since(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        rows = [_cc_row("m1", model="<synthetic>", output_tokens=1),
                _cc_row("m2", ts="2026-01-01T00:00:00Z", output_tokens=1),
                _cc_row("m3", output_tokens=1)]
        (d / "s.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        since = datetime(2026, 9, 1, tzinfo=timezone.utc)
        recs = list(sources.read_claude_code(d, since=since))
        assert [r.model for r in recs] == ["claude-opus-5"]

    def test_tolerates_garbage_lines(self, tmp_path):
        d = tmp_path / "p"; d.mkdir()
        (d / "s.jsonl").write_text("not json\n" + json.dumps(_cc_row("m1", output_tokens=1)) + "\n{\n")
        assert len(list(sources.read_claude_code(d))) == 1


class TestCodex:
    def _write(self, tmp_path, events):
        d = tmp_path / "sessions" / "2026" / "09" / "18"
        d.mkdir(parents=True)
        (d / "rollout-2026-09-18T10-00-00-abc.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        return tmp_path / "sessions"

    def _count(self, inp, cached, out, ts="2026-09-18T10:00:01Z"):
        return {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}}}}

    def test_takes_deltas_of_cumulative_usage(self, tmp_path):
        root = self._write(tmp_path, [
            {"type": "session_meta", "payload": {"id": "sess", "cwd": "/w"}},
            {"type": "turn_context", "payload": {"model": "gpt-5.2-codex", "cwd": "/w"}},
            self._count(100, 80, 10),
            self._count(100, 80, 10),      # duplicate event: contributes nothing
            self._count(250, 200, 25),
        ])
        recs = list(sources.read_codex(root))
        assert len(recs) == 2
        assert (recs[0].input, recs[0].cache_read, recs[0].output) == (20, 80, 10)
        assert (recs[1].input, recs[1].cache_read, recs[1].output) == (30, 120, 15)
        assert recs[0].model == "gpt-5.2-codex" and recs[0].session_id == "sess"

    def test_null_info_is_skipped(self, tmp_path):
        root = self._write(tmp_path, [{"timestamp": "2026-09-18T10:00:00Z", "type": "event_msg",
                                       "payload": {"type": "token_count", "info": None}}])
        assert list(sources.read_codex(root)) == []


class TestOpencode:
    def _db(self, tmp_path):
        db = tmp_path / "opencode.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE session (id text primary key, title text, directory text)")
        con.execute("CREATE TABLE message (id text primary key, session_id text, time_created integer, time_updated integer, data text)")
        con.execute("INSERT INTO session VALUES ('ses1', 'Refactor auth', '/w')")
        a = {"role": "assistant", "providerID": "anthropic", "modelID": "claude-sonnet-5",
             "tokens": {"input": 100, "output": 20, "reasoning": 5, "cache": {"read": 400, "write": 50}},
             "path": {"cwd": "/w/sub"}}
        u = {"role": "user"}
        con.execute("INSERT INTO message VALUES ('m1','ses1',1758189600000,0,?)", (json.dumps(a),))
        con.execute("INSERT INTO message VALUES ('m2','ses1',1758189600000,0,?)", (json.dumps(u),))
        con.commit(); con.close()
        return db

    def test_reads_assistant_rows_only(self, tmp_path):
        recs = list(sources.read_opencode(self._db(tmp_path)))
        assert len(recs) == 1
        r = recs[0]
        assert (r.input, r.output, r.cache_read, r.cache_write_5m) == (100, 25, 400, 50)
        assert r.model == "anthropic:claude-sonnet-5" and r.title == "Refactor auth" and r.project == "/w/sub"

    def test_missing_db_is_empty_not_an_error(self, tmp_path):
        assert list(sources.read_opencode(tmp_path / "nope.db")) == []


class TestPricing:
    def test_opus_5_message(self):
        # 1M of each bucket at Opus 5 rates: $5 in, $25 out, 1.25x / 2x writes, 0.1x reads
        c = pricing.cost("claude-opus-5", input_tokens=1_000_000, output_tokens=1_000_000,
                         cache_read=1_000_000, cache_write_5m=1_000_000, cache_write_1h=1_000_000)
        assert c == pytest.approx(5 + 25 + 0.5 + 6.25 + 10)

    def test_fable_cache_read_is_flat_quarter(self):
        assert pricing.cost("claude-fable-5-1", cache_read=1_000_000) == pytest.approx(0.25)

    def test_longest_prefix_wins(self):
        assert pricing._family("claude-opus-4-8") == "claude-opus-4-8"
        assert pricing._family("claude-opus-4-1-20250805") == "claude-opus-4-1"
        assert pricing._family("claude-sonnet-5") == "claude-sonnet-5"

    def test_unknown_model_is_none_not_zero(self):
        assert pricing.cost("opencode:big-pickle", output_tokens=10) is None
        assert not pricing.is_priced("big-pickle")

    def test_provider_prefix_is_stripped(self):
        assert pricing.is_priced("anthropic/claude-opus-5")

    def test_uncached_is_never_less_than_cached(self):
        kw = dict(input_tokens=10, output_tokens=10, cache_read=1000, cache_write_5m=100)
        assert pricing.uncached_cost("claude-opus-5", **kw) >= pricing.cost("claude-opus-5", **kw)


class TestReport:
    def _rec(self, **kw):
        base = dict(source="claude-code", session_id="s", project="/p", model="claude-opus-5",
                    at=datetime(2026, 9, 18, 10, tzinfo=timezone.utc), output=1000)
        base.update(kw)
        return sources.Record(**base)

    def test_render_sums_and_mentions_savings(self):
        recs = [self._rec(cache_read=1_000_000), self._rec(session_id="t", model="opencode:big-pickle", output=5)]
        out = report.render(recs, days=7, since=datetime(2026, 9, 12, tzinfo=timezone.utc),
                            until=datetime(2026, 9, 19, tzinfo=timezone.utc), color=False)
        assert "2 messages" in out and "2 sessions" in out
        assert "Caching saved" in out
        assert "Not priced" in out and "big-pickle" in out

    def test_empty_window(self):
        out = report.render([], days=7, since=datetime(2026, 9, 12, tzinfo=timezone.utc),
                            until=datetime(2026, 9, 19, tzinfo=timezone.utc), color=False)
        assert "No agent sessions" in out

    def test_json_totals(self):
        recs = [self._rec(cache_read=100, input=10), self._rec(output=5)]
        d = report.to_json(recs, since=datetime(2026, 9, 12, tzinfo=timezone.utc),
                           until=datetime(2026, 9, 19, tzinfo=timezone.utc))
        assert d["total"]["messages"] == 2 and d["total"]["total_tokens"] == 1115
        assert d["total"]["cost_usd"] == pytest.approx(pricing.cost("claude-opus-5", input_tokens=10, cache_read=100, output_tokens=1005), abs=1e-4)
