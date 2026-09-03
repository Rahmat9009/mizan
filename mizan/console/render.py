"""Rendering: view models in, HTML out. The only strings that reach the page go through ``escaping``.

Every section carries ``data-region`` (which view model it came from) and ``data-authority``:

* ``enforcement`` - the deterministic engine, the governor, the authorization, the execution gate. Values in
  these sections come from closed vocabularies, so free text cannot appear in one even by mistake.
* ``advisory`` - the model's opinion. Fenced, banner-first, and structurally incapable of carrying a verdict:
  the only vocabulary it has is CONCUR / REDUCE / REJECT, and the banner says so on the page.
* ``agent`` - what the agent claimed. Audit material, no authority.

There is no theme switching script, and no script of any kind: :func:`document` emits a stylesheet and nothing
else executable, and ``el()`` refuses ``<script>`` outright. Dark is the default for traders and ops; light is
for risk and compliance. Nothing is written to browser storage, because nothing here can write anything.
"""

from __future__ import annotations

from typing import Any

from mizan.console.escaping import el, escape_text, render
from mizan.console.views import ADVISORY_BANNER, Untrusted

__all__ = [
    "THEMES",
    "document",
    "fragment",
    "render_audit_timeline",
    "render_decision_detail",
    "render_decision_feed",
    "render_kill_switch",
    "render_policy_editor",
    "render_response_level",
    "untrusted_node",
]

_BASE_CSS = """
:root {
  --font: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --font-ui: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: var(--font-ui); font-size: 14px; line-height: 1.5;
       background: var(--bg); color: var(--fg); }
main { max-width: 1180px; margin: 0 auto; padding: 24px 20px 72px; }
h1 { font-size: 20px; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.08em; margin: 0 0 12px;
     color: var(--muted); }
h3 { font-size: 13px; margin: 18px 0 8px; color: var(--muted); }
section { border: 1px solid var(--line); border-radius: 8px; padding: 16px 18px; margin: 0 0 16px;
          background: var(--panel); }
table { border-collapse: collapse; width: 100%; font-family: var(--font); font-size: 12.5px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 11px;
     letter-spacing: 0.06em; }
dl.kv { display: grid; grid-template-columns: minmax(180px, 260px) 1fr; gap: 2px 16px; margin: 0;
        font-family: var(--font); font-size: 12.5px; }
dl.kv dt { color: var(--muted); }
dl.kv dd { margin: 0; overflow-wrap: anywhere; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-family: var(--font);
         font-size: 11px; font-weight: 700; letter-spacing: 0.04em; border: 1px solid var(--line); }
.verdict-APPROVE { background: var(--ok-bg); color: var(--ok); border-color: var(--ok); }
.verdict-REDUCE { background: var(--warn-bg); color: var(--warn); border-color: var(--warn); }
.verdict-REJECT { background: var(--bad-bg); color: var(--bad); border-color: var(--bad); }
.outcome-PASS { color: var(--ok); }
.outcome-FAIL { color: var(--bad); font-weight: 700; }
.outcome-NOT { color: var(--muted); font-style: italic; }
.note { color: var(--muted); font-size: 12px; margin: 10px 0 0; }
.untrusted { display: inline-block; max-width: 100%; border-left: 3px solid var(--taint);
             background: var(--taint-bg); padding: 2px 8px; border-radius: 0 4px 4px 0;
             font-family: var(--font); white-space: pre-wrap; overflow-wrap: anywhere; }
.untrusted-label { display: block; font-size: 10px; letter-spacing: 0.08em; text-transform: uppercase;
                   color: var(--taint); }
.untrusted-flags { display: block; font-size: 10px; color: var(--bad); }
.untrusted-flagged { border-left-color: var(--bad); }
[data-authority="advisory"] { border: 1px dashed var(--taint); background: var(--advisory-bg); }
[data-authority="advisory"] .banner { display: block; font-weight: 700; letter-spacing: 0.06em;
                                      color: var(--taint); margin-bottom: 8px; }
[data-authority="enforcement"] { border-left: 3px solid var(--ok); }
.chain-ok { color: var(--ok); font-weight: 700; }
.chain-bad { color: var(--bad); font-weight: 700; }
.diff-added td { color: var(--ok); }
.diff-removed td { color: var(--bad); }
.diff-changed td { color: var(--warn); }
.ladder { display: flex; gap: 6px; flex-wrap: wrap; margin: 8px 0; }
.step { border: 1px solid var(--line); border-radius: 6px; padding: 6px 12px; font-family: var(--font);
        font-size: 12px; }
.step-active { border-color: var(--warn); color: var(--warn); font-weight: 700; }
form.control { border: 1px solid var(--line); border-radius: 6px; padding: 12px; margin: 10px 0 0; }
form.control button { font: inherit; padding: 6px 14px; border-radius: 6px; cursor: pointer;
                      border: 1px solid var(--line); background: var(--panel); color: var(--fg); }
form.human-only { border-color: var(--warn); }
label { display: block; font-size: 12px; color: var(--muted); margin: 6px 0 2px; }
input { font: inherit; padding: 5px 8px; border-radius: 5px; border: 1px solid var(--line);
        background: var(--bg); color: var(--fg); width: 320px; max-width: 100%; }
""".strip()

