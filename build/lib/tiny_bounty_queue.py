"""
tiny-bounty-queue — zero-dependency bounty issue scoring, queueing, and dispatch.

A standalone, single-file library for autonomous coding agents that crawl GitHub
bounties (Algora, Opire, Gitcoin, label-based "bounty" issues on OSS repos), score
them against an agent's capability profile, and dispatch them to a worker pool with
policy gates. Designed to slot into OpenClaw's daily-bounty-work loop or any agent
that wants deterministic, auditable bounty triage.

Design goals
------------
- Zero deps (stdlib only).
- One file. ~700 LOC. Readable in one sitting.
- Deterministic: same input -> same score, same decision.
- Auditable: every score breakdown is exposed via .explain().
- Pluggable: score function takes any callable, scorer is replaceable.

Core concepts
-------------
- BountyIssue: a normalized bounty (issue, repo, reward, tags, age, signals).
- Scorer: weights capability, freshness, reward, difficulty, lockability into 0..1.
- Filter: skip if blacklist matches, label missing, repo banned, PR exists, etc.
- Queue: priority queue with admission control and budget tracking.
- Dispatch: returns a plan (issues to work, in order) without side effects.

Usage
-----
    from tiny_bounty_queue import (
        BountyIssue, Scorer, Filter, Queue, Dispatch,
    )

    issues = [
        BountyIssue(
            id="arakoodev/EdgeChains#273",
            repo="arakoodev/EdgeChains",
            number=273,
            title="feat: add Qdrant vector store adapter",
            labels=["bounty", "enhancement"],
            reward_usd=120.0,
            difficulty=0.5,
            required_skills={"python", "qdrant", "vector-db"},
            age_hours=10.0,
            has_existing_pr=False,
            is_locked=False,
        ),
        ...
    ]

    scorer = Scorer(
        capability={"python": 0.9, "qdrant": 0.4, "vector-db": 0.7},
        reward_weight=0.35,
        freshness_weight=0.20,
        difficulty_weight=0.20,
        lockability_weight=0.25,
    )

    queue = Queue(
        scorer=scorer,
        filter=Filter(forbid_repos={"blocked/repo"}, max_difficulty=0.85),
        max_concurrent=1,
        budget_usd=200.0,
    )

    dispatch = Dispatch(queue=queue)
    plan = dispatch.plan(issues, agent_profile={"python": 0.9, "qdrant": 0.4, "vector-db": 0.7})

    for item in plan:
        print(item.score, item.issue.id, item.issue.reward_usd)

License: MIT
"""
from __future__ import annotations

import heapq
import json
import math
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BountyIssue:
    """A normalized bounty issue, ready for scoring."""
    id: str                    # e.g. "owner/repo#123"
    repo: str                  # "owner/repo"
    number: int
    title: str
    labels: tuple[str, ...] = ()
    body: str = ""
    reward_usd: float = 0.0
    difficulty: float = 0.5    # 0..1, higher = harder
    required_skills: frozenset[str] = field(default_factory=frozenset)
    age_hours: float = 0.0
    has_existing_pr: bool = False
    is_locked: bool = False
    has_assignee: bool = False
    is_draft: bool = False
    comments: int = 0
    url: str = ""

    @property
    def is_actionable(self) -> bool:
        return (
            not self.is_locked
            and not self.has_existing_pr
            and not self.has_assignee
            and not self.is_draft
            and self.reward_usd > 0
        )


@dataclass(frozen=True)
class ScoreBreakdown:
    """Why a BountyIssue got the score it did. Stable, JSON-serializable."""
    capability: float
    reward: float
    freshness: float
    difficulty: float
    lockability: float
    penalty: float
    total: float
    matched_skills: tuple[str, ...]
    missing_skills: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ScoredBounty:
    issue: BountyIssue
    score: ScoreBreakdown

    @property
    def rank_key(self) -> tuple[float, float]:
        """Negative score so heapq (min-heap) returns highest score first."""
        return (-self.score.total, -self.issue.reward_usd)


