"""Invariant 17 - API-SURFACE section 0 / Master Plan section 11: `reasoning` is audit-only, never enforcement input.

Pass criterion: two proposals identical except for reasoning ("" versus the prompt-injection text from
tests.fixtures.injection_reasoning()) share the same proposal_id; mizan.risk.evaluate returns byte-identical
canonical JSON (same evaluation_id) for both; mizan.governor.govern returns the same verdict_hash and the same
canonical JSON once decision_id is removed; the injection text appears nowhere in the evaluation or the decision;
and an AST scan finds no attribute access, subscript, getattr or string constant "reasoning" in any file under
mizan/risk, mizan/governor, mizan/policy, mizan/authorization, mizan/execution (offenders listed as file:line).
"""
from __future__ import annotations

import ast

from mizan import governor, risk
from mizan.contracts.canonical import canonical_json, proposal_id_for

from tests.fixtures import injection_reasoning, make_policy, make_proposal
from tests.invariants._support import (
    context_for,
    offenders_message,
    opinion,
    parse,
    python_files,
    quantity_of,
    rel,
)

ENFORCEMENT_PACKAGES = ("risk", "governor", "policy", "authorization", "execution")


def _reasoning_offenders(path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(parse(path)):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.Attribute) and node.attr == "reasoning":
            found.append(f"{rel(path)}:{line}: attribute .reasoning")
        elif isinstance(node, ast.Subscript):
            index = node.slice
            if isinstance(index, ast.Constant) and index.value == "reasoning":
                found.append(f"{rel(path)}:{line}: subscript ['reasoning']")
        elif isinstance(node, ast.Call):
            func = node.func
            is_getattr = isinstance(func, ast.Name) and func.id in {"getattr", "hasattr"}
            is_get = isinstance(func, ast.Attribute) and func.attr in {"get", "pop"}
            if (is_getattr or is_get) and any(
                isinstance(a, ast.Constant) and a.value == "reasoning" for a in node.args
            ):
                found.append(f"{rel(path)}:{line}: dynamic access to 'reasoning'")
        elif isinstance(node, ast.Constant) and node.value == "reasoning":
            found.append(f"{rel(path)}:{line}: string constant 'reasoning'")
    return sorted(set(found))


def _strip_decision_id(decision) -> str:
    payload = decision.model_dump(mode="json")
    payload.pop("decision_id")
    return canonical_json(payload)


def test_reasoning_field_never_reaches_enforcement():
    policy = make_policy()
    context = context_for(policy)
    injection = injection_reasoning()
    assert injection and injection.strip(), "injection_reasoning() must be non-empty"

    clean = make_proposal(reasoning="")
    poisoned = make_proposal(reasoning=injection)
    assert clean.reasoning == "" and poisoned.reasoning == injection
    assert clean.proposal_id == poisoned.proposal_id
    assert proposal_id_for(poisoned.model_dump(mode="json")) == clean.proposal_id

    clean_eval = risk.evaluate(clean, context, policy)
    poisoned_eval = risk.evaluate(poisoned, context, policy)
    assert canonical_json(clean_eval) == canonical_json(poisoned_eval)
    assert clean_eval.evaluation_id == poisoned_eval.evaluation_id
    assert injection not in canonical_json(poisoned_eval)

    clean_decision = governor.govern(clean, clean_eval, policy, None, context=context)
    poisoned_decision = governor.govern(poisoned, poisoned_eval, policy, None, context=context)
    assert clean_decision.verdict_hash == poisoned_decision.verdict_hash
    assert _strip_decision_id(clean_decision) == _strip_decision_id(poisoned_decision)
    assert quantity_of(clean_decision.authorized) == quantity_of(poisoned_decision.authorized)
    assert injection not in canonical_json(poisoned_decision)

    offenders: list[str] = []
    for path in python_files(*ENFORCEMENT_PACKAGES):
        offenders.extend(_reasoning_offenders(path))
    assert not offenders, offenders_message("`reasoning` read inside the enforcement path", offenders)


def test_injected_advisory_reasoning_does_not_change_the_verdict():
    """The advisory's own free text is audit-only too: identical opinions with different reasoning govern alike."""
    policy = make_policy()
    context = context_for(policy)
    proposal = make_proposal()
    evaluation = risk.evaluate(proposal, context, policy)
    plain = governor.govern(proposal, evaluation, policy, opinion("CONCUR"), context=context)
    injected = governor.govern(
        proposal, evaluation, policy, opinion("CONCUR", reasoning=injection_reasoning()), context=context
    )
    assert plain.verdict == injected.verdict
    assert plain.verdict_hash == injected.verdict_hash
    assert quantity_of(plain.authorized) == quantity_of(injected.authorized)
    assert sorted(plain.reason_codes) == sorted(injected.reason_codes)