_DARK_CSS = """
:root[data-theme="dark"] {
  --bg: #0d1117; --panel: #131a23; --fg: #e6edf3; --muted: #8b98a5; --line: #253040;
  --ok: #3fb950; --ok-bg: #0f2417; --warn: #d29922; --warn-bg: #241c0d;
  --bad: #f85149; --bad-bg: #2a1315; --taint: #a371f7; --taint-bg: #1b1526;
  --advisory-bg: #17121f;
}
""".strip()

_LIGHT_CSS = """
:root[data-theme="light"] {
  --bg: #ffffff; --panel: #f7f8fa; --fg: #1c2128; --muted: #5a6570; --line: #d5dae0;
  --ok: #116329; --ok-bg: #e8f5eb; --warn: #8a6100; --warn-bg: #fbf3e0; --bad: #a40e26;
  --bad-bg: #fdeced; --taint: #6639ba; --taint-bg: #f2edfb; --advisory-bg: #f6f2fd;
}
""".strip()

#: The two themes. Dark for traders and ops on a trading desk; light for risk and compliance on paper.
#: Both map to the same stylesheet on purpose: the ``data-theme`` attribute on the root element selects
#: which block applies, so there is no runtime switch and therefore no script.
THEMES: dict[str, str] = {
    "dark": "\n".join([_BASE_CSS, _DARK_CSS, _LIGHT_CSS]),
    "light": "\n".join([_BASE_CSS, _DARK_CSS, _LIGHT_CSS]),
}


def _stylesheet(theme: str) -> str:
    """The stylesheet for a theme. Constant text only - it is asserted to carry no markup character."""
    if theme not in THEMES:
        raise ValueError(f"unknown theme {theme!r}; expected one of {sorted(THEMES)}")
    css = THEMES[theme]
    if "<" in css or "&" in css:
        raise ValueError("the console stylesheet must never contain a markup character")
    return css


# ----------------------------------------------------------------------------------------------------------
# Primitives
# ----------------------------------------------------------------------------------------------------------


