"""The policy editor: two versions, field by field, each with its hash."""

from __future__ import annotations

from mizan.console import views
from mizan.console.render import fragment, render_policy_editor
from tests.console._helpers import FakeClient, assert_inert, region_text
from tests.fixtures import POLICY_ID, make_policy


def _versions() -> tuple:
    old = make_policy(restricted={"symbols": [], "strategies": []})
    new = make_policy(
        policy_version="2.0.0",
        portfolio={
            "max_single_symbol_pct": "0.10",
            "max_sector_concentration_pct": "0.25",
            "max_drawdown_pct": "0.20",
            "max_buying_power_utilization": "0.5",
        },
        restricted={"symbols": ["TSLA"], "strategies": []},
    )
    return old, new


def _client() -> FakeClient:
    old, new = _versions()
    return FakeClient(
        policies={(POLICY_ID, old.policy_version): old, (POLICY_ID, new.policy_version): new}
    )


def test_the_diff_is_field_level_and_names_the_exact_path() -> None:
    old, new = _versions()
    rows = views.policy_diff_rows(old, new)
    paths = {row["path"]: row for row in rows}
    assert "portfolio.max_single_symbol_pct" in paths
    assert paths["portfolio.max_single_symbol_pct"]["old"] == "0.15"
    assert paths["portfolio.max_single_symbol_pct"]["new"] == "0.1"
    assert paths["portfolio.max_single_symbol_pct"]["change"] == "changed"


def test_an_added_field_is_reported_as_added_and_a_dropped_one_as_removed() -> None:
    old, new = _versions()
    rows = {row["path"]: row for row in views.policy_diff_rows(old, new)}
    assert rows["restricted.symbols[0]"]["change"] == "added"
    assert rows["restricted.symbols[0]"]["new"] == "TSLA"
    assert rows["restricted.symbols[0]"]["old"] is None
    assert rows["restricted.symbols"]["change"] == "removed"


def test_unchanged_fields_are_omitted_unless_asked_for() -> None:
    old, new = _versions()
    changed = views.policy_diff_rows(old, new)
    assert all(row["change"] != "unchanged" for row in changed)
    everything = views.policy_diff_rows(old, new, include_unchanged=True)
    assert len(everything) > len(changed)


def test_both_policy_hashes_are_shown_next_to_their_versions() -> None:
    old, new = _versions()
    view = views.policy_editor_view(_client(), POLICY_ID, old.policy_version, new.policy_version)
    assert view["old"] == {"version": old.policy_version, "hash": old.policy_hash}
    assert view["new"] == {"version": new.policy_version, "hash": new.policy_hash}
    text = region_text(fragment(render_policy_editor(view)), "policy_editor")
    assert old.policy_hash in text
    assert new.policy_hash in text


def test_the_editor_says_what_happens_on_activation() -> None:
    old, new = _versions()
    view = views.policy_editor_view(_client(), POLICY_ID, old.policy_version, new.policy_version)
    text = region_text(fragment(render_policy_editor(view)), "policy_editor")
    assert "recomputed from the content" in text
    assert "refused at load" in text
    assert "policy_activated control event" in text


def test_a_version_this_tenant_cannot_see_is_not_distinguished_from_a_missing_one() -> None:
    view = views.policy_editor_view(_client(), POLICY_ID, "1.4.0", "does-not-exist")
    assert view["available"] is False
    assert "not visible to this tenant" in view["message"]
    assert views.policy_diff_view(_client(), POLICY_ID, "1.4.0", "does-not-exist") == []


def test_a_tainted_policy_field_is_escaped_in_the_diff() -> None:
    payload = "<img src=x onerror=alert(1)>"
    old = make_policy()
    new = make_policy(
        policy_version="2.0.0",
        advisory={"enabled": True, "profile": payload, "authority_ceiling": "reduce_or_reject"},
    )
    client = FakeClient(
        policies={(POLICY_ID, old.policy_version): old, (POLICY_ID, new.policy_version): new}
    )
    view = views.policy_editor_view(client, POLICY_ID, old.policy_version, new.policy_version)
    html = fragment(render_policy_editor(view))
    assert_inert(html, "policy editor")
    assert payload in region_text(html, "policy_editor")


def test_identical_versions_say_so_rather_than_showing_an_empty_table() -> None:
    old = make_policy()
    client = FakeClient(policies={(POLICY_ID, old.policy_version): old})
    view = views.policy_editor_view(client, POLICY_ID, old.policy_version, old.policy_version)
    text = region_text(fragment(render_policy_editor(view)), "policy_editor")
    assert "identical field for field" in text