@dataclass(frozen=True)
class QueuedBounty:
    """An item admitted to the queue with a plan slot."""
    item: ScoredBounty
    slot: int
    estimated_cost_usd: float
    cumulative_cost_usd: float


@dataclass
class Filter:
    """Hard-reject rules. Any matched rule -> drop."""
    forbid_repos: frozenset[str] = field(default_factory=frozenset)
    forbid_labels: frozenset[str] = field(default_factory=frozenset)
    require_labels: frozenset[str] = field(default_factory=frozenset)
    min_reward_usd: float = 0.0
    max_difficulty: float = 1.0
    max_age_hours: float = 24 * 90      # 90 days default
    require_actionable: bool = True
    blocked_phrases: tuple[str, ...] = ()  # scam/spam filter
    blocked_repos_substrings: tuple[str, ...] = ()

    def keep(self, issue: BountyIssue) -> tuple[bool, str]:
        if issue.repo in self.forbid_repos:
            return False, "forbidden_repo"
        if self.forbid_labels & set(issue.labels):
            return False, "forbidden_label"
        if self.require_labels and not (self.require_labels & set(issue.labels)):
            return False, "missing_required_label"
        if issue.reward_usd < self.min_reward_usd:
            return False, "below_min_reward"
        if issue.difficulty > self.max_difficulty:
            return False, "above_max_difficulty"
        if issue.age_hours > self.max_age_hours:
            return False, "too_old"
        if self.require_actionable and not issue.is_actionable:
            return False, "not_actionable"
        body_low = (issue.body or "").lower()
        title_low = issue.title.lower()
        for phrase in self.blocked_phrases:
            if phrase.lower() in body_low or phrase.lower() in title_low:
                return False, f"blocked_phrase:{phrase}"
        for sub in self.blocked_repos_substrings:
            if sub.lower() in issue.repo.lower():
                return False, f"blocked_substring:{sub}"
        return True, "kept"


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

Capability = dict[str, float]  # skill -> 0..1


