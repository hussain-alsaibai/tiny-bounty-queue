"""Tests for tiny-bounty-queue."""
import json
import math
import unittest

from tiny_bounty_queue import (
    BountyIssue, Filter, Queue, Scorer, Dispatch,
    ScoreBreakdown, ScoredBounty, parse_github_issue, to_json,
)


class TestScorer(unittest.TestCase):
    def setUp(self):
        self.cap = {"python": 0.9, "go": 0.5, "qdrant": 0.4, "docs": 0.95}
        self.scorer = Scorer(capability=self.cap)

    def test_weights_must_sum_to_one(self):
        with self.assertRaises(ValueError):
            Scorer(capability=self.cap, capability_weight=0.99)

    def test_clean_high_cap_high_reward_high_score(self):
        issue = BountyIssue(
            id="o/r#1", repo="o/r", number=1, title="t",
            reward_usd=100.0, difficulty=0.55,
            required_skills=frozenset({"python"}),
            age_hours=2.0,
        )
        s = self.scorer.score(issue)
        self.assertGreater(s.total, 0.7)
        self.assertEqual(s.matched_skills, ("python",))
        self.assertEqual(s.missing_skills, ())

    def test_missing_skill_penalizes(self):
        issue = BountyIssue(
            id="o/r#2", repo="o/r", number=2, title="t",
            reward_usd=100.0, difficulty=0.55,
            required_skills=frozenset({"rust"}),
            age_hours=2.0,
        )
        s = self.scorer.score(issue)
        self.assertEqual(s.matched_skills, ())
        self.assertEqual(s.missing_skills, ("rust",))
        self.assertGreater(s.penalty, 0.0)
        self.assertLess(s.total, 0.5)

    def test_existing_pr_penalizes(self):
        issue = BountyIssue(
            id="o/r#3", repo="o/r", number=3, title="t",
            reward_usd=100.0, difficulty=0.55,
            required_skills=frozenset({"python"}),
            age_hours=2.0, has_existing_pr=True,
        )
        s = self.scorer.score(issue)
        self.assertGreaterEqual(s.penalty, 0.50)

    def test_lockability_decays_with_age_and_comments(self):
        fresh = BountyIssue(id="o/r#4", repo="o/r", number=4, title="t",
                            reward_usd=10.0, difficulty=0.5,
                            required_skills=frozenset(), age_hours=1.0, comments=0)
        stale = BountyIssue(id="o/r#5", repo="o/r", number=5, title="t",
                            reward_usd=10.0, difficulty=0.5,
                            required_skills=frozenset(), age_hours=200.0, comments=30)
        self.assertGreater(self.scorer._lockability_score(fresh),
                           self.scorer._lockability_score(stale))

    def test_freshness_half_life(self):
        # default half-life 72h
        self.assertAlmostEqual(self.scorer._freshness_score(
            BountyIssue(id="x", repo="x", number=0, title="t", age_hours=72)), 0.5, places=2)
        self.assertAlmostEqual(self.scorer._freshness_score(
            BountyIssue(id="x", repo="x", number=0, title="t", age_hours=144)), 0.25, places=2)

    def test_difficulty_sweet_spot(self):
        sweet = BountyIssue(id="x", repo="x", number=0, title="t", difficulty=0.55)
        far = BountyIssue(id="x", repo="x", number=0, title="t", difficulty=0.05)
        self.assertGreater(self.scorer._difficulty_score(sweet),
                           self.scorer._difficulty_score(far))