def untrusted_node(value: Any, *, tag: str = "span") -> Any:
    """Render an :class:`Untrusted` string: escaped, fenced, badged when flagged, captioned when it matters.

    The fence, the ``data-untrusted`` marker and the escaping are unconditional. The written caption is not:
    on a four-character symbol it would be more chrome than content, so it appears on block-level free text
    (the fields finding F-8 was about) and on anything :func:`taint_flags` found suspicious.
    """
    if value is None:
        return None
    if not isinstance(value, Untrusted):
        raise TypeError("only an Untrusted value may be rendered as untrusted text")
    classes = "untrusted untrusted-flagged" if value.suspicious else "untrusted"
    caption = tag != "span" or value.suspicious
    return el(
        tag,
        el("span", f"{value.origin}-authored text - escaped, never interpreted", class_="untrusted-label")
        if caption
        else None,
        el("span", value.text, class_="untrusted-body"),
        el("span", "flagged: " + ", ".join(value.flags), class_="untrusted-flags") if value.flags else None,
        class_=classes,
        title=f"untrusted text authored by the {value.origin}; escaped, never interpreted as markup",
        data_untrusted="true",
        data_origin=value.origin,
    )


def _value(value: Any) -> Any:
    """Render one field value: untrusted text is fenced, everything else is plain escaped text."""
    if isinstance(value, Untrusted):
        return untrusted_node(value)
    if value is None:
        return "—"
    if value is True or value is False:
        return "yes" if value else "no"
    return str(value)


def _kv(pairs: list[tuple[str, Any]]) -> Any:
    children: list[Any] = []
    for label, value in pairs:
        children.append(el("dt", label))
        children.append(el("dd", _value(value)))
    return el("dl", *children, class_="kv")


def _verdict_badge(value: str) -> Any:
    return el("span", value, class_=f"badge verdict-{value}", data_verdict=value)


def _codes(codes: list[dict[str, str]]) -> Any:
    if not codes:
        return el("span", "—")
    return el(
        "ul",
        *[
            el("li", el("code", code["code"]), " ", code["description"], data_reason_code=code["code"])
            for code in codes
        ],
    )


def _section(name: str, title: str, *children: Any, authority: str | None = None) -> Any:
    return el(
        "section",
        el("h2", title),
        *children,
        data_region=name,
        data_authority=authority,
    )


def _table(headers: list[str], rows: list[list[Any]], *, row_classes: list[str] | None = None) -> Any:
    body = []
    for index, row in enumerate(rows):
        css = row_classes[index] if row_classes else None
        body.append(el("tr", *[el("td", _value(cell)) for cell in row], class_=css))
    return el(
        "table",
        el("thead", el("tr", *[el("th", header) for header in headers])),
        el("tbody", *body),
    )


# ----------------------------------------------------------------------------------------------------------
# The six views
# ----------------------------------------------------------------------------------------------------------


def render_decision_feed(page: dict[str, Any]) -> Any:
    """The decision feed: newest first, cursor-paged."""
    rows = page["rows"]
    table_rows = [
        [
            row["sequence"],
            row["decision_timestamp"],
            row["symbol"],
            row["agent_id"],
            row["original_quantity"],
            row["authorized_quantity"],
            f"{row['policy_version']} @ {row['policy_hash_short']}",
            ", ".join(code["code"] for code in row["reason_codes"]) or "—",
        ]
        for row in rows
    ]
    table = el(
        "table",
        el(
            "thead",
            el(
                "tr",
                *[
                    el("th", header)
                    for header in (
                        "seq",
                        "verdict",
                        "decided at",
                        "symbol",
                        "agent",
                        "proposed",
                        "authorised",
                        "policy",
                        "reason codes",
                    )
                ],
            ),
        ),
        el(
            "tbody",
            *[
                el(
                    "tr",
                    el("td", str(cells[0])),
                    el("td", _verdict_badge(rows[index]["verdict"])),
                    *[el("td", _value(cell)) for cell in cells[1:]],
                    data_decision_id=rows[index]["decision_id"],
                )
                for index, cells in enumerate(table_rows)
            ],
        ),
    )
    cursor = page.get("next_before_sequence")
    footer = el(
        "p",
        (
            f"Newest first. Showing {len(rows)} of at most {page['limit']}. "
            + (
                f"Next page: before_sequence={cursor}."
                if cursor is not None
                else "This is the end of the chain."
            )
        ),
        class_="note",
    )
    return _section(
        "decision_feed",
        "Decision feed",
        table if rows else el("p", "No decisions are visible to this tenant.", class_="note"),
        footer,
        authority="enforcement",
    )


