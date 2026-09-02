"""L4 — the console.

Reads only through the SDK and the API read models; it never touches a ledger file or a broker directly.
The Streamlit app is a disposable debugging tool for the team and is never shown to a customer
(Master Plan C11).
"""

from __future__ import annotations

from typing import Any

__all__ = ["decision_feed", "decision_detail", "audit_timeline", "policy_diff_view"]


def decision_feed(client: Any, *, limit: int = 50) -> list[dict[str, Any]]:
    """Rows for the decision feed: verdict, agent, policy version, quantities, chain position."""
    raise NotImplementedError("L4 implements this in Sprint 2")


def decision_detail(client: Any, decision_id: str) -> dict[str, Any]:
    """One decision as a case file: every check with its threshold, actual value and distance."""
    raise NotImplementedError("L4 implements this in Sprint 2")


def audit_timeline(client: Any, decision_id: str) -> list[dict[str, Any]]:
    """The ordered event timeline for one decision, with chain verification state."""
    raise NotImplementedError("L4 implements this in Sprint 2")


def policy_diff_view(client: Any, policy_id: str, old_version: str, new_version: str) -> list[dict[str, Any]]:
    """Field-level policy diff, for showing why a replayed verdict changed."""
    raise NotImplementedError("L4 implements this in Sprint 2")
