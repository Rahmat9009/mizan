"""Configuration and the two kill switches: the machinery that must be impossible to misconfigure.

These are the smallest objects in the lane and the ones with the least room for a benign mistake. A
kill switch that caches its state is a switch that cannot be thrown. A configuration that defaults to
"enabled" is a deployment that trades before anyone asked it to. An ``ALPACA_PAPER`` that is absent and
treated as permission is the whole of Hard Rule B1, lost in a default argument.

Every environment variable here is set through ``monkeypatch``, which restores the process environment
at the end of each test - nothing this file does outlives it.

Self-contained by design: no shared fixtures, so a change elsewhere cannot quietly re-point it.
"""

from __future__ import annotations

import threading

import pytest
from pydantic import ValidationError

from mizan.contracts.errors import LiveTradingForbidden
from mizan.execution import (
    CHECK_ORDER,
    EnvKillSwitch,
    ExecutionConfig,
    InMemoryKillSwitch,
    KillSwitch,
)

TRUE_SPELLINGS = ("1", "true", "TRUE", "True", "yes", "on", "  true  ", "\tON\n")
FALSE_SPELLINGS = ("0", "false", "FALSE", "no", "off", "", "   ")
#: Neither true nor false. For the kill switch these mean "active"; for a config flag they are an error.
GARBAGE = ("maybe", "banana", "2", "null", "None", "-1")


# ---------------------------------------------------------------------------------------------
# The documented order
# ---------------------------------------------------------------------------------------------
def test_the_check_order_is_the_documented_one_and_ends_with_the_switch_then_the_mutation():
    assert CHECK_ORDER == (
        "execution_enabled",
        "authorization_valid",
        "idempotency",
        "toctou_revalidation",
        "authorization_consumed",
        "authorization_fresh",
        "kill_switch",
        "submit",
    )
    # E4 restated as a property of the constant itself, so a reordering is a failing test
    assert CHECK_ORDER[-1] == "submit"
    assert CHECK_ORDER[-2] == "kill_switch"
    assert CHECK_ORDER.index("authorization_consumed") < CHECK_ORDER.index("authorization_fresh")


# ---------------------------------------------------------------------------------------------
# InMemoryKillSwitch
# ---------------------------------------------------------------------------------------------
def test_the_in_memory_switch_starts_inactive_and_flips_both_ways():
    switch = InMemoryKillSwitch()
    assert isinstance(switch, KillSwitch)
    assert switch.is_active() is False
    switch.activate()
    assert switch.is_active() is True
    switch.deactivate()
    assert switch.is_active() is False


def test_an_in_memory_switch_can_be_constructed_already_active():
    assert InMemoryKillSwitch(active=True).is_active() is True


def test_the_in_memory_switch_is_safe_to_read_while_it_is_being_flipped():
    """It is read on the hot path from whatever thread is executing; a torn read is not acceptable."""
    switch = InMemoryKillSwitch()
    seen: list[bool] = []
    stop = threading.Event()

    def flipper() -> None:
        while not stop.is_set():
            switch.activate()
            switch.deactivate()

    def reader() -> None:
        for _ in range(2000):
            seen.append(switch.is_active())

    writer = threading.Thread(target=flipper, daemon=True)
    writer.start()
    reader()
    stop.set()
    writer.join(timeout=5)

    assert len(seen) == 2000
    assert all(isinstance(value, bool) for value in seen)


# ---------------------------------------------------------------------------------------------
# EnvKillSwitch
# ---------------------------------------------------------------------------------------------
def test_the_env_switch_names_the_variable_an_operator_would_set():
    assert EnvKillSwitch.variable == "MIZAN_KILL_SWITCH"


def test_an_unset_variable_is_not_an_active_switch(monkeypatch):
    monkeypatch.delenv("MIZAN_KILL_SWITCH", raising=False)
    assert EnvKillSwitch().is_active() is False


@pytest.mark.parametrize("value", TRUE_SPELLINGS)
def test_every_spelling_of_yes_trips_the_env_switch(value, monkeypatch):
    monkeypatch.setenv("MIZAN_KILL_SWITCH", value)
    assert EnvKillSwitch().is_active() is True


