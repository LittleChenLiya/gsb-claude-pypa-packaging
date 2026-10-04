# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

"""Tests for :mod:`packaging.diffexplain` -- the offline dependency-diff
explainer.

These tests are fully offline: they never query PyPI and never resolve
dependency versions.
"""

from __future__ import annotations

import pytest

from packaging.diffexplain import (
    DiffError,
    GroupResult,
    RequirementDiff,
    _change_str,
    _eval_atom,
    _extras_from_env,
    _status_str,
    collect_atoms,
    detect_conflicts,
    diff_environments,
    eval_group,
    evaluate_requirement,
    format_diff,
    load_environment,
    load_requirements,
    render_marker,
    specifier_set_satisfiable,
)
from packaging.markers import Marker
from packaging.specifiers import SpecifierSet


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


ENV_A = {
    "python_version": "3.10",
    "python_full_version": "3.10.0",
    "os_name": "posix",
    "sys_platform": "linux",
    "extras": ["fast"],
}

ENV_B = {
    "python_version": "3.12",
    "python_full_version": "3.12.0",
    "os_name": "posix",
    "sys_platform": "linux",
    "extras": [],
}


def _eval(marker_text: str, env: dict) -> GroupResult:
    marker = Marker(marker_text)
    return eval_group(marker._markers, env)


# ---------------------------------------------------------------------------
# Three-valued atom evaluation
# ---------------------------------------------------------------------------


class TestEvalAtom:
    def test_true_atom(self):
        atom = _eval_atom(
            Marker('python_version < "3.11"')._markers[0], ENV_A
        )
        assert atom.result is True
        assert atom.missing is None

    def test_false_atom(self):
        atom = _eval_atom(
            Marker('python_version < "3.11"')._markers[0], ENV_B
        )
        assert atom.result is False
        assert atom.missing is None

    def test_missing_field_is_unknown_not_false(self):
        # A missing environment field must be reported as unknown, never
        # silently treated as an empty string and forced to False.
        env = {"python_version": "3.10"}
        atom = _eval_atom(
            Marker('os_name == "posix"')._markers[0], env
        )
        assert atom.result is None
        assert atom.missing == "os_name"

    def test_missing_version_field_is_unknown(self):
        env = {"os_name": "posix"}
        atom = _eval_atom(
            Marker('python_version < "3.11"')._markers[0], env
        )
        assert atom.result is None
        assert atom.missing == "python_version"

    def test_set_valued_marker_on_lhs_is_unknown(self):
        # "extras" is set-valued; it cannot appear on the LHS of ==.
        env = {"extras": {"fast"}}
        atom = _eval_atom(
            Marker('extras == "fast"')._markers[0], env
        )
        assert atom.result is None


# ---------------------------------------------------------------------------
# Three-valued group evaluation (and / or)
# ---------------------------------------------------------------------------


class TestEvalGroup:
    def test_single_atom_true(self):
        group = _eval('python_version < "3.11"', ENV_A)
        assert group.result is True
        assert len(group.conjunctions) == 1
        assert len(collect_atoms(group)) == 1

    def test_single_atom_false(self):
        group = _eval('python_version < "3.11"', ENV_B)
        assert group.result is False

    def test_and_both_true(self):
        group = _eval(
            'os_name == "posix" and python_version < "3.11"', ENV_A
        )
        assert group.result is True
        assert len(group.conjunctions) == 1
        assert len(group.conjunctions[0].items) == 2

    def test_and_one_false(self):
        group = _eval(
            'os_name == "posix" and python_version < "3.11"', ENV_B
        )
        assert group.result is False

    def test_or_first_true(self):
        group = _eval(
            'os_name == "posix" or python_version < "3.11"', ENV_A
        )
        assert group.result is True

    def test_or_second_true(self):
        group = _eval(
            'os_name == "plan9" or python_version < "3.11"', ENV_A
        )
        assert group.result is True

    def test_or_both_false(self):
        group = _eval(
            'os_name == "plan9" or python_version < "3.11"', ENV_B
        )
        assert group.result is False

    def test_or_unknown_propagates(self):
        # One disjunct is unknown (missing field), the other is False.
        # unknown OR False = unknown (three-valued logic).
        env = {"python_version": "3.12"}
        group = _eval(
            'os_name == "plan9" or python_version < "3.11"', env
        )
        assert group.result is None

    def test_or_unknown_dominated_by_true(self):
        # unknown OR True = True (True dominates in disjunction).
        env = {"python_version": "3.10"}
        group = _eval(
            'os_name == "plan9" or python_version < "3.11"', env
        )
        assert group.result is True

    def test_and_unknown_propagates(self):
        env = {"python_version": "3.10"}
        group = _eval(
            'os_name == "posix" and python_version < "3.11"', env
        )
        assert group.result is None

    def test_nested_group(self):
        group = _eval(
            'os_name == "posix" and (python_version < "3.11" or os_name == "plan9")',
            ENV_A,
        )
        assert group.result is True

    def test_render_marker(self):
        group = _eval('python_version < "3.11"', ENV_A)
        rendered = render_marker(group)
        assert "python_version < \"3.11\"=True" in rendered

    def test_render_none(self):
        assert render_marker(None) == "(none)"


