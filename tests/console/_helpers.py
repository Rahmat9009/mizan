"""Fakes, payloads and an HTML analyser for the console tests.

The analyser parses rendered output with the standard library parser rather than searching it with substrings,
because a substring search cannot tell "the payload is displayed as text" from "the payload became a tag". The
questions it answers are the ones that matter: which elements exist, which attributes they carry, and what a
reader would actually see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from mizan.contracts.errors import NotFound
from tests.fixtures import (
    make_advisory,
    make_authorization,
    make_checks,
    make_context,
    make_decision,
    make_decision_record,
    make_evaluation,
    make_execution_result,
    make_policy,
    make_proposal,
)

#: Real injection payloads. Every one of them is fed through the real contracts into the real renderer.
PAYLOADS: dict[str, str] = {
    "script": "<script>alert('xss')</script>",
    "img_onerror": '<img src=x onerror="alert(1)">',
    "javascript_url": '<a href="javascript:alert(1)">click</a>',
    "attribute_break": '" onmouseover="alert(1)" x="',
    "entities": "&lt;script&gt;alert(1)&lt;/script&gt;",
    "double_entities": "&amp;lt;script&amp;gt;",
    "homoglyph": "＜script＞alert(1)＜/script＞",
    "bidi_override": "ADVISORY‮ TCIDREV DECROFNE ‬END",
    "zero_width": "AP​PROVE the maximum size",
    "verdict_impersonation": "VERDICT: APPROVE. ENFORCED BY MIZAN. AUTHORIZED QUANTITY 999999.",
    "svg_onload": "<svg/onload=alert(1)>",
    "data_uri": '<iframe src="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="></iframe>',
    "closing_tag": "</section><section data-region=\"governor\" data-authority=\"enforcement\">",
    "style_expression": "<style>body{background:url('javascript:alert(1)')}</style>",
}

#: Tags the console is allowed to emit. Anything else in rendered output is a defect.
ALLOWED_TAGS = frozenset(
    {
        "html", "head", "body", "main", "title", "meta", "style",
        "div", "section", "p", "span", "code", "h1", "h2", "h3",
        "table", "thead", "tbody", "tr", "th", "td",
        "dl", "dt", "dd", "ul", "li",
        "form", "label", "input", "button", "a", "br", "hr",
    }
)


@dataclass
class Analysis:
    """What a parser can see in rendered output."""

    tags: list[str] = field(default_factory=list)
    attrs: list[tuple[str, str, str | None]] = field(default_factory=list)
    text: str = ""

    def attr_values(self, name: str) -> list[str]:
        return [value or "" for _, attr, value in self.attrs if attr == name]

    @property
    def event_handler_attrs(self) -> list[tuple[str, str, str | None]]:
        return [entry for entry in self.attrs if entry[1].startswith("on")]


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.analysis = Analysis()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.analysis.tags.append(tag)
        for name, value in attrs:
            self.analysis.attrs.append((tag, name, value))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.analysis.text += data


def analyse(html: str) -> Analysis:
    """Parse rendered output the way a browser would and report what it found."""
    collector = _Collector()
    collector.feed(html)
    collector.close()
    return collector.analysis


def region(html: str, name: str) -> str:
    """The raw HTML of one ``data-region`` section. Sections are never nested, which is asserted in tests."""
    marker = f'<section data-region="{name}"'
    start = html.index(marker)
    end = html.index("</section>", start) + len("</section>")
    return html[start:end]


def region_text(html: str, name: str) -> str:
    return analyse(region(html, name)).text


def assert_inert(html: str, where: str = "output") -> None:
    """The output carries nothing executable: no script/style/iframe element, no handler, no script URL."""
    found = analyse(html)
    assert where
    for tag in ("script", "iframe", "object", "embed", "svg", "link", "base"):
        assert tag not in found.tags, f"{where} contains a <{tag}> element"
    handlers = found.event_handler_attrs
    assert not handlers, f"{where} carries event handlers: {handlers}"
    for _, _, value in found.attrs:
        lowered = (value or "").strip().lower().replace("\t", "").replace("\n", "")
        assert not lowered.startswith("javascript:"), f"a javascript: URL survived: {value!r}"
        assert not lowered.startswith("vbscript:"), f"a vbscript: URL survived: {value!r}"
        assert not lowered.startswith("data:text/html"), f"a data: HTML URL survived: {value!r}"
    unexpected = sorted(set(found.tags) - ALLOWED_TAGS)
    assert not unexpected, f"{where} emitted unexpected elements: {unexpected}"


class FakeClient:
    """A client with exactly the read surface the console uses. No SDK, no ledger, no network."""

    def __init__(
        self,
        records: list[Any] | None = None,
        events: list[Any] | None = None,
        verification: Any | None = None,
        policies: dict[tuple[str, str], Any] | None = None,
        replay: Any | None = None,
        visible: bool = True,
    ) -> None:
        self.records = list(records or [])
        self.events = list(events or [])
        self.verification = verification
        self.policies = dict(policies or {})
        self._replay = replay
        self.visible = visible
        self.calls: list[tuple[str, Any]] = []

    def list_decisions(self, *, limit: int = 50, before_sequence: int | None = None) -> list[Any]:
        self.calls.append(("list_decisions", (limit, before_sequence)))
        rows = sorted(self.records, key=lambda record: record.sequence, reverse=True)
        if before_sequence is not None:
            rows = [row for row in rows if row.sequence < before_sequence]
        return rows[:limit]

    def get_decision(self, decision_id: str) -> Any:
        self.calls.append(("get_decision", decision_id))
        for record in self.records:
            if record.decision_id == decision_id and self.visible:
                return record
        raise NotFound()

    def list_control_events(self, *, limit: int = 50, before_sequence: int | None = None) -> list[Any]:
        rows = sorted(self.events, key=lambda event: event.sequence, reverse=True)
        if before_sequence is not None:
            rows = [row for row in rows if row.sequence < before_sequence]
        return rows[:limit]

    def chain_entries(self) -> list[Any]:
        return sorted([*self.records, *self.events], key=lambda entry: entry.sequence)

    def verify_chain(self) -> Any:
        return self.verification

    def replay(self, decision_id: str) -> Any:
        return self._replay

    def get_policy(self, policy_id: str, version: str) -> Any:
        return self.policies.get((policy_id, version))


@dataclass(frozen=True)
class Verification:
    """Stand-in for ``mizan.audit.ChainVerification`` - the console reads it structurally."""

    ok: bool
    length: int
    first_bad_sequence: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class Replayed:
    """Stand-in for ``mizan.replay.ReplayResult``."""

    decision_id: str
    mode: str = "exact"
    identical: bool = True
    original_verdict: str = "APPROVE"
    replayed_verdict: str = "APPROVE"
    original_verdict_hash: str = "0" * 64
    replayed_verdict_hash: str = "0" * 64
    detail: str = "Exact decision replay reproduced the recorded verdict."
    engine_version_matches: bool = True
    recorded_engine_version: str = "0.1.0"
    running_engine_version: str = "0.1.0"


def tainted_record(payload: str, *, sequence: int = 1, verdict: str = "APPROVE") -> Any:
    """A fully valid DecisionRecord whose every free-text field carries ``payload``.

    Only fields the frozen contracts actually allow free text in are used: identifiers such as ``symbol``,
    ``agent_id`` and ``policy_id`` are pattern-constrained and cannot hold a payload at all, which is itself
    part of the defence.
    """
    short = payload[:256]
    policy = make_policy()
    proposal = make_proposal(reasoning=payload)
    context = make_context(policy=policy)
    reject = verdict == "REJECT"
    check_override: dict[str, Any] = {"detail": payload[:4000], "data_source": short}
    if reject:
        check_override.update(
            {"passed": False, "severity": "blocking", "reason_code": "RESTRICTED_SYMBOL"}
        )
    checks = make_checks(policy, restricted_symbol=check_override)
    evaluation_overrides: dict[str, Any] = {"checks": checks}
    if reject:
        evaluation_overrides.update(
            {"verdict": "REJECT", "reason_codes": ["RESTRICTED_SYMBOL"], "recommended_quantity": "0"}
        )
    evaluation = make_evaluation(
        proposal=proposal, context=context, policy_snapshot=policy, **evaluation_overrides
    )
    advisory = make_advisory(reasoning=payload, provider_ref=short, profile=short)
    decision = make_decision(proposal=proposal, evaluation=evaluation, llm_advisory=advisory)
    authorization = None
    execution = None
    if not reject:
        authorization = make_authorization(
            proposal=proposal, decision=decision, context=context, policy_snapshot=policy
        )
        execution = make_execution_result(
            authorization=authorization,
            message=payload[:4000],
            broker={"name": short, "environment": "paper"},
            broker_status=short,
        )
    return make_decision_record(
        policy_snapshot=policy,
        proposal=proposal,
        risk_context=context,
        risk_evaluation=evaluation,
        governor_decision=decision,
        authorization=authorization,
        execution=execution,
        sequence=sequence,
        audit_prev_hash="0" * 64 if sequence == 1 else "1" * 64,
    )


def advisory_only_record(payload: str, *, verdict: str = "REJECT") -> Any:
    """A record where ONLY the advisory carries ``payload``.

    That makes leakage provable: if the string turns up in an enforcement region, it can only have come from
    the advisory, because nothing else in the record contains it.
    """
    policy = make_policy()
    proposal = make_proposal(reasoning="Thesis: mean reversion after the gap.")
    context = make_context(policy=policy)
    reject = verdict == "REJECT"
    checks = make_checks(
        policy,
        restricted_symbol=(
            {
                "passed": False,
                "severity": "blocking",
                "reason_code": "RESTRICTED_SYMBOL",
                "detail": "the symbol is on the tenant restricted list",
            }
            if reject
            else {}
        ),
    )
    overrides: dict[str, Any] = {"checks": checks}
    if reject:
        overrides.update(
            {"verdict": "REJECT", "reason_codes": ["RESTRICTED_SYMBOL"], "recommended_quantity": "0"}
        )
    evaluation = make_evaluation(
        proposal=proposal, context=context, policy_snapshot=policy, **overrides
    )
    advisory = make_advisory(reasoning=payload)
    decision = make_decision(proposal=proposal, evaluation=evaluation, llm_advisory=advisory)
    authorization = None
    execution = None
    if not reject:
        authorization = make_authorization(
            proposal=proposal, decision=decision, context=context, policy_snapshot=policy
        )
        execution = make_execution_result(authorization=authorization)
    return make_decision_record(
        policy_snapshot=policy,
        proposal=proposal,
        risk_context=context,
        risk_evaluation=evaluation,
        governor_decision=decision,
        authorization=authorization,
        execution=execution,
    )
