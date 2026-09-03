"""L4 - the console.

Reads only through the SDK and the API read models; it never touches a ledger file or a broker directly.
The Streamlit app is a disposable debugging tool for the team and is never shown to a customer
(Master Plan C11).

The package is three layers, and the separation is the point:

``escaping``  every tainted string's only route to the page. No unescaped sink exists here; ``el()`` refuses
              ``<script>``, ``<style>``, ``<iframe>``, every ``on*`` attribute and every URL scheme outside
              http/https/mailto. This is the direct answer to security finding F-8.
``views``     contracts in, plain data out. Agent-, model-, tenant- and broker-authored strings come out
              wrapped in :class:`~mizan.console.views.Untrusted`; enforcement values are re-validated against
              closed vocabularies, so free text cannot occupy an enforcement field at all.
``render``    view models in, HTML out, with ``data-region`` and ``data-authority`` on every section so the
              advisory can never be mistaken for - or dressed up as - enforcement.

Six views: the decision feed, the decision detail, the audit timeline, the policy editor with its diff, the
kill switch, and the graduated-response level indicator.

Nothing here writes to browser storage: no script is emitted, so there is nothing that could.

There is deliberately no ``streamlit_app.py``. Streamlit's only route to custom markup is
``st.markdown(..., unsafe_allow_html=True)`` - the exact sink finding F-8 was raised about - and a
disposable debug UI is not worth reopening it. :func:`~mizan.console.render.document` renders the same
views as a self-contained page that any static server or test can hold.
"""

from __future__ import annotations

from mizan.console import client, escaping, render, views
from mizan.console.client import ConsoleClient
from mizan.console.escaping import (
    BLOCKED_URL,
    el,
    escape_attr,
    escape_text,
    safe_url,
    taint_flags,
)
from mizan.console.render import (
    THEMES,
    document,
    fragment,
    render_audit_timeline,
    render_decision_detail,
    render_decision_feed,
    render_kill_switch,
    render_policy_editor,
    render_response_level,
    untrusted_node,
)
from mizan.console.views import (
    ADVISORY_BANNER,
    ADVISORY_NOTE,
    DE_ESCALATION_NOTE,
    MAX_RESPONSE_LEVEL,
    NOT_FOUND_MESSAGE,
    PERFORMANCE_FIELDS,
    Untrusted,
    audit_timeline,
    audit_timeline_view,
    chain_status,
    decision_detail,
    decision_feed,
    feed_page,
    kill_switch_view,
    policy_diff_rows,
    policy_diff_view,
    policy_editor_view,
    response_level_view,
)

__all__ = [
    "ADVISORY_BANNER",
    "ADVISORY_NOTE",
    "BLOCKED_URL",
    "DE_ESCALATION_NOTE",
    "MAX_RESPONSE_LEVEL",
    "NOT_FOUND_MESSAGE",
    "PERFORMANCE_FIELDS",
    "THEMES",
    "ConsoleClient",
    "Untrusted",
    "audit_timeline",
    "audit_timeline_view",
    "chain_status",
    "client",
    "decision_detail",
    "decision_feed",
    "document",
    "el",
    "escape_attr",
    "escape_text",
    "escaping",
    "feed_page",
    "fragment",
    "kill_switch_view",
    "policy_diff_rows",
    "policy_diff_view",
    "policy_editor_view",
    "render",
    "render_audit_timeline",
    "render_decision_detail",
    "render_decision_feed",
    "render_kill_switch",
    "render_policy_editor",
    "render_response_level",
    "response_level_view",
    "safe_url",
    "taint_flags",
    "untrusted_node",
    "views",
]