def _not_found(detail: dict[str, Any]) -> Any:
    """The one not-found state. It echoes nothing the caller supplied, so every miss looks identical."""
    return _section(
        "not_found",
        "Decision not found",
        el("p", detail["message"]),
        el(
            "p",
            "An id that does not exist and an id belonging to another tenant produce this same page, on "
            "purpose: the console cannot be used to discover which ids exist elsewhere.",
            class_="note",
        ),
    )


def _advisory_section(advisory: dict[str, Any]) -> Any:
    if not advisory.get("present"):
        return _section(
            "advisory",
            "Advisory",
            el("span", ADVISORY_BANNER, class_="banner"),
            el("p", advisory.get("status", "")),
            el("p", advisory["note"], class_="note"),
            authority="advisory",
        )
    return _section(
        "advisory",
        "Advisory",
        el("span", ADVISORY_BANNER, class_="banner"),
        _kv(
            [
                ("status", advisory["status"]),
                ("profile", advisory["profile"]),
                ("invoked", advisory["invoked"]),
                ("available", advisory["available"]),
                ("recommendation (opinion only)", advisory["recommendation"]),
                ("recommended quantity (opinion only)", advisory["recommended_quantity"]),
                ("authority ceiling", advisory["authority_ceiling"]),
                ("provider", advisory["provider_ref"]),
                ("raw response hash", advisory["raw_hash"]),
            ]
        ),
        el("h3", "Model reasoning"),
        untrusted_node(advisory["reasoning"], tag="div")
        or el("p", "The advisory returned no reasoning.", class_="note"),
        el("p", advisory["note"], class_="note"),
        authority="advisory",
    )


def _checks_table(risk: dict[str, Any]) -> Any:
    rows = []
    for check in risk["checks"]:
        outcome = check["outcome"]
        css = "outcome-PASS" if outcome == "PASS" else "outcome-FAIL" if outcome == "FAIL" else "outcome-NOT"
        rows.append(
            el(
                "tr",
                el("td", el("code", check["check_id"])),
                el("td", el("span", outcome, class_=css)),
                el("td", check["severity"]),
                el("td", _value(check["threshold"])),
                el("td", _value(check["actual"])),
                el("td", _value(check["distance"])),
                el("td", check["reason_code"]["code"] if check["reason_code"] else "—"),
                el("td", _value(check["data_source"])),
                el("td", _value(check["detail"])),
                data_check_id=check["check_id"],
            )
        )
    return el(
        "table",
        el(
            "thead",
            el(
                "tr",
                *[
                    el("th", header)
                    for header in (
                        "check",
                        "outcome",
                        "severity",
                        "threshold",
                        "actual",
                        "distance",
                        "reason code",
                        "data source",
                        "detail",
                    )
                ],
            ),
        ),
        el("tbody", *rows),
    )


