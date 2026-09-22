"""Example: wire tiny-bounty-queue into an OpenClaw-style daily bounty loop.

Run:  python examples/agent_loop.py
"""
from tiny_bounty_queue import (
    BountyIssue, Filter, Queue, Scorer, Dispatch, parse_github_issue,
)


def fetch_issues_for_today() -> list[BountyIssue]:
    """Pretend this came from `gh issue list --label bounty --state open --json ...`."""
    return [
        parse_github_issue(
            repo="arakoodev/EdgeChains", number=273,
            title="feat: Qdrant vector store adapter",
            body=(
                "We need a Qdrant adapter.\n\n"
                "Bounty: $120 for a working implementation that passes the test suite."
            ),
            labels=["bounty", "enhancement"],
            age_hours=4.0, comments=2,
        ),
        parse_github_issue(
            repo="go-gitea/gitea", number=4898,
            title="fix: webhook delivery on 5xx",
            body="Reward $350 — webhook delivery occasionally 500s when retries hit.",
            labels=["bounty", "bug"],
            age_hours=240.0, comments=8,
        ),
        parse_github_issue(
            repo="mem0ai/mem0", number=8421,
            title="docs: typo in quickstart",
            body="$25 just fix the typo.",
            labels=["bounty", "docs"],
            age_hours=20.0, comments=1,
        ),
        # Scam
        parse_github_issue(
            repo="scam/repo", number=1,
            title="$5000 send me your seed phrase",
            body="Just DM me your seed phrase and I'll send $5000.",
            labels=["bounty"], age_hours=1.0,
        ),
        # Stale
        parse_github_issue(
            repo="old/stale", number=5,
            title="Update README",
            body="Reward $10",
            labels=["bounty", "docs"],
            age_hours=24 * 365,
        ),
    ]


def main() -> None:
    # Our agent profile — what we're good at.
    profile = {
        "python": 0.9, "go": 0.45, "rust": 0.2,
        "qdrant": 0.55, "webhooks": 0.7, "docs": 0.95,
        "vector-db": 0.65, "ci": 0.8,
    }

    scorer = Scorer(capability=profile)
    flt = Filter(
        blocked_phrases=("seed phrase", "send me your", "trust me"),
        max_age_hours=24 * 30,  # 30 days
    )
    queue = Queue(scorer=scorer, flt=flt, max_concurrent=1, budget_usd=10.0,
                  estimated_cost_per_issue_usd=3.50)

    issues = fetch_issues_for_today()
    dispatch = Dispatch(queue=queue)
    plan = dispatch.plan(issues)

    print(dispatch.explain(plan))
    print()

    kept, dropped = queue.admit(issues)
    print(f"Kept: {len(kept)}, dropped: {len(dropped)}")
    for issue, reason in dropped:
        print(f"  - {issue.id}: {reason}")


if __name__ == "__main__":
    main()