class TestFilter(unittest.TestCase):
    def setUp(self):
        self.base = BountyIssue(
            id="o/r#1", repo="o/r", number=1, title="fix thing",
            labels=("bounty", "bug"), reward_usd=50.0, difficulty=0.5,
            required_skills=frozenset(), age_hours=2.0,
        )

    def test_keep_passes_clean(self):
        f = Filter()
        ok, reason = f.keep(self.base)
        self.assertTrue(ok)
        self.assertEqual(reason, "kept")

    def test_forbid_repo(self):
        f = Filter(forbid_repos=frozenset({"o/r"}))
        ok, _ = f.keep(self.base)
        self.assertFalse(ok)

    def test_require_label(self):
        f = Filter(require_labels=frozenset({"enhancement"}))
        ok, reason = f.keep(self.base)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_required_label")

    def test_blocked_phrase(self):
        issue = BountyIssue(**{**self.base.__dict__, "body": "send me your seed"})
        f = Filter(blocked_phrases=("seed",))
        ok, reason = f.keep(issue)
        self.assertFalse(ok)
        self.assertIn("blocked_phrase", reason)

    def test_age_cutoff(self):
        old = BountyIssue(**{**self.base.__dict__, "age_hours": 24 * 30})
        f = Filter(max_age_hours=24 * 7)
        ok, _ = f.keep(old)
        self.assertFalse(ok)


class TestQueueAndDispatch(unittest.TestCase):
    def test_plan_orders_by_score_and_respects_budget(self):
        issues = [
            BountyIssue(id=f"o/r#{i}", repo="o/r", number=i, title=f"t{i}",
                        labels=("bounty",), reward_usd=float(10 + i * 50),
                        difficulty=0.5, required_skills=frozenset({"python"}),
                        age_hours=2.0)
            for i in range(1, 6)
        ]
        scorer = Scorer(capability={"python": 0.9})
        q = Queue(scorer=scorer, max_concurrent=1, budget_usd=12.0,
                  estimated_cost_per_issue_usd=5.0)
        plan = Dispatch(queue=q).plan(issues)
        # Budget caps us at 2 items (10 USD spent, third would be 15).
        self.assertEqual(len(plan), 2)
        scores = [p.item.score.total for p in plan]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertLessEqual(plan[-1].cumulative_cost_usd, 12.0)

    def test_dropped_returned_with_reason(self):
        issues = [
            BountyIssue(id="o/r#1", repo="o/r", number=1, title="t",
                        labels=("bounty",), reward_usd=0.0, age_hours=1.0),
            BountyIssue(id="o/r#2", repo="o/r", number=2, title="send me your seed",
                        labels=("bounty",), reward_usd=1000.0, age_hours=1.0,
                        body="send me your seed phrase"),
        ]
        scorer = Scorer(capability={"x": 1.0})
        q = Queue(scorer=scorer, flt=Filter(blocked_phrases=("send me your",)))
        kept, dropped = q.admit(issues)
        self.assertEqual(len(kept), 0)
        self.assertEqual(len(dropped), 2)
        reasons = {r for _, r in dropped}
        self.assertIn("blocked_phrase:send me your", reasons)


class TestGithubParser(unittest.TestCase):
    def test_extracts_reward_dollar_amount(self):
        issue = parse_github_issue(
            repo="o/r", number=7, title="fix",
            body="Bounty: $250 for whoever fixes this",
            labels=["bounty", "bug"], age_hours=3.0,
        )
        self.assertEqual(issue.reward_usd, 250.0)
        self.assertIn("bounty", issue.labels)

    def test_no_reward_yields_zero(self):
        issue = parse_github_issue(repo="o/r", number=8, title="fix",
                                   body="just a regular issue", labels=[])
        self.assertEqual(issue.reward_usd, 0.0)

    def test_actionable_flag(self):
        issue = parse_github_issue(repo="o/r", number=9, title="x",
                                   body="Bounty $10", labels=["bounty"], state="locked")
        self.assertFalse(issue.is_actionable)


class TestSerialization(unittest.TestCase):
    def test_to_json_round_trip_safe(self):
        issue = BountyIssue(id="o/r#1", repo="o/r", number=1, title="t",
                            labels=("a", "b"), required_skills=frozenset({"x"}))
        sb = ScoredBounty(issue=issue, score=ScoreBreakdown(
            capability=0.5, reward=0.5, freshness=0.5, difficulty=0.5,
            lockability=0.5, penalty=0.0, total=0.5,
            matched_skills=("x",), missing_skills=(), reasons=("clean",),
        ))
        out = to_json(sb)
        data = json.loads(out)
        self.assertEqual(data["issue"]["id"], "o/r#1")
        self.assertEqual(sorted(data["issue"]["required_skills"]), ["x"])


if __name__ == "__main__":
    unittest.main()