def render_decision_detail(detail: dict[str, Any]) -> Any:
    """One decision as a case file: policy, risk, state, governor, advisory, authorization, execution."""
    if not detail.get("found"):
        return el("div", _not_found(detail))

    header = detail["header"]
    policy = detail["policy"]
    proposal = detail["proposal"]
    risk = detail["risk"]
    governor = detail["governor"]
    sections: list[Any] = [
        _section(
            "decision_header",
            "Decision",
            el("p", _verdict_badge(header["verdict"]), " ", el("code", header["decision_id"])),
            _kv(
                [
                    ("sequence", header["sequence"]),
                    ("decided at", header["decision_timestamp"]),
                    ("recorded at", header["recorded_at"]),
                    ("agent", header["agent_id"]),
                    ("symbol", header["symbol"]),
                    ("strategy", header["strategy"]),
                    ("intent", header["intent"]),
                    ("engine version", header["engine_version"]),
                    ("audit hash", header["audit_hash"]),
                    ("previous hash", header["audit_prev_hash"]),
                ]
            ),
            authority="enforcement",
        ),
        _section(
            "policy",
            "Policy in force",
            _kv(
                [
                    ("policy", policy["policy_id"]),
                    ("version", policy["version"]),
                    ("policy hash", policy["hash"]),
                    ("snapshot hash", policy["snapshot_hash"]),
                    ("enabled checks", str(len(policy["enabled_checks"]))),
                ]
            ),
            authority="enforcement",
        ),
        _section(
            "risk_evaluation",
            "Risk evaluation",
            _kv(
                [
                    ("evaluation verdict", risk["verdict"]),
                    ("evaluation id", risk["evaluation_id"]),
                    ("data complete", risk["data_complete"]),
                    ("proposed quantity", risk["original_quantity"]),
                    ("recommended quantity", risk["recommended_quantity"]),
                ]
            ),
            _checks_table(risk),
            el("p", risk["note"], class_="note"),
            authority="enforcement",
        ),
    ]

    path_state = detail.get("path_state")
    aggregate = detail.get("aggregate_state")
    state_children: list[Any] = [
        _kv([("graduated-response level", detail.get("response_level"))]),
    ]
    if path_state:
        state_children.append(el("h3", "Path state"))
        state_children.append(
            _kv(
                [
                    ("as of", path_state["as_of"]),
                    ("peak equity", path_state["peak_equity"]),
                    ("current drawdown %", path_state["current_drawdown_pct"]),
                    ("consecutive losses", path_state["consecutive_losses"]),
                    ("days under water", path_state["days_under_water"]),
                    ("sample size", path_state["sample_size"]),
                ]
            )
        )
    if aggregate:
        state_children.append(el("h3", "Aggregate state (all agents of this tenant)"))
        state_children.append(
            _kv(
                [
                    ("as of", aggregate["as_of"]),
                    ("gross exposure", aggregate["gross_exposure"]),
                    ("net exposure", aggregate["net_exposure"]),
                    ("exposure % of equity", aggregate["exposure_pct_of_equity"]),
                    ("crowding score", aggregate["crowding_score"]),
                    ("days to liquidate book", aggregate["days_to_liquidate_book"]),
                ]
            )
        )
        if aggregate["by_agent"]:
            state_children.append(
                _table(
                    ["agent", "exposure"],
                    [[entry["key"], entry["value"]] for entry in aggregate["by_agent"]],
                )
            )
        if aggregate["pending_intents"]:
            state_children.append(el("h3", "Pending intents"))
            state_children.append(
                _table(
                    ["agent", "symbol", "direction", "notional", "proposed at"],
                    [
                        [
                            intent["agent_id"],
                            intent["symbol"],
                            intent["direction"],
                            intent["notional"],
                            intent["proposed_at"],
                        ]
                        for intent in aggregate["pending_intents"]
                    ],
                )
            )
    sections.append(
        _section("state", "State the rules were applied to", *state_children, authority="enforcement")
    )

    sections.append(
        _section(
            "governor",
            "Governor arbitration",
            el("p", _verdict_badge(governor["verdict"])),
            _kv(
                [
                    ("proposed quantity", governor["original_quantity"]),
                    ("authorised quantity", governor["authorized_quantity"]),
                    ("proposed notional", governor["original_notional"]),
                    ("authorised notional", governor["authorized_notional"]),
                    ("verdict hash", governor["verdict_hash"]),
                ]
            ),
            el("h3", "Reason codes"),
            _codes(governor["reason_codes"]),
            (
                el(
                    "div",
                    el("h3", "Reductions"),
                    _table(
                        ["source", "from", "to", "reason code"],
                        [
                            [
                                reduction["source"],
                                reduction["from_quantity"],
                                reduction["to_quantity"],
                                reduction["reason_code"]["code"],
                            ]
                            for reduction in governor["reductions"]
                        ],
                    ),
                )
                if governor["reductions"]
                else None
            ),
            authority="enforcement",
        )
    )

    sections.append(_advisory_section(detail["advisory"]))

    authorization = detail.get("authorization")
    if authorization:
        binding = authorization["binding"]
        sections.append(
            _section(
                "authorization",
                "Authorization and its binding",
                _kv(
                    [
                        ("auth id", authorization["auth_id"]),
                        ("issued at", authorization["issued_at"]),
                        ("expires at", authorization["expires_at"]),
                        ("ttl seconds", authorization["ttl_seconds"]),
                        ("single use", authorization["single_use"]),
                        ("environment", authorization["environment"]),
                        ("idempotency key", authorization["idempotency_key"]),
                        ("authorization hash", authorization["authorization_hash"]),
                        ("authorised quantity", authorization["total_quantity"]),
                    ]
                ),
                el("h3", "Bound state"),
                _kv(
                    [
                        ("policy hash", binding["policy_hash"]),
                        ("portfolio snapshot", binding["portfolio_snapshot_id"]),
                        ("portfolio state hash", binding["portfolio_state_hash"]),
                        ("market snapshot", binding["market_snapshot_id"]),
                        ("response level", binding["response_level"]),
                        ("path state hash", binding["path_state_hash"]),
                        ("aggregate state hash", binding["aggregate_state_hash"]),
                    ]
                ),
                el("p", binding["note"], class_="note"),
                authority="enforcement",
            )
        )
    else:
        sections.append(
            _section(
                "authorization",
                "Authorization and its binding",
                el("p", "No authorization was issued for this decision."),
                authority="enforcement",
            )
        )

    execution = detail.get("execution")
    if execution:
        sections.append(
            _section(
                "execution",
                "Execution result",
                _kv(
                    [
                        ("status", execution["status"]),
                        ("broker", execution["broker"]),
                        ("environment", execution["environment"]),
                        ("client order id", execution["client_order_id"]),
                        ("broker order id", execution["broker_order_id"]),
                        ("broker status", execution["broker_status"]),
                        ("checked at", execution["checked_at"]),
                        ("authorization validated at", execution["authorization_validated_at"]),
                        ("kill switch checked at", execution["kill_switch_checked_at"]),
                        ("submitted at", execution["submitted_at"]),
                        ("revalidation performed", execution["revalidation"]["performed"]),
                        ("revalidation supports it", execution["revalidation"]["supported"]),
                        ("state changed since decision", execution["revalidation"]["state_changed"]),
                    ]
                ),
                el("h3", "Broker message"),
                untrusted_node(execution["message"], tag="div")
                or el("p", "The broker returned no message.", class_="note"),
                _codes(execution["reason_codes"]),
                authority="enforcement",
            )
        )

    replayed = detail.get("decision_replay")
    if replayed:
        sections.append(
            _section(
                "decision_replay",
                "Decision replay",
                _kv(
                    [
                        ("mode", replayed["mode"]),
                        ("identical", replayed["identical"]),
                        ("verdict as recorded", replayed["original_verdict"]),
                        ("verdict on decision replay", replayed["replayed_verdict"]),
                        ("engine version matches", replayed["engine_version_matches"]),
                        ("recorded engine version", replayed["recorded_engine_version"]),
                        ("running engine version", replayed["running_engine_version"]),
                    ]
                ),
                el("h3", "Decision replay detail (verbatim)"),
                untrusted_node(replayed["detail"], tag="div")
                or el("p", "The decision replay reported no detail.", class_="note"),
                authority="enforcement",
            )
        )

    sections.append(
        _section(
            "agent_reasoning",
            "What the agent claimed",
            _kv(
                [
                    ("proposal id", proposal["proposal_id"]),
                    ("created at", proposal["created_at"]),
                    ("expires at", proposal["expires_at"]),
                    ("confidence", proposal["confidence"]),
                    ("model provider", proposal["model"]["provider"]),
                    ("model", proposal["model"]["model"]),
                    ("prompt hash", proposal["model"]["prompt_hash"]),
                ]
            ),
            el("h3", "Agent reasoning"),
            untrusted_node(proposal["reasoning"], tag="div")
            or el("p", "The agent supplied no reasoning.", class_="note"),
            el("p", proposal["reasoning_note"], class_="note"),
            authority="agent",
        )
    )
    sections.append(el("p", detail["suppressed_note"], class_="note"))
    return el("div", *sections, data_view="decision_detail")


