"""Plan labels from credential subscription fields."""

from __future__ import annotations

import json

from claude_swap import plan_tier
from claude_swap.json_output import account_row
from claude_swap.models import AccountSnapshot
from claude_swap.usage_store import UsageEntry


class TestLabel:
    def test_known_tiers(self):
        assert plan_tier.label("max", "default_claude_max_20x") == "Max 20x"
        assert plan_tier.label("max", "default_claude_max_5x") == "Max 5x"
        assert plan_tier.label("pro", "default_claude_pro") == "Pro"
        assert plan_tier.label("team", "default_claude_max_5x") == "Team premium seat"
        assert plan_tier.label("team", "default_raven") == "Team standard seat"

    def test_unknown_tier_keeps_the_raw_name(self):
        assert plan_tier.label("team", "default_condor_7x") == "Team condor 7x"
        assert plan_tier.label("enterprise", None) == "Enterprise"
        assert plan_tier.label(None, "default_claude_max_20x") == "Max 20x"
        assert plan_tier.label(None, None) == ""


class TestFromCredentials:
    def test_reads_the_oauth_block(self):
        creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "x",
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_20x",
            }
        })
        assert plan_tier.from_credentials(creds) == "Max 20x"

    def test_blank_for_api_keys_and_garbage(self):
        assert plan_tier.from_credentials("") == ""
        assert plan_tier.from_credentials("sk-ant-api03-abc") == ""
        assert plan_tier.from_credentials(json.dumps({"apiKey": "x"})) == ""


class TestSurfaces:
    def test_json_row_carries_plan_only_when_known(self):
        row = account_row(1, "a@x.org", "Org", "org-1", True, None, plan="Max 20x")
        assert row["plan"] == "Max 20x"
        assert "plan" not in account_row(1, "a@x.org", "Org", "org-1", True, None)

    def test_snapshot_tag_includes_plan(self):
        acc = AccountSnapshot(
            number="1", email="a@x.org", org_name="Org", org_uuid="org-1",
            is_active=True, kind="oauth", switchable=True, usage=UsageEntry(),
            plan="Team premium seat",
        )
        assert acc.display_tag == "Org · Team premium seat"
        acc = AccountSnapshot(
            number="1", email="a@x.org", org_name="", org_uuid="",
            is_active=True, kind="oauth", switchable=True, usage=UsageEntry(),
        )
        assert acc.display_tag == "personal"
