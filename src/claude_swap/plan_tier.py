"""Readable plan labels from a Claude credential's subscription fields."""

from __future__ import annotations

import json

_TIER_LABELS = {
    "default_claude_max_20x": "Max 20x",
    "default_claude_max_5x": "Max 5x",
    "default_claude_pro": "Pro",
    "default_raven": "standard",
}

_TEAM_TIER_LABELS = {
    "default_claude_max_5x": "premium seat",
    "default_raven": "standard seat",
}

_SUBSCRIPTION_LABELS = {
    "max": "Max",
    "pro": "Pro",
    "team": "Team",
    "enterprise": "Enterprise",
    "free": "Free",
}


def label(subscription: str | None, tier: str | None) -> str:
    """``("team", "default_claude_max_5x")`` -> ``Team premium seat``."""
    sub = (subscription or "").strip().lower()
    tier = (tier or "").strip()
    if sub == "team":
        seat = _TEAM_TIER_LABELS.get(tier)
        if seat:
            return f"Team {seat}"
    if tier in _TIER_LABELS and sub in ("max", "pro", ""):
        return _TIER_LABELS[tier]
    parts = []
    if sub:
        parts.append(_SUBSCRIPTION_LABELS.get(sub, sub.title()))
    if tier:
        raw = tier.removeprefix("default_").replace("_", " ")
        if raw and raw.lower() not in {p.lower() for p in parts}:
            parts.append(raw)
    return " ".join(parts)


def from_credentials(creds: str | dict | None) -> str:
    """The plan label carried by a credentials JSON blob, or ``""``."""
    if not creds:
        return ""
    data = creds
    if isinstance(creds, str):
        try:
            data = json.loads(creds)
        except ValueError:
            return ""
    if not isinstance(data, dict):
        return ""
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return ""
    return label(oauth.get("subscriptionType"), oauth.get("rateLimitTier"))


def with_plan(tag: str, plan: str) -> str:
    return f"{tag} · {plan}" if plan else tag
