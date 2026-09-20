"""API list prices, so a subscription user can see what their week would have cost.

Prices are USD per million tokens. Cache writes cost 1.25x input for the
5-minute TTL and 2x for the 1-hour TTL; cache reads cost 0.1x input, except on
Claude Fable 5.1 where they are a flat $0.25. Unknown models are reported with
tokens but no dollar figure, never with a guessed one.

Snapshot date is in PRICES_AS_OF. Update the table, bump the date.
"""

from __future__ import annotations

PRICES_AS_OF = "2026-06-24"

# family prefix -> (input, output) per MTok. Longest prefix wins.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-mythos-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus-4-1": (15.00, 75.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-3-5-haiku": (0.80, 4.00),
    # OpenAI, for Codex logs. List prices, standard tier.
    "gpt-5.2-codex": (1.75, 14.00),
    "gpt-5.2": (1.75, 14.00),
    "gpt-5.1-codex": (1.25, 10.00),
    "gpt-5.1": (1.25, 10.00),
    "gpt-5-codex": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5": (1.25, 10.00),
    "o3": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
}

# Cache read as a fraction of input price, by family; default 0.1.
CACHE_READ_OVERRIDE: dict[str, float] = {
    "claude-fable-5-1": 0.025,
    "claude-mythos-5-1": 0.025,
}
OPENAI_CACHE_READ = 0.10  # OpenAI cached input is 10% of input price


def _family(model: str) -> str | None:
    """Longest configured prefix that matches the model id, or None."""
    m = model.lower().split("/")[-1]  # strip provider paths like openrouter/x
    best = None
    for key in PRICES:
        if m.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return best


def is_priced(model: str) -> bool:
    return _family(model) is not None


def cost(model: str, *, input_tokens: int = 0, output_tokens: int = 0,
         cache_read: int = 0, cache_write_5m: int = 0, cache_write_1h: int = 0) -> float | None:
    """USD for one message, or None when the model is not in the table."""
    fam = _family(model)
    if fam is None:
        return None
    inp, out = PRICES[fam]
    read_mult = CACHE_READ_OVERRIDE.get(fam, 0.10)
    per = 1_000_000
    return (
        input_tokens * inp
        + output_tokens * out
        + cache_read * inp * read_mult
        + cache_write_5m * inp * 1.25
        + cache_write_1h * inp * 2.0
    ) / per


def uncached_cost(model: str, *, input_tokens: int = 0, output_tokens: int = 0,
                  cache_read: int = 0, cache_write_5m: int = 0, cache_write_1h: int = 0) -> float | None:
    """What the same message would cost if every input token were billed at full price."""
    fam = _family(model)
    if fam is None:
        return None
    inp, out = PRICES[fam]
    total_in = input_tokens + cache_read + cache_write_5m + cache_write_1h
    return (total_in * inp + output_tokens * out) / 1_000_000