def render_audit_timeline(view: dict[str, Any]) -> Any:
    """Decisions and control events in sequence order, under the chain verification banner."""
    status = view["status"]
    banner = el(
        "p",
        el("span", status["headline"], class_="chain-ok" if status.get("ok") else "chain-bad"),
    )
    detail = status.get("detail")
    rows = []
    for entry in view["entries"]:
        if entry["kind"] == "decision":
            what = [_verdict_badge(entry["verdict"]), " ", _value(entry["symbol"])]
        else:
            level = f"{entry['from_level']} -> {entry['to_level']}" if entry["direction"] else ""
            actor = "human" if entry["actor_type"] == "human" else "system"
            what = [
                el("code", entry["event_type"]),
                " ",
                level,
                " ",
                el("span", f"[{actor}]", class_="badge"),
            ]
        rows.append(
            el(
                "tr",
                el("td", str(entry["sequence"])),
                el("td", entry["kind"]),
                el("td", *what),
                el("td", _value(entry["occurred_at"])),
                el("td", el("code", (entry["audit_hash"] or "")[:12] + "...")),
                el(
                    "td",
                    el(
                        "span",
                        entry["chain"],
                        class_="chain-ok" if entry["chain"] == "VERIFIED" else "chain-bad",
                    ),
                ),
                data_sequence=str(entry["sequence"]),
                data_kind=entry["kind"],
            )
        )
    table = el(
        "table",
        el(
            "thead",
            el(
                "tr",
                *[
                    el("th", header)
                    for header in ("seq", "kind", "what happened", "occurred at", "audit hash", "chain")
                ],
            ),
        ),
        el("tbody", *rows),
    )
    return _section(
        "audit_timeline",
        "Audit timeline",
        banner,
        untrusted_node(detail, tag="p") if detail else None,
        table if rows else el("p", "No chain entries are visible to this tenant.", class_="note"),
        el(
            "p",
            "Decisions and control events share one chain per tenant, so a control action cannot be slipped "
            "in beside the decisions whose meaning it changed.",
            class_="note",
        ),
        authority="enforcement",
    )


