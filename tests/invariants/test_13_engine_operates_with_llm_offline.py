"""Invariant 13 - Hard Rule E8: the deterministic engine runs and rejects with the LLM entirely offline.

Pass criterion: with socket.socket / socket.create_connection / socket.getaddrinfo monkeypatched to raise,
mizan.risk.evaluate produces a verdict, mizan.governor.govern(advisory=None) produces a decision, and
mizan.advisory.get_advisory with a provider that raises ConnectionError returns an opinion with invoked=True,
available=False, recommendation=None that leaves the deterministic decision unchanged (same verdict, quantity and
verdict_hash); a hard REJECT stands offline; timeouts, garbage and malformed provider output never raise; a policy
that fails closed on advisory unavailability still yields a deterministic REJECT (ADVISORY_UNAVAILABLE); and no
file under mizan/risk, mizan/governor, mizan/policy, mizan/authorization imports openai, anthropic, httpx,
requests, aiohttp, urllib, socket (or any other network client, or mizan.advisory).
"""
from __future__ import annotations

import socket

import pytest

from mizan import advisory, governor, risk
from mizan.contracts import AdvisoryOpinion

from tests.fixtures import make_policy, make_proposal
from tests.invariants._support import (
    ScriptedAdvisoryProvider,
    codes,
    context_for,
    imported_modules,
    linked_evaluation,
    offenders_message,
    parse,
    python_files,
    quantity_of,
    rel,
)

FORBIDDEN_IMPORTS = {
    "openai", "anthropic", "httpx", "requests", "aiohttp", "urllib", "urllib3", "socket", "http", "ssl",
    "websocket", "websockets", "grpc", "boto3", "google", "litellm", "mizan.advisory", "mizan.console",
}


def _go_offline(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise RuntimeError("network access attempted while the invariant suite is offline")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    with pytest.raises(RuntimeError):
        socket.create_connection(("127.0.0.1", 9))


def _assert_unavailable(opinion: AdvisoryOpinion):
    assert isinstance(opinion, AdvisoryOpinion)
    assert opinion.invoked is True
    assert opinion.available is False
    assert opinion.recommendation is None


def test_engine_operates_with_llm_offline(monkeypatch):
    _go_offline(monkeypatch)
    policy = make_policy()
    context = context_for(policy)
    proposal = make_proposal()

    evaluation = risk.evaluate(proposal, context, policy)
    assert evaluation.verdict in {"PASS", "REDUCE", "REJECT"}

    baseline = governor.govern(proposal, evaluation, policy, None, context=context)
    assert baseline.verdict in {"APPROVE", "REDUCE", "REJECT"}

    provider = ScriptedAdvisoryProvider(raises=ConnectionError("LLM endpoint unreachable"))
    opinion = advisory.get_advisory(provider, proposal, evaluation, context, policy)
    assert provider.calls == 1
    _assert_unavailable(opinion)

    decision = governor.govern(proposal, evaluation, policy, opinion, context=context)
    assert decision.verdict == baseline.verdict
    assert quantity_of(decision.authorized) == quantity_of(baseline.authorized)
    assert decision.verdict_hash == baseline.verdict_hash
    assert decision.llm_advisory is not None and decision.llm_advisory.available is False

    # a deterministic hard rejection is enforced offline too
    rejected = linked_evaluation(proposal, context, policy, verdict="REJECT")
    blocked = governor.govern(proposal, rejected, policy, opinion, context=context)
    assert blocked.verdict == "REJECT"
    assert blocked.authorized.total_quantity == "0"
    assert "HARD_REJECTION_UPHELD" in codes(blocked)


def test_advisory_failures_never_raise_and_never_change_the_deterministic_verdict(monkeypatch):
    _go_offline(monkeypatch)
    policy = make_policy()
    context = context_for(policy)
    proposal = make_proposal()
    evaluation = risk.evaluate(proposal, context, policy)
    baseline = governor.govern(proposal, evaluation, policy, None, context=context)

    failures = [
        ScriptedAdvisoryProvider(raises=TimeoutError("advisory timed out")),
        ScriptedAdvisoryProvider(raises=ValueError("truncated JSON")),
        ScriptedAdvisoryProvider(raises=RuntimeError("HTTP 500")),
        ScriptedAdvisoryProvider(raises=OSError("network is unreachable")),
        ScriptedAdvisoryProvider(result=None),
        ScriptedAdvisoryProvider(result={"recommendation": "CONCUR", "extra_field": 1}),
        ScriptedAdvisoryProvider(result='{"recommendation": "CONC'),
    ]
    for provider in failures:
        opinion = advisory.get_advisory(provider, proposal, evaluation, context, policy)
        _assert_unavailable(opinion)
        decision = governor.govern(proposal, evaluation, policy, opinion, context=context)
        assert decision.verdict == baseline.verdict
        assert decision.verdict_hash == baseline.verdict_hash


def test_fail_closed_on_advisory_unavailable_is_still_a_deterministic_decision(monkeypatch):
    _go_offline(monkeypatch)
    policy = make_policy(
        fail_closed={
            "on_missing_market_data": True,
            "on_missing_portfolio_state": True,
            "on_engine_degraded": True,
            "on_advisory_unavailable": True,
        }
    )
    assert policy.fail_closed.on_advisory_unavailable is True
    context = context_for(policy)
    proposal = make_proposal()
    evaluation = risk.evaluate(proposal, context, policy)
    for opinion in (None, advisory.get_advisory(
        ScriptedAdvisoryProvider(raises=ConnectionError()), proposal, evaluation, context, policy
    )):
        decision = governor.govern(proposal, evaluation, policy, opinion, context=context)
        assert decision.verdict == "REJECT"
        assert "ADVISORY_UNAVAILABLE" in codes(decision), codes(decision)
        assert decision.authorized.total_quantity == "0"


def test_engine_modules_import_no_network_or_llm_clients():
    offenders: list[str] = []
    for path in python_files("risk", "governor", "policy", "authorization"):
        for module, line in imported_modules(parse(path)):
            top = module.split(".")[0]
            if module in FORBIDDEN_IMPORTS or top in FORBIDDEN_IMPORTS:
                offenders.append(f"{rel(path)}:{line}: import of {module}")
    assert not offenders, offenders_message("network/LLM imports in the deterministic engine", offenders)
