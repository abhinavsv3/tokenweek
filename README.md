# tokentab

[![tests](https://github.com/abhinavsv3/tokentab/actions/workflows/test.yml/badge.svg)](https://github.com/abhinavsv3/tokentab/actions/workflows/test.yml)

**Where did my tokens go this week?**

One command. Reads the session logs that Claude Code, Codex and opencode
already keep on your disk, and tells you what you spent, on what, and what
caching saved you. No accounts, no API key, no network, no dependencies.

```bash
uvx tokentab
```

```
tokentab  ·  last 7 days  ·  Sep 12 → Sep 19

  448 messages  ·  4 sessions  ·  claude-code

  TOKENS                      API-EQUIVALENT
  input              1.2k              $0.01
  cache write        4.0M             $45.72
  cache read       144.6M             $71.92
  output           594.3k             $15.70
  ──────────────────────────────────────────
  total            149.2M               $133

  Cache reads were 97% of input. Without caching this would have been $770. Caching saved $636.

  BY DAY
  Fri 18                         ▇▇▇▇▇▇▇▇▇▇▇▇▇▇            $51.35 263 msgs
  Sat 19                         ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇    $82.01 185 msgs

  BY MODEL
  claude-opus-5                  ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇      $119  89%  431 msgs
  claude-fable-5-1               ▇▇▇                       $14.14  11%  17 msgs

  BY PROJECT  (top 8)
  opencode-wide/groundhog        ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇      $119  89%
  yc/opencode-wide               ▇▇                        $12.86  10%

  BY SESSION  (top 8)
  Brainstorm universal app idea… ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇      $128  96%  418 msgs · 45.3h · opencode-wide/groundhog
  Brainstorm useful community t… ▇                          $4.43   3%  16 msgs · 0.4h · yc/opencode-wide
```

That is a real week on one laptop. 149 million tokens, of which 97% were
cache reads. On a subscription you never see this number. It is worth seeing.

## Why

Coding agents burn tokens invisibly. The bill, if there is one, arrives as a
single line. If you are on a subscription there is no line at all, just a rate
limit that hits at 4pm on a Thursday. Every agent already writes a detailed
per-message record of what it used. Nobody reads it.

`tokentab` reads it.

## Install

```bash
uvx tokentab            # run without installing
pipx install tokentab   # or install
pip install tokentab
```

Python 3.10+. No runtime dependencies; the standard library reads JSONL and
SQLite already.

## Usage

```bash
tokentab                     # last 7 days
tokentab --days 30
tokentab --since 2026-09-01
tokentab --all               # everything ever logged
tokentab --by session --top 20
tokentab --source claude-code
tokentab --json | jq .total
```

## What it reads

| Tool | Where | What it uses |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | `message.usage` on assistant rows, deduplicated by message id, cache writes split by TTL |
| Codex CLI | `~/.codex/sessions/**/rollout-*.jsonl` | `token_count` events, as deltas of the session's cumulative usage |
| opencode | `~/.local/share/opencode/opencode.db` | `tokens` on assistant messages, cache read and write already split |

Everything stays on your machine. `tokentab` opens files read-only and never
makes a network request.

## About the dollar figure

The column is labelled **API-equivalent** on purpose. It is what the same
tokens would cost at list API prices, computed per message from the model that
served it:

- input at the model's input rate
- output at the output rate
- cache reads at 0.1x input (0.025x on Claude Fable 5.1)
- cache writes at 1.25x input for the 5-minute TTL, 2x for the 1-hour TTL

If you pay per token, this is close to your bill. If you are on a
subscription, it is what the subscription is absorbing, which is the number
that tells you whether the plan is worth it.

Models without a known list price, such as opencode's free models or anything
served by Ollama, are reported in tokens and marked as not priced. A missing
price is shown as a dash, never guessed as zero.

Prices are a snapshot, dated in the output. Anthropic prices come from the
official rate card. OpenAI prices for Codex models were entered from memory and
should be checked against
[openai.com/api/pricing](https://openai.com/api/pricing) before you rely on
them; a pull request correcting them is welcome.

## Things worth knowing

**Claude Code logs the same API call several times.** Each content block of a
response is its own JSONL row, and every row carries the full `usage` object.
Sum them naively and every number doubles. `tokentab` counts each message id
once. If you build your own reader, this is the bug you will have.

**Codex reports cumulative totals.** Each `token_count` event carries the
running sum for the session, not the increment. Subtracting consecutive
events gives per-turn usage and makes repeated events harmless.

**Cache reads dominate.** In the week above, 97% of input tokens were served
from cache. Agents re-send the whole conversation on every turn; caching is
what makes that affordable. The savings line is the difference between what
you were charged and what you would have been charged without it.

## Not yet

- Cursor, Windsurf, Aider, Gemini CLI: each stores sessions differently. The
  reader interface is three functions in `sources.py`; adding one is an
  afternoon.
- An HTML report you can share.
- Per-turn detail: which single turn was the expensive one.

## License

MIT.