# ---------------------------------------------------------------------------
# Per-requirement evaluation (extras per caller set)
# ---------------------------------------------------------------------------


class TestEvaluateRequirement:
    def test_python_version_change_disables(self):
        req = evaluate_requirement(
            'foo; python_version < "3.11"', ENV_A, ENV_B
        )
        assert req.enabled_a is True
        assert req.enabled_b is False
        assert _change_str(req.enabled_a, req.enabled_b) == "enabled->disabled"

    def test_python_version_shows_conditional_value(self):
        req = evaluate_requirement(
            'foo; python_version < "3.11"', ENV_A, ENV_B
        )
        base_a = req.per_extra_a[""]
        base_b = req.per_extra_b[""]
        atoms_a = collect_atoms(base_a)
        atoms_b = collect_atoms(base_b)
        assert atoms_a[0].result is True
        assert atoms_b[0].result is False
        assert "python_version < \"3.11\"" in atoms_a[0].text

    def test_extra_fast_enabled_when_extras_include_fast(self):
        req = evaluate_requirement('bar; extra == "fast"', ENV_A, ENV_B)
        # In env A (extras=["fast"]), the fast extra enables the requirement.
        assert req.enabled_a is True
        # In env B (extras=[]), no extra enables it.
        assert req.enabled_b is False
        # The per-extra evaluation shows base=False, fast=True.
        assert req.per_extra_a[""].result is False
        assert req.per_extra_a["fast"].result is True

    def test_extra_fast_disabled_when_no_extras(self):
        req = evaluate_requirement('bar; extra == "fast"', ENV_B, ENV_B)
        assert req.enabled_a is False
        assert req.enabled_b is False

    def test_no_marker_always_enabled(self):
        req = evaluate_requirement("baz>=1.0", ENV_A, ENV_B)
        assert req.enabled_a is True
        assert req.enabled_b is True
        assert req.marker_text is None

    def test_missing_field_is_unknown(self):
        env_a = {"python_version": "3.10"}
        env_b = {"python_version": "3.12"}
        req = evaluate_requirement(
            'qux; os_name == "plan9"', env_a, env_b
        )
        assert req.enabled_a is None
        assert req.enabled_b is None
        assert _change_str(req.enabled_a, req.enabled_b) == "unchanged (unknown)"

    def test_extra_canonicalization(self):
        # "Fast" should be canonicalized to "fast".
        env = {"extras": ["Fast"]}
        extras = _extras_from_env(env)
        assert extras == ["fast"]

    def test_extras_from_env_accepts_string(self):
        env = {"extras": "fast"}
        assert _extras_from_env(env) == ["fast"]

    def test_extras_from_env_accepts_extra_key(self):
        env = {"extra": "fast"}
        assert _extras_from_env(env) == ["fast"]

    def test_extras_from_env_empty(self):
        assert _extras_from_env({}) == []

    def test_invalid_requirement_raises(self):
        with pytest.raises(DiffError):
            evaluate_requirement("foo; python_version <", ENV_A, ENV_B)


# ---------------------------------------------------------------------------
# Duplicate aggregation and conflict detection
# ---------------------------------------------------------------------------