class Scorer:
    """Weighted, transparent bounty scorer.

    Components (each 0..1):
      - capability: mean of agent's skill scores for the issue's required_skills.
                    Missing skills drop capability sharply.
      - reward:     normalized reward, log-scaled so $5 vs $25 is meaningful.
      - freshness:  exp-decay on age_hours; half-life 72h by default.
      - difficulty: penalty if the issue is too easy (boring) or too hard.
      - lockability: bonus if the issue is fresh and unlikely to be claimed.
      - penalty:    static 1.0 -> 0.0 multiplied by penalties.

    Total = w_cap*cap + w_rew*rew + w_fresh*fresh + w_diff*diff + w_lock*lock
            then multiplied by (1 - penalty).
    """

    DEFAULT_WEIGHTS: dict[str, float] = {
        "capability": 0.35,
        "reward":     0.20,
        "freshness":  0.15,
        "difficulty": 0.10,
        "lockability":0.20,
    }

    def __init__(
        self,
        capability: Capability,
        reward_weight: float = 0.20,
        freshness_weight: float = 0.15,
        difficulty_weight: float = 0.10,
        lockability_weight: float = 0.20,
        capability_weight: float = 0.35,
        reward_log_base: float = 50.0,
        freshness_half_life_hours: float = 72.0,
        sweet_spot_difficulty: float = 0.55,
        difficulty_bandwidth: float = 0.30,
    ) -> None:
        s = reward_weight + freshness_weight + difficulty_weight + lockability_weight + capability_weight
        if abs(s - 1.0) > 0.001:
            raise ValueError(f"weights must sum to 1.0, got {s}")
        self.capability = dict(capability)
        self.w_cap = capability_weight
        self.w_rew = reward_weight
        self.w_fresh = freshness_weight
        self.w_diff = difficulty_weight
        self.w_lock = lockability_weight
        self.reward_log_base = reward_log_base
        self.freshness_half_life = freshness_half_life_hours
        self.sweet_spot = sweet_spot_difficulty
        self.difficulty_bandwidth = difficulty_bandwidth

    # ----- component scorers ------------------------------------------------
    def _cap_score(self, issue: BountyIssue) -> tuple[float, tuple[str, ...], tuple[str, ...]]:
        if not issue.required_skills:
            return 0.5, (), ()  # neutral if no skills declared
        matched, missing = [], []
        scores: list[float] = []
        for skill in issue.required_skills:
            v = self.capability.get(skill, 0.0)
            if v > 0:
                matched.append(skill)
                scores.append(v)
            else:
                missing.append(skill)
        if not scores:
            return 0.0, (), tuple(missing)
        # Use geometric mean weighted by recency-of-skill. Simple mean is fine.
        cap = sum(scores) / len(scores)
        # Sharper drop when ANY required skill is missing.
        coverage = len(matched) / (len(matched) + len(missing))
        return cap * coverage, tuple(matched), tuple(missing)

    def _reward_score(self, issue: BountyIssue) -> float:
        if issue.reward_usd <= 0:
            return 0.0
        # log1p scaled so $reward_log_base -> 0.63
        return 1.0 - math.exp(-issue.reward_usd / self.reward_log_base)

    def _freshness_score(self, issue: BountyIssue) -> float:
        if issue.age_hours <= 0:
            return 1.0
        # half-life decay
        return 0.5 ** (issue.age_hours / self.freshness_half_life)

    def _difficulty_score(self, issue: BountyIssue) -> float:
        # Peak at sweet_spot, drops linearly to 0 at +/- bandwidth.
        delta = abs(issue.difficulty - self.sweet_spot)
        if delta >= self.difficulty_bandwidth:
            return 0.0
        return 1.0 - (delta / self.difficulty_bandwidth)

    def _lockability_score(self, issue: BountyIssue) -> float:
        if issue.has_existing_pr or issue.has_assignee or issue.is_locked:
            return 0.0
        if issue.is_draft:
            return 0.4  # draft issues often turn into PRs fast
        # Newer + few comments = more likely open
        age_factor = 1.0 if issue.age_hours < 24 else max(0.0, 1.0 - (issue.age_hours - 24) / (24 * 7))
        comment_factor = max(0.0, 1.0 - min(1.0, issue.comments / 20.0))
        return 0.5 * age_factor + 0.5 * comment_factor

    # ----- public API --------------------------------------------------------
    def score(self, issue: BountyIssue) -> ScoreBreakdown:
        cap, matched, missing = self._cap_score(issue)
        rew = self._reward_score(issue)
        fresh = self._freshness_score(issue)
        diff = self._difficulty_score(issue)
        lock = self._lockability_score(issue)

        base = (
            self.w_cap * cap
            + self.w_rew * rew
            + self.w_fresh * fresh
            + self.w_diff * diff
            + self.w_lock * lock
        )

        # Penalty: missing critical skills
        penalty = 0.0
        reasons: list[str] = []
        if missing:
            penalty += 0.30 * (len(missing) / (len(matched) + len(missing)))
            reasons.append(f"missing {len(missing)} skill(s)")
        if issue.has_existing_pr:
            penalty += 0.50
            reasons.append("existing PR")
        if issue.has_assignee:
            penalty += 0.50
            reasons.append("has assignee")
        if issue.is_draft:
            penalty += 0.10
            reasons.append("draft")
        penalty = min(1.0, penalty)

        total = max(0.0, base * (1.0 - penalty))

        return ScoreBreakdown(
            capability=round(cap, 4),
            reward=round(rew, 4),
            freshness=round(fresh, 4),
            difficulty=round(diff, 4),
            lockability=round(lock, 4),
            penalty=round(penalty, 4),
            total=round(total, 4),
            matched_skills=matched,
            missing_skills=missing,
            reasons=tuple(reasons) or ("clean",),
        )

    def score_all(self, issues: Iterable[BountyIssue]) -> list[ScoredBounty]:
        return [ScoredBounty(issue=i, score=self.score(i)) for i in issues]


# ---------------------------------------------------------------------------
# Queue + dispatch
# ---------------------------------------------------------------------------

