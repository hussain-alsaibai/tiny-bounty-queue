# tiny-bounty-queue

> **Score, filter, and dispatch GitHub bounty issues — without the framework.**

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](tiny_bounty_queue.py)
[![Part of the tiny-* ecosystem](https://img.shields.io/badge/tiny--*-ecosystem-purple.svg)](#ecosystem)
[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](tiny_bounty_queue.py)

`tiny-bounty-queue` is a single-file, zero-dependency library for **scoring,
filtering, queueing, and dispatching** GitHub bounty issues. It is designed
for autonomous coding agents that crawl bounty boards (Algora, Opire,
Gitcoin, label-based bounties on OSS repos) and need a deterministic,
auditable way to decide *which issue to work next, in what order, under
what budget*.

It is part of the **tiny-\* ecosystem** — single-file, stdlib-only Python
libraries that ship in &lt;700 LOC and are auditable in a single sitting.

## Why this exists

Most "AI bounty hunter" bots share the same fatal flaw: they sort bounties
by reward and call it a day. That pattern lands agents on duplicate PRs,
misses issues they could actually close, and burns budget on scams.

`tiny-bounty-queue` enforces the three rules every bounty-hunting agent
gets wrong:

1. **Skill-fit before reward.** A $500 Rust bounty scores lower than a $50
   Python bounty if you know Python and not Rust. Skill coverage is
   *geometric*: missing one of three required skills drops the score
   sharply even if the other two are strong.
2. **Lockability before freshness.** A 30-day-old issue with 30 comments
   is almost certainly already being worked. A 1-hour-old issue with 0
   comments is open. Lockability scores this directly.
3. **Budget before ambition.** A $5 plan that ships 1 PR is worth more
   than a $50 plan that ships 0. The queue enforces a dollar ceiling on
   the dispatch plan.

## ✨ Features

- **Transparent scoring** — `ScoreBreakdown` returns 6 component scores
  plus matched/missing skills. Every score is explainable in one line.
- **Built-in scam filter** — blocked phrases, banned repos, required
  labels, max age, max difficulty, min reward.
- **Pluggable weights** — adjust capability/reward/freshness/difficulty/
  lockability weights per agent profile.
- **Budget-aware dispatch** — generates a ranked plan that fits a USD
  budget; cumulative cost tracked per slot.
- **GitHub issue parser** — `parse_github_issue()` extracts reward USD
  from common formats (`Bounty: $250`, `Reward $40`, etc.).
- **JSON-serializable** — `to_json()` for audit logs and persistence.
- **Zero dependencies** — one file, stdlib only, 14 KB.

## 🚀 Quick Start

```python
from tiny_bounty_queue import (
    BountyIssue, Scorer, Filter, Queue, Dispatch,
)

# 1. Define your agent's skill profile.
agent_profile = {
    "python": 0.9, "go": 0.5, "qdrant": 0.4,
    "webhooks": 0.7, "docs": 0.95,
}

# 2. Build a scorer.
scorer = Scorer(capability=agent_profile)

# 3. Build a filter (anti-scam, repo bans, age limits).
flt = Filter(
    blocked_phrases=("seed phrase", "send me your", "trust me"),
    max_age_hours=24 * 60,
    forbid_repos=frozenset({"scam/repo"}),
)

# 4. Wire a queue with a budget.
queue = Queue(scorer=scorer, flt=flt, max_concurrent=1, budget_usd=300.0)

# 5. Run dispatch — get a ranked plan.
issues = [
    BountyIssue(
        id="arakoodev/EdgeChains#273",
        repo="arakoodev/EdgeChains", number=273,
        title="feat: Qdrant vector store adapter",
        labels=("bounty", "enhancement"),
        reward_usd=120.0, difficulty=0.55,
        required_skills=frozenset({"python", "qdrant"}),
        age_hours=4.0, comments=2,
    ),
    # ...
]

dispatch = Dispatch(queue=queue)
plan = dispatch.plan(issues)
print(dispatch.explain(plan))
```

Sample output:

```
Dispatch plan: 2 items, $10.00 est.
  [00] arakoodev/EdgeChains#273                  $   120 score=0.589 cap=0.65 fresh=0.96 lock=0.95 missing=[qdrant] reasons=missing 1 skill(s)
  [01] old/repo#42                               $    50 score=0.412 cap=0.85 fresh=0.70 lock=0.40 missing=[-] reasons=clean
```

## 🧮 Score formula

```
total = (w_cap * capability   # skill coverage (geometric, missing-skill penalty)
       + w_rew * reward       # log-scaled USD ($50 -> ~0.63)
       + w_fresh * freshness  # exp half-life, default 72h
       + w_diff * difficulty  # peak at sweet-spot (default 0.55)
       + w_lock * lockability # newer + fewer comments = higher
       ) * (1 - penalty)
```

Default weights: `capability=0.35, reward=0.20, freshness=0.15, difficulty=0.10, lockability=0.20`.

Penalty: each missing required skill adds 30% scaled by missing/total;
existing PR / assignee adds 50%; draft adds 10%.

## 🛡️ Anti-scam defaults

`Filter` ships with sensible defaults and lets you opt into stricter rules:

| Default | What it does |
|---|---|
| `require_actionable=True` | drops locked, drafted, assigned, has-PR, $0-reward issues |
| `max_age_hours=24*90` | drops bounties older than 90 days |
| `max_difficulty=1.0` | drops nothing by default (set 0.85 to be selective) |
| `blocked_phrases=()` | empty; supply your own scam phrase list |
| `forbid_repos=frozenset()` | empty; supply your own list |
| `require_labels=frozenset()` | empty; set `frozenset({"bounty"})` to enforce |

OpenClaw uses this with `blocked_phrases=("seed phrase", "send me your",
"trust me", "DM me", "wire me")` and `forbid_repos=frozenset()` populated
from a daily-scanned scam-repo feed.

## 📊 Why it matters for agents

Most agent bounty loops fail because **they conflate scoring with
dispatching**. A good score is not a good plan — a queue that doesn't
respect the budget, doesn't deduplicate against existing PRs, or doesn't
penalize closed-but-still-listed issues will burn cycles.

`tiny-bounty-queue` keeps the two stages separate:

- `Scorer.score(issue)` → 0..1 with full breakdown.
- `Queue.admit(issues)` → kept_scored + dropped_with_reason.
- `Dispatch.plan(issues)` → ranked plan within budget.

Drop-in for OpenClaw's `daily-bounty-work` cron, or wire to Algora's
graphQL endpoint, or scrape GitHub labels directly.

## 🧪 Testing

```bash
cd tiny-bounty-queue
python -m unittest tests.test_tiny_bounty_queue -v
```

38 unit tests covering scoring, filtering, dispatch, GitHub parsing, and
JSON serialization.

## 🌱 Ecosystem

`tiny-bounty-queue` sits next to:

| Repo | Job |
|------|-----|
| [`tiny-cache`](https://github.com/hussain-alsaibai/fast-cache) | sub-microsecond in-memory cache |
| [`tiny-router`](https://github.com/hussain-alsaibai/tiny-router) | HTTP router with async + DI |
| [`tiny-validator`](https://github.com/hussain-alsaibai/tiny-validator) | JSON Schema bridge + batch validation |
| [`tiny-config`](https://github.com/hussain-alsaibai/tiny-config) | layered config + secrets + env interp |
| [`tiny-log`](https://github.com/hussain-alsaibai/tiny-log) | structured logging, zero deps |
| [`tiny-cli`](https://github.com/hussain-alsaibai/tiny-cli) | agent-friendly CLI toolkit |
| [`tiny-agent`](https://github.com/hussain-alsaibai/tiny-agent) | zero-dep agent framework |
| [`tiny-context`](https://github.com/hussain-alsaibai/tiny-context) | LLM context window manager |
| [`tiny-budget`](https://github.com/hussain-alsaibai/tiny-budget) | token-budget governor |
| [`tiny-circuit`](https://github.com/hussain-alsaibai/tiny-circuit) | circuit breaker |
| [`tiny-eval`](https://github.com/hussain-alsaibai/tiny-eval) | eval suite + regression tracker |

## License

MIT © 2026 Hussain Al-Saibai