def render_policy_editor(view: dict[str, Any]) -> Any:
    """Old versus new policy version, field by field, each with its hash."""
    if not view.get("available"):
        return _section(
            "policy_editor",
            "Policy editor",
            el("p", view.get("message", "")),
            authority="enforcement",
        )
    rows = []
    for row in view["rows"]:
        rows.append(
            el(
                "tr",
                el("td", el("code", row["path"])),
                el("td", row["change"]),
                el("td", _value(row["old"])),
                el("td", _value(row["new"])),
                class_=f"diff-{row['change']}",
                data_path=row["path"],
            )
        )
    return _section(
        "policy_editor",
        "Policy editor - version diff",
        _kv(
            [
                ("policy", view["policy_id"]),
                ("old version", view["old"]["version"]),
                ("old policy hash", view["old"]["hash"]),
                ("new version", view["new"]["version"]),
                ("new policy hash", view["new"]["hash"]),
                ("changed fields", view["changed_count"]),
            ]
        ),
        el(
            "table",
            el("thead", el("tr", *[el("th", header) for header in ("field", "change", "old", "new")])),
            el("tbody", *rows),
        )
        if rows
        else el("p", "The two versions are identical field for field.", class_="note"),
        el("p", view["note"], class_="note"),
        authority="enforcement",
    )