@dataclass
class Queue:
    scorer: Scorer
    flt: Filter = field(default_factory=Filter)
    max_concurrent: int = 1
    budget_usd: float = float("inf")
    estimated_cost_per_issue_usd: float = 5.0

    def admit(self, issues: Iterable[BountyIssue]) -> tuple[list[ScoredBounty], list[tuple[BountyIssue, str]]]:
        """Filter + score. Returns (kept_scored, dropped_with_reason)."""
        kept: list[ScoredBounty] = []
        dropped: list[tuple[BountyIssue, str]] = []
        for issue in issues:
            ok, reason = self.flt.keep(issue)
            if not ok:
                dropped.append((issue, reason))
                continue
            s = self.scorer.score(issue)
            if s.total <= 0.0:
                dropped.append((issue, "score_zero"))
                continue
            kept.append(ScoredBounty(issue=issue, score=s))
        return kept, dropped


class Dispatch:
    """Decides which bounties to work and in what order.

    Returns a list[QueuedBounty] sorted by priority and within budget.
    """
    def __init__(self, queue: Queue) -> None:
        self.queue = queue

    def plan(self, issues: Iterable[BountyIssue], agent_profile: Capability | None = None) -> list[QueuedBounty]:
        # Optionally retune the scorer for this agent profile (does not mutate queue).
        if agent_profile is not None:
            old = self.queue.scorer.capability
            try:
                self.queue.scorer.capability = dict(agent_profile)
                kept, dropped = self.queue.admit(issues)
            finally:
                self.queue.scorer.capability = old
        else:
            kept, dropped = self.queue.admit(issues)

        # Heap by rank_key, then stable on id
        heap: list[tuple[Any, int, ScoredBounty]] = []
        counter = 0
        for sb in kept:
            counter += 1
            entry = (sb.rank_key, counter, sb)
            heapq.heappush(heap, entry)

        out: list[QueuedBounty] = []
        cumulative = 0.0
        slot = 0
        max_slots = self.queue.max_concurrent * 50  # plan horizon
        while heap and slot < max_slots and cumulative + self.queue.estimated_cost_per_issue_usd <= self.queue.budget_usd:
            _, _, sb = heapq.heappop(heap)
            cost = self.queue.estimated_cost_per_issue_usd
            cumulative += cost
            out.append(QueuedBounty(item=sb, slot=slot, estimated_cost_usd=cost, cumulative_cost_usd=cumulative))
            slot += 1
        return out

    def explain(self, plan: list[QueuedBounty]) -> str:
        """Return a human-readable audit string."""
        lines = [f"Dispatch plan: {len(plan)} items, ${plan[-1].cumulative_cost_usd if plan else 0:.2f} est."]
        for q in plan:
            sb = q.item
            s = sb.score
            mins = ",".join(s.missing_skills) or "-"
            lines.append(
                f"  [{q.slot:02d}] {sb.issue.id:42s} ${sb.issue.reward_usd:6.0f} "
                f"score={s.total:.3f} cap={s.capability:.2f} fresh={s.freshness:.2f} "
                f"lock={s.lockability:.2f} missing=[{mins}] reasons={','.join(s.reasons)}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Convenience: GitHub-flavored issue parser
# ---------------------------------------------------------------------------

_LABEL_RE = re.compile(r'"label[s]?"\s*:\s*\[(.*?)\]', re.IGNORECASE | re.DOTALL)
_REWARD_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)")
_REWARD_AFTER_RE = re.compile(
    r"(?:bounty|reward|payout|prize)\s*[:\-]?\s*\$\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


def parse_github_issue(
    repo: str,
    number: int,
    title: str,
    body: str,
    labels: Iterable[str] | None = None,
    state: str = "open",
    has_existing_pr: bool = False,
    is_locked: bool = False,
    has_assignee: bool = False,
    is_draft: bool = False,
    comments: int = 0,
    age_hours: float | None = None,
    url: str = "",
) -> BountyIssue:
    """Parse a GitHub issue (or issue payload dict) into a BountyIssue.

    Tries hard to extract reward_usd from common bounty formats:
      "$120 bounty"
      "Reward: $250"
      "Prize: $50"
      Just "$40" anywhere in body.
    """
    label_set: tuple[str, ...] = tuple(labels) if labels is not None else tuple(_LABEL_RE.findall(body or "")[0].split(',')) if _LABEL_RE.search(body or "") else ()

    reward = 0.0
    m = _REWARD_AFTER_RE.search(body or "") or _REWARD_AFTER_RE.search(title or "")
    if m:
        reward = float(m.group(1))
    else:
        m = _REWARD_RE.search(body or "")
        if m:
            reward = float(m.group(1))

    if age_hours is None:
        # heuristic: assume fresh if not provided
        age_hours = 1.0

    return BountyIssue(
        id=f"{repo}#{number}",
        repo=repo,
        number=number,
        title=title or "",
        labels=label_set,
        body=body or "",
        reward_usd=reward,
        difficulty=0.5,
        required_skills=frozenset(),
        age_hours=max(0.0, age_hours),
        has_existing_pr=has_existing_pr,
        is_locked=(state == "locked") or is_locked,
        has_assignee=has_assignee,
        is_draft=is_draft,
        comments=max(0, comments),
        url=url,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def to_json(obj: Any) -> str:
    """Serialize ScoredBounty, QueuedBounty, or list of them to JSON."""
    def default(o: Any) -> Any:
        if isinstance(o, ScoredBounty):
            return {"issue": asdict(o.issue), "score": asdict(o.score)}
        if isinstance(o, QueuedBounty):
            return {"slot": o.slot, "estimated_cost_usd": o.estimated_cost_usd,
                    "cumulative_cost_usd": o.cumulative_cost_usd,
                    "item": default(o.item)}
        if isinstance(o, frozenset):
            return sorted(o)
        if isinstance(o, BountyIssue):
            return asdict(o)
        if isinstance(o, ScoreBreakdown):
            return asdict(o)
        raise TypeError(f"not serializable: {type(o)}")
    return json.dumps(obj, default=default, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# CLI / quick demo
# ---------------------------------------------------------------------------

def _demo() -> None:
    issues = [
        BountyIssue(
            id="arakoodev/EdgeChains#273", repo="arakoodev/EdgeChains", number=273,
            title="feat: Qdrant vector store adapter",
            labels=("bounty", "enhancement"),
            reward_usd=120.0, difficulty=0.55,
            required_skills=frozenset({"python", "qdrant"}),
            age_hours=4.0, has_existing_pr=False, comments=2,
        ),
        BountyIssue(
            id="go-gitea/gitea#4898", repo="go-gitea/gitea", number=4898,
            title="fix: webhook delivery on 5xx",
            labels=("bounty", "bug"),
            reward_usd=350.0, difficulty=0.65,
            required_skills=frozenset({"go", "webhooks"}),
            age_hours=240.0, has_existing_pr=False, comments=8,
        ),
        BountyIssue(
            id="scam/repo#1", repo="scam/repo", number=1,
            title="$5000 send me your seed phrase",
            labels=("bounty",), reward_usd=5000.0, difficulty=0.1,
            required_skills=frozenset({"trust"}),
            age_hours=0.5, has_existing_pr=False,
        ),
        BountyIssue(
            id="old/stale#5", repo="old/stale", number=5,
            title="Update README",
            labels=("bounty", "docs"),
            reward_usd=10.0, difficulty=0.05,
            required_skills=frozenset({"docs"}),
            age_hours=24 * 365, has_existing_pr=False,
        ),
    ]

    scorer = Scorer(
        capability={"python": 0.9, "go": 0.5, "qdrant": 0.4, "webhooks": 0.7, "docs": 0.95},
    )
    flt = Filter(
        blocked_phrases=("seed phrase", "send me your", "trust me"),
        max_age_hours=24 * 60,
    )
    q = Queue(scorer=scorer, flt=flt, max_concurrent=1, budget_usd=300.0)
    plan = Dispatch(queue=q).plan(issues)
    print(Dispatch(queue=q).explain(plan))


if __name__ == "__main__":  # pragma: no cover
    _demo()
