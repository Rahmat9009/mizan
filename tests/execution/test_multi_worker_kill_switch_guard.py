"""The kill switch must cover every worker, or the process refuses to start (security finding F-28 class).

A process-local kill switch behind N worker processes is the worst possible shape for a safety control:
the operator trips it, gets 200 and ``active: true``, one worker stops, and the other N-1 keep trading.
It reports success while failing. Refusing at construction is deliberate - the alternative is finding out
during the incident the switch exists for.
"""
from __future__ import annotations

import pytest

from mizan.contracts.errors import ConfigurationError
from mizan.execution import (
    WORKER_COUNT_VARIABLES,
    EnvKillSwitch,
    InMemoryKillSwitch,
    assert_kill_switch_covers_every_worker,
    configured_worker_count,
)


class SharedKillSwitch:
    """A stand-in for a future Redis/Postgres-backed switch: state shared across processes."""

    shared_state = True

    def is_active(self) -> bool:
        return False


@pytest.fixture(autouse=True)
def _no_worker_variables(monkeypatch):
    for name in WORKER_COUNT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_a_single_worker_boots():
    assert configured_worker_count() == 1
    assert assert_kill_switch_covers_every_worker(InMemoryKillSwitch()) is None


@pytest.mark.parametrize("variable", WORKER_COUNT_VARIABLES)
def test_every_worker_variable_is_recognised(variable, monkeypatch):
    """Missing one spelling is the same as having no guard for that deployment."""
    monkeypatch.setenv(variable, "4")
    assert configured_worker_count() == 4
    with pytest.raises(ConfigurationError):
        assert_kill_switch_covers_every_worker(InMemoryKillSwitch())


@pytest.mark.parametrize("value", ["banana", "", "  ", "2.5", "-1", "1e3"])
def test_an_unparseable_worker_count_refuses_rather_than_assuming_one(value, monkeypatch):
    """A variable we cannot read is not evidence of a single worker. Fail toward refusing to boot."""
    monkeypatch.setenv("WEB_CONCURRENCY", value)
    if value.strip() in {"", "-1"}:
        # empty is 'unset'; a negative count cannot mean more than one worker
        assert_kill_switch_covers_every_worker(InMemoryKillSwitch())
        return
    with pytest.raises(ConfigurationError):
        assert_kill_switch_covers_every_worker(InMemoryKillSwitch())


def test_the_highest_requested_count_wins(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setenv("GUNICORN_WORKERS", "6")
    assert configured_worker_count() == 6
    with pytest.raises(ConfigurationError):
        assert_kill_switch_covers_every_worker(InMemoryKillSwitch())


def test_env_kill_switch_is_also_refused(monkeypatch):
    """EnvKillSwitch re-reads on every call, but a running worker cannot see an edit to the parent's
    environment after it forked. It is a deploy-time control, not a runtime one, so it does not qualify."""
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    assert EnvKillSwitch.shared_state is False
    with pytest.raises(ConfigurationError):
        assert_kill_switch_covers_every_worker(EnvKillSwitch())


def test_a_shared_state_switch_permits_many_workers(monkeypatch):
    """The guard blocks the unsafe SHAPE, not multi-worker deployment itself - otherwise it would just
    be a scaling limit and someone would delete it."""
    monkeypatch.setenv("WEB_CONCURRENCY", "8")
    assert assert_kill_switch_covers_every_worker(SharedKillSwitch()) is None


def test_the_refusal_names_the_variable_and_the_remedy(monkeypatch):
    monkeypatch.setenv("UVICORN_WORKERS", "3")
    with pytest.raises(ConfigurationError) as excinfo:
        assert_kill_switch_covers_every_worker(InMemoryKillSwitch())
    text = f"{excinfo.value}"
    assert "UVICORN_WORKERS" in text, "the operator must be told which variable caused the refusal"
    assert "shared" in text.lower(), "the operator must be told what would fix it"


def test_a_switch_without_the_attribute_is_treated_as_process_local(monkeypatch):
    """Absence of a declaration is not a claim of safety."""

    class Undeclared:
        def is_active(self) -> bool:
            return False

    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(ConfigurationError):
        assert_kill_switch_covers_every_worker(Undeclared())


def test_the_shipped_switches_declare_themselves_process_local():
    assert InMemoryKillSwitch.shared_state is False
    assert EnvKillSwitch.shared_state is False
