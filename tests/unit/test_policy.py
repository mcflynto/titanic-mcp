"""Policy tests (PERM-01 to PERM-04)."""

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import POLICY_PATH
from tmcp.policy.engine import resolve
from tmcp.policy.models import Policy
from tmcp.policy.store import PolicyStore, parse_policy


def test_perm01_demo_policy_is_valid(policy_store: PolicyStore) -> None:
    assert {"statistician", "analyst", "sql-analyst", "editor", "approver"} <= set(policy_store.policy.roles)


@pytest.mark.parametrize(
    "text, message",
    [
        ("version: 1\nroles: {r: {tools: [drop_database]}}", "unknown tools"),
        ("version: 1\nroles: {r: {tools: [], datasets: {reference: {nope: {}}}}}", "unknown table"),
        (
            "version: 1\nroles: {r: {tools: [], datasets: {reference: {passengers: {columns: {deny: [x]}}}}}}",
            "unknown columns",
        ),
        (
            "version: 1\nroles: {r: {tools: [], datasets: {reference: {passengers: {row_filter: 'x = 1'}}}}}",
            "unknown column",
        ),
        ("version: 1\nroles: {}\nagents: {a: {roles: [missing]}}", "unknown roles"),
        ("version: 2\nroles: {}", "version"),
        ("version: 1\nroles: {r: {tools: [], unknown_key: 1}}", "Extra inputs"),
    ],
)
def test_perm01_rejects_invalid_policy(text: str, message: str) -> None:
    with pytest.raises((ValidationError, ValueError), match=message):
        parse_policy(text)


def test_deny_by_default_for_unknown_agent(policy_store: PolicyStore) -> None:
    perms = policy_store.permissions("not-in-policy")
    assert perms.tools == frozenset()
    assert perms.tables == {}


def test_perm03_token_roles_are_added_only_if_they_exist(policy_store: PolicyStore) -> None:
    perms = policy_store.permissions("not-in-policy", ["statistician", "superuser"])
    assert perms.roles == ("statistician",)


def test_perm04_union_of_roles(policy_store: PolicyStore) -> None:
    perms = policy_store.permissions("curation-agent")  # analyst + editor
    assert perms.can("query_passengers") and perms.can("propose_passenger_update")
    assert not perms.can("review_change_request")
    working = perms.table("working", "passengers")
    assert working is not None and "fare" in working.columns  # editor gives all columns
    reference = perms.table("reference", "passengers")
    assert reference is not None and "fare" not in reference.columns
    assert perms.limits.max_rows == 500


def test_perm04_row_filters_combine_with_or() -> None:
    policy = parse_policy(
        """
version: 1
roles:
  a: {tools: [query_passengers], datasets: {reference: {passengers: {row_filter: "class = 'First'"}}}}
  b: {tools: [query_passengers], datasets: {reference: {passengers: {row_filter: "class = 'Third'"}}}}
  c: {tools: [query_passengers], datasets: {reference: {passengers: {}}}}
agents:
  ab: {roles: [a, b]}
  ac: {roles: [a, c]}
"""
    )
    ab = resolve(policy, "ab").table("reference", "passengers")
    assert ab is not None and ab.row_filter is not None and " OR " in ab.row_filter.sql()
    ac = resolve(policy, "ac").table("reference", "passengers")
    assert ac is not None and ac.row_filter is None


def test_min_group_size_only_if_all_aggregating_roles_have_it(policy_store: PolicyStore) -> None:
    assert policy_store.permissions("reporting-agent").min_group_size == 5
    assert policy_store.permissions("churn-agent").min_group_size is None


def test_perm02_reload_keeps_old_policy_if_new_file_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    shutil.copy(POLICY_PATH, path)
    store = PolicyStore(path)
    old_version = store.version
    path.write_text("version: 1\nroles: {r: {tools: [nope]}}")
    assert store.reload_if_changed() is False
    assert store.version == old_version
    assert store.permissions("reporting-agent").can("aggregate_passengers")


def test_perm02_reload_applies_valid_change(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    shutil.copy(POLICY_PATH, path)
    store = PolicyStore(path)
    assert store.permissions("reporting-agent").can("aggregate_passengers")
    path.write_text("version: 1\nroles: {}\nagents: {}\n")
    assert store.reload_if_changed() is True
    assert not store.permissions("reporting-agent").can("aggregate_passengers")


def test_policy_model_accepts_wildcard_table() -> None:
    policy = Policy.model_validate(
        {"version": 1, "roles": {"r": {"tools": ["run_sql"], "datasets": {"reference": {"*": {}}}}}, "agents": {}}
    )
    assert resolve(policy, "x").tables == {}
