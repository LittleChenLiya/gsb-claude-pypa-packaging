"""Tests for tasks/marker_diff.py -- the offline dependency marker diff explainer."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tasks"))

import marker_diff  # noqa: E402
from marker_diff import Bool, Leaf, Tri  # noqa: E402


def make_env(python_version: str, python_full_version: str) -> dict:
    return {
        "implementation_name": "cpython",
        "implementation_version": python_full_version,
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_release": "5.15.0",
        "platform_system": "Linux",
        "platform_version": "#1 SMP",
        "python_full_version": python_full_version,
        "platform_python_implementation": "CPython",
        "python_version": python_version,
        "sys_platform": "linux",
    }


ENV_A = make_env("3.10", "3.10.14")
ENV_B = make_env("3.12", "3.12.5")


def test_python_version_change_disables_requirement():
    report = marker_diff.diff_environments(
        'foo; python_version < "3.11"\n',
        ENV_A,
        ENV_B,
        env_a_name="py310",
        env_b_name="py312",
    )
    assert len(report.changes) == 1
    change = report.changes[0]
    assert change.kind == "disabled"
    assert change.before.result == Tri.TRUE
    assert change.after.result == Tri.FALSE
    # condition values are kept on the explanation leaves
    leaf_a, leaf_b = marker_diff.changed_leaves(change.before, change.after)[0]
    assert leaf_a.env_value == "3.10"
    assert leaf_b.env_value == "3.12"
    text = marker_diff.render_report(report)
    assert "[DISABLED] foo" in text
    assert 'python_version < "3.11"' in text
    assert '"3.10" -> "3.12"' in text


def test_extra_enabled_when_extras_set_contains_fast():
    reqs = 'bar; extra == "fast"\n'
    # caller extras set without "fast": disabled in both envs -> no change
    report = marker_diff.diff_environments(reqs, ENV_A, ENV_B, extras_sets=[frozenset()])
    assert report.changes == []
    # caller extras set with "fast": enabled in both envs -> no change either
    report = marker_diff.diff_environments(
        reqs, ENV_A, ENV_B, extras_sets=[frozenset({"fast"})]
    )
    assert report.changes == []
    # evaluate each set separately and check the per-set results
    node_off = marker_diff.evaluate_marker(
        marker_diff.Requirement('bar; extra == "fast"').marker, ENV_A, frozenset()
    )
    node_on = marker_diff.evaluate_marker(
        marker_diff.Requirement('bar; extra == "fast"').marker, ENV_A, frozenset({"fast"})
    )
    assert node_off.result == Tri.FALSE
    assert node_on.result == Tri.TRUE
    assert "fast" in node_on.note


def test_extra_diff_when_marker_combines_extra_and_version():
    reqs = 'baz; python_version < "3.11" or extra == "fast"\n'
    report = marker_diff.diff_environments(reqs, ENV_A, ENV_B, extras_sets=[frozenset({"fast"})])
    # with extra "fast" the marker stays TRUE in both envs -> no change
    assert report.changes == []
    report = marker_diff.diff_environments(reqs, ENV_A, ENV_B, extras_sets=[frozenset()])
    # without the extra, only the python_version disjunct matters -> disabled
    assert [c.kind for c in report.changes] == ["disabled"]


def test_missing_environment_field_is_unknown_not_false():
    env_missing = {k: v for k, v in ENV_B.items() if k != "platform_machine"}
    reqs = 'thing; platform_machine == "x86_64"\n'
    report = marker_diff.diff_environments(reqs, ENV_A, env_missing)
    assert report.changes[0].kind == "became-unknown"
    assert report.changes[0].after.result == Tri.UNKNOWN
    assert report.changes[0].after.result != Tri.FALSE  # never silently false
    assert "platform_machine" in report.unknown_fields_b
    text = marker_diff.render_report(report)
    assert "UNKNOWN" in text
    assert "platform_machine" in text


def test_and_or_explanation_tree_keeps_condition_path():
    marker = marker_diff.Marker(
        'python_version < "3.11" and os_name == "posix" or extra == "fast"'
    )
    node = marker_diff.evaluate_marker(marker, ENV_B, frozenset())
    # top-level or, left side is the and-group (and binds tighter)
    assert isinstance(node, Bool) and node.op == "or"
    assert isinstance(node.children[0], Bool) and node.children[0].op == "and"
    assert node.children[0].result == Tri.FALSE  # 3.12 < 3.11 is false
    assert node.result == Tri.FALSE  # no "fast" extra either
    rendered = "\n".join(marker_diff.render_node(node))
    assert "or => FALSE" in rendered
    assert "and => FALSE" in rendered
    assert 'python_version < "3.11"' in rendered
    assert 'os_name == "posix"' in rendered


def test_unknown_propagates_through_and_or():
    env_missing = {k: v for k, v in ENV_A.items() if k != "os_name"}
    # unknown and true -> unknown
    node = marker_diff.evaluate_marker(
        marker_diff.Marker('os_name == "posix" and python_version < "3.11"'),
        env_missing,
        frozenset(),
    )
    assert node.result == Tri.UNKNOWN
    # false and unknown -> false (false dominates)
    node = marker_diff.evaluate_marker(
        marker_diff.Marker('python_version > "3.11" and os_name == "posix"'),
        env_missing,
        frozenset(),
    )
    assert node.result == Tri.FALSE
    # true or unknown -> true
    node = marker_diff.evaluate_marker(
        marker_diff.Marker('python_version < "3.11" or os_name == "posix"'),
        env_missing,
        frozenset(),
    )
    assert node.result == Tri.TRUE


def test_conflicting_specifiers_keep_sources():
    reqs = "qux==1.4\nqux>=2.0\n"
    report = marker_diff.diff_environments(reqs, ENV_A, ENV_B)
    assert "qux" in report.conflicts
    problems = report.conflicts["qux"]
    assert any("line 1" in p and "line 2" in p for p in problems)
    text = marker_diff.render_report(report)
    assert "NOT installable" in text
    assert "qux==1.4" in text and "qux>=2.0" in text


def test_empty_version_range_conflict():
    occs, errors = marker_diff.parse_requirements_text("pkg>=3\npkg<2\n")
    assert errors == []
    problems = marker_diff.find_specifier_conflicts(occs)
    assert any("empty version range" in p for p in problems)


def test_duplicates_aggregated_by_canonical_name():
    occs, _ = marker_diff.parse_requirements_text("Foo_Bar>=1\nfoo-bar<3\n")
    groups = marker_diff.aggregate_by_name(occs)
    assert list(groups) == ["foo-bar"]
    assert len(groups["foo-bar"]) == 2
    # compatible specifiers -> no conflict reported
    assert marker_diff.find_specifier_conflicts(occs) == []


def test_extra_in_environment_is_ignored_with_warning():
    env = dict(ENV_A, extra="fast")
    report = marker_diff.diff_environments('bar; extra == "fast"\n', env, ENV_B)
    assert any("'extra' in environment is ignored" in w for w in report.warnings)
    # extra came from the caller set (empty), so the marker is FALSE in both
    assert report.changes == []


def test_cli_demo():
    import io
    import json
    import tempfile
    from contextlib import redirect_stdout

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        reqs = tmp_path / "reqs.txt"
        reqs.write_text('foo; python_version < "3.11"\n', encoding="utf-8")
        env_a = tmp_path / "a.json"
        env_b = tmp_path / "b.json"
        env_a.write_text(json.dumps(ENV_A), encoding="utf-8")
        env_b.write_text(json.dumps(ENV_B), encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = marker_diff.main(
                [
                    str(reqs),
                    "--env-a",
                    str(env_a),
                    "--env-b",
                    str(env_b),
                    "--name-a",
                    "py310",
                    "--name-b",
                    "py312",
                ]
            )
    assert rc == 0
    out = buf.getvalue()
    assert "[DISABLED] foo" in out
    assert "py310" in out and "py312" in out


def test_no_marker_requirement_never_changes():
    report = marker_diff.diff_environments("plainpkg>=1\n", ENV_A, ENV_B)
    assert report.changes == []
    assert report.total_requirements == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