def render_kill_switch(view: dict[str, Any]) -> Any:
    """The kill switch, with the two controls kept visibly asymmetric."""
    engaged = view["engaged"]
    if engaged is None:
        badge_class, badge_state = "badge", "unknown"
    elif engaged:
        badge_class, badge_state = "badge verdict-REJECT", "engaged"
    else:
        badge_class, badge_state = "badge verdict-APPROVE", "disengaged"
    return _section(
        "kill_switch",
        "Kill switch",
        el(
            "p",
            el(
                "span",
                view["state_label"],
                class_=badge_class,
                data_kill_switch=badge_state,
            ),
            " ",
            el("span", f"(state read from: {view['source']})", class_="note"),
        ),
        el(
            "form",
            el("p", view["engage"]["note"], class_="note"),
            el("button", view["engage"]["label"], type="submit", name="engage", value="true"),
            class_="control",
            method="post",
            action=view["engage"]["action"],
        ),
        el(
            "form",
            el("p", view["disengage"]["note"], class_="note"),
            el("label", "Human actor id (required)", for_="kill-switch-actor"),
            el("input", id="kill-switch-actor", name="actor_id", required=True),
            el("button", view["disengage"]["label"], type="submit", name="engage", value="false"),
            class_="control human-only",
            method="post",
            action=view["disengage"]["action"],
            data_requires_human="true",
        ),
        el("p", view["asymmetry"], class_="note"),
        authority="enforcement",
    )


def render_response_level(view: dict[str, Any]) -> Any:
    """The graduated-response level 0..5, its ladder, and the asymmetry between the two directions."""
    steps = [
        el(
            "span",
            f"level {step['level']}",
            class_="step step-active" if step["active"] else "step",
            data_level=str(step["level"]),
            data_active="true" if step["active"] else None,
        )
        for step in view["levels"]
    ]
    ladder_rows = [
        [
            step["level"],
            step["size_multiplier"],
            step["new_risk_allowed"],
            step["daily_loss_pct"],
            step["drawdown_pct"],
        ]
        for step in view["ladder"]
    ]
    return _section(
        "response_level",
        "Graduated response level",
        el("div", *steps, class_="ladder"),
        _kv(
            [
                ("current level", view["level"]),
                ("escalation", view["escalation"]),
                ("de-escalation", view["de_escalation"]),
            ]
        ),
        _table(
            ["level", "size multiplier", "new risk allowed", "daily loss trigger %", "drawdown trigger %"],
            ladder_rows,
        )
        if ladder_rows
        else el("p", "No response ladder is configured in the policy in force.", class_="note"),
        el(
            "form",
            el("p", view["asymmetry"], class_="note"),
            el("label", "Human actor id (required)", for_="response-level-actor"),
            el("input", id="response-level-actor", name="actor_id", required=True),
            el("label", "New level", for_="response-level-target"),
            el("input", id="response-level-target", name="to_level", type="number", min="0", max="5"),
            el("button", view["de_escalate"]["label"], type="submit"),
            class_="control human-only",
            method="post",
            action=view["de_escalate"]["action"],
            data_requires_human="true",
        ),
        authority="enforcement",
    )


# ----------------------------------------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------------------------------------


def fragment(node: Any) -> str:
    """Serialise one rendered view."""
    return render(node)


def document(title: str, *nodes: Any, theme: str = "dark") -> str:
    """A complete page. No script of any kind is emitted, so nothing can reach browser storage."""
    css = _stylesheet(theme)
    head = (
        '<!doctype html><html lang="en" data-theme="' + theme + '">'
        '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>" + escape_text(title) + "</title>"
        "<style>" + css + "</style></head><body><main>"
    )
    body = el("h1", title)
    return head + render(body) + "".join(render(node) for node in nodes) + "</main></body></html>"