@pytest.mark.parametrize("value", FALSE_SPELLINGS)
def test_every_spelling_of_no_leaves_the_env_switch_alone(value, monkeypatch):
    monkeypatch.setenv("MIZAN_KILL_SWITCH", value)
    assert EnvKillSwitch().is_active() is False


@pytest.mark.parametrize("value", GARBAGE)
def test_an_unparseable_value_is_treated_as_ACTIVE(value, monkeypatch):
    """The switch fails safe. A typo in the one variable that stops trading must stop trading."""
    monkeypatch.setenv("MIZAN_KILL_SWITCH", value)
    assert EnvKillSwitch().is_active() is True


def test_the_env_switch_re_reads_the_environment_on_every_single_call(monkeypatch):
    """A cached value is a switch that cannot be thrown without a redeploy - which is not a switch."""
    switch = EnvKillSwitch()
    monkeypatch.setenv("MIZAN_KILL_SWITCH", "false")
    assert switch.is_active() is False
    monkeypatch.setenv("MIZAN_KILL_SWITCH", "true")
    assert switch.is_active() is True, "the second reading must see the new value"
    monkeypatch.setenv("MIZAN_KILL_SWITCH", "off")
    assert switch.is_active() is False


# ---------------------------------------------------------------------------------------------
# ExecutionConfig
# ---------------------------------------------------------------------------------------------
def test_the_defaults_are_the_safe_ones():
    config = ExecutionConfig()
    assert config.paper is True
    assert config.enabled is False, "a fresh deployment does not trade until someone says so"
    assert config.dry_run is True, "and when it does, it dry-runs until someone says otherwise"


def test_the_config_is_frozen_and_closed():
    config = ExecutionConfig()
    with pytest.raises(ValidationError):
        config.enabled = True  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ExecutionConfig(live=True)  # type: ignore[call-arg]


def test_there_is_no_way_to_spell_a_non_paper_configuration():
    """B1: the type has no representation for it, so no flag, route or helper can produce one."""
    with pytest.raises(ValidationError):
        ExecutionConfig(paper=False)


def test_from_environment_reads_the_two_flags_when_paper_is_explicit(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("MIZAN_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("MIZAN_EXECUTION_DRY_RUN", "false")

    config = ExecutionConfig.from_environment()
    assert (config.paper, config.enabled, config.dry_run) == (True, True, False)


def test_from_environment_defaults_both_flags_closed_when_only_paper_is_set(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.delenv("MIZAN_EXECUTION_ENABLED", raising=False)
    monkeypatch.delenv("MIZAN_EXECUTION_DRY_RUN", raising=False)

    config = ExecutionConfig.from_environment()
    assert config.enabled is False and config.dry_run is True


@pytest.mark.parametrize("value", ("false", "0", "no", "off", "", "   ", "paper", "maybe"))
def test_anything_but_an_explicit_true_ALPACA_PAPER_is_refused(value, monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", value)
    with pytest.raises(LiveTradingForbidden):
        ExecutionConfig.from_environment()


def test_an_absent_ALPACA_PAPER_is_refused_even_when_execution_is_switched_on(monkeypatch):
    """An unset variable is not permission - least of all in the presence of MIZAN_EXECUTION_ENABLED."""
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    monkeypatch.setenv("MIZAN_EXECUTION_ENABLED", "true")
    with pytest.raises(LiveTradingForbidden):
        ExecutionConfig.from_environment()


@pytest.mark.parametrize("variable", ("MIZAN_EXECUTION_ENABLED", "MIZAN_EXECUTION_DRY_RUN"))
@pytest.mark.parametrize("value", GARBAGE)
def test_an_unparseable_flag_is_an_error_rather_than_a_guess(variable, value, monkeypatch):
    """Guessing at "banana" is how a dry run becomes a live submission. It raises instead."""
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv(variable, value)
    with pytest.raises(LiveTradingForbidden):
        ExecutionConfig.from_environment()