class TestConflictDetection:
    def test_conflicting_specifiers_detected(self):
        s1 = SpecifierSet(">=2.0")
        s2 = SpecifierSet("<1.0")
        assert specifier_set_satisfiable(s1 & s2) is False

    def test_compatible_specifiers(self):
        s1 = SpecifierSet(">=1.0")
        s2 = SpecifierSet("<2.0")
        assert specifier_set_satisfiable(s1 & s2) is True

    def test_exact_version_conflict(self):
        s1 = SpecifierSet("==1.0")
        s2 = SpecifierSet("==2.0")
        assert specifier_set_satisfiable(s1 & s2) is False

    def test_same_version_compatible(self):
        s1 = SpecifierSet("==1.0")
        s2 = SpecifierSet("==1.0")
        assert specifier_set_satisfiable(s1 & s2) is True

    def test_not_equal_excludes_point(self):
        s1 = SpecifierSet("==1.0")
        s2 = SpecifierSet("!=1.0")
        assert specifier_set_satisfiable(s1 & s2) is False

    def test_not_equal_allows_others(self):
        s1 = SpecifierSet(">1.0")
        s2 = SpecifierSet("!=1.5")
        assert specifier_set_satisfiable(s1 & s2) is True

    def test_compatible_release(self):
        # ~=1.4.5 means >=1.4.5, <1.5
        s1 = SpecifierSet("~=1.4.5")
        s2 = SpecifierSet("<1.5")
        assert specifier_set_satisfiable(s1 & s2) is True
        s3 = SpecifierSet(">=1.5")
        assert specifier_set_satisfiable(s1 & s3) is False

    def test_prefix_match(self):
        # ==1.4.* means >=1.4, <1.5
        s1 = SpecifierSet("==1.4.*")
        s2 = SpecifierSet("<1.5")
        assert specifier_set_satisfiable(s1 & s2) is True
        s3 = SpecifierSet(">=1.5")
        assert specifier_set_satisfiable(s1 & s3) is False

    def test_empty_specifier_set(self):
        assert specifier_set_satisfiable(SpecifierSet("")) is True

    def test_detect_conflicts_in_group(self):
        items = [
            evaluate_requirement("foo>=2.0", ENV_A, ENV_B),
            evaluate_requirement("foo<1.0", ENV_A, ENV_B),
        ]
        conflicts = detect_conflicts(items)
        assert len(conflicts) == 1
        assert conflicts[0].spec_a == ">=2.0"
        assert conflicts[0].spec_b == "<1.0"

    def test_no_conflict_when_compatible(self):
        items = [
            evaluate_requirement("foo>=1.0", ENV_A, ENV_B),
            evaluate_requirement("foo<2.0", ENV_A, ENV_B),
        ]
        conflicts = detect_conflicts(items)
        assert len(conflicts) == 0


# ---------------------------------------------------------------------------
# Diff orchestration
# ---------------------------------------------------------------------------


class TestDiffEnvironments:
    def test_diff_groups_by_canonical_name(self):
        sources = ["Foo>=1.0", "foo<2.0", "BAR"]
        diff = diff_environments(ENV_A, ENV_B, sources)
        assert "foo" in diff.groups
        assert "bar" in diff.groups
        assert len(diff.groups["foo"]) == 2
        assert len(diff.groups["bar"]) == 1

    def test_diff_detects_conflicts(self):
        sources = ["dup>=2.0", "dup<1.0"]
        diff = diff_environments(ENV_A, ENV_B, sources)
        assert "dup" in diff.conflicts
        assert len(diff.conflicts["dup"]) == 1

    def test_diff_no_conflicts_for_unique_names(self):
        sources = ["foo>=1.0", "bar>=2.0"]
        diff = diff_environments(ENV_A, ENV_B, sources)
        assert diff.conflicts == {}

    def test_format_diff_contains_key_info(self):
        sources = [
            'foo; python_version < "3.11"',
            'bar; extra == "fast"',
            "baz>=1.0",
            'qux; os_name == "plan9"',
            'quux; platform_release > "5.0"',
            "dup>=2.0",
            "dup<1.0",
        ]
        diff = diff_environments(ENV_A, ENV_B, sources)
        output = format_diff(diff)
        # Summary table
        assert "enabled->disabled" in output
        assert "unchanged" in output
        # Unknown field
        assert "unknown (missing: platform_release)" in output
        # Conflict
        assert "CONFLICT" in output
        assert ">=2.0" in output
        assert "<1.0" in output
        # and/or explanation
        assert " and " in output or " or " in output


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------


class TestFileLoading:
    def test_load_environment(self):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write('{"python_version": "3.10"}')
            path = f.name
        try:
            env = load_environment(path)
            assert env == {"python_version": "3.10"}
        finally:
            os.unlink(path)

    def test_load_environment_missing(self):
        with pytest.raises(DiffError):
            load_environment("/nonexistent/env.json")

    def test_load_environment_invalid_json(self):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write("not json")
            path = f.name
        try:
            with pytest.raises(DiffError):
                load_environment(path)
        finally:
            os.unlink(path)

    def test_load_requirements(self):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as f:
            f.write(
                "# comment\n"
                "foo>=1.0\n"
                "\n"
                "bar; python_version < '3.11'\n"
            )
            path = f.name
        try:
            sources = load_requirements(path)
            assert sources == ["foo>=1.0", "bar; python_version < '3.11'"]
        finally:
            os.unlink(path)

    def test_load_requirements_missing(self):
        with pytest.raises(DiffError):
            load_requirements("/nonexistent/req.txt")


# ---------------------------------------------------------------------------
# Status / change strings
# ---------------------------------------------------------------------------


class TestStatusStrings:
    def test_status_str(self):
        assert _status_str(True) == "enabled"
        assert _status_str(False) == "disabled"
        assert _status_str(None) == "unknown"

    def test_change_str(self):
        assert _change_str(True, False) == "enabled->disabled"
        assert _change_str(False, True) == "disabled->enabled"
        assert _change_str(None, None) == "unchanged (unknown)"
        assert _change_str(True, True) == "unchanged"
        assert _change_str(False, False) == "unchanged"
