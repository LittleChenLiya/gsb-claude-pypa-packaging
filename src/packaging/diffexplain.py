"""Offline dependency-diff explainer, built on top of ``packaging``.

Given two explicit target environments (JSON mappings) and one shared
requirements list, report which requirements become enabled / disabled /
unknown because of their markers.

The explainer is fully offline: it never queries PyPI and never resolves
dependency versions.  Instead it walks the marker AST (see
:mod:`packaging.markers`) and evaluates every atom with three-valued logic
(*True* / *False* / *unknown*).  A marker that references an environment
field which is missing from the supplied environment is reported as
``unknown`` -- it is **not** silently treated as an empty string and forced
to ``False``.

Extras are evaluated per caller set: each requirement is evaluated once for
the base environment (``extra == ""``) and once for every extra the caller
asked for.  A requirement is enabled if *any* of those evaluations is true.

Duplicate requirements are aggregated by canonical name, but conflicting
specifiers are kept with their sources and flagged as conflicts -- the
aggregate is never falsely declared installable.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

from packaging._parser import Variable
from packaging.markers import (
    UndefinedComparison,
    _eval_op,
    _normalize,
)
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

__all__ = [
    "AtomEval",
    "ConjunctionResult",
    "GroupResult",
    "RequirementDiff",
    "DiffResult",
    "DiffError",
    "load_environment",
    "load_requirements",
    "evaluate_requirement",
    "diff_environments",
    "format_diff",
    "main",
]


class DiffError(ValueError):
    """Raised when the input (environment / requirements) is invalid."""


# A unique sentinel used internally to detect *missing* environment fields,
# so that a missing field is never confused with an empty string.
_MISSING = object()


# ---------------------------------------------------------------------------
# Three-valued marker evaluation with AST path tracking
# ---------------------------------------------------------------------------


@dataclass
class AtomEval:
    """The result of evaluating one marker atom against one environment.

    :param text: the serialized atom, e.g. ``'python_version < "3.11"'``
    :param result: ``True`` / ``False`` / ``None`` (unknown)
    :param missing: the environment variable name that was missing, when
        :attr:`result` is ``None`` because of a missing field
    """

    text: str
    result: bool | None
    missing: str | None = None


@dataclass
class ConjunctionResult:
    """A conjunction (``and``) of atoms and nested marker groups."""

    items: list[AtomEval | GroupResult]
    result: bool | None


@dataclass
class GroupResult:
    """A parenthesized marker group: a disjunction of conjunctions."""

    conjunctions: list[ConjunctionResult]
    result: bool | None


def _and3(a: bool | None, b: bool | None) -> bool | None:
    """Three-valued conjunction (``and``)."""
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def _or3(a: bool | None, b: bool | None) -> bool | None:
    """Three-valued disjunction (``or``)."""
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def _eval_atom(atom: tuple, env: dict[str, Any]) -> AtomEval:
    """Evaluate a single marker atom with three-valued logic.

    A missing environment field yields an unknown result (with
    :attr:`AtomEval.missing` set) rather than a silent ``False``.
    """
    lhs, op, rhs = atom
    if isinstance(lhs, Variable):
        env_key = lhs.value
        lhs_value = env.get(env_key, _MISSING)
        rhs_value = rhs.value
    else:
        lhs_value = lhs.value
        env_key = rhs.value
        rhs_value = env.get(env_key, _MISSING)

    text = f"{lhs.serialize()} {op.serialize()} {rhs.serialize()}"

    if lhs_value is _MISSING or rhs_value is _MISSING:
        return AtomEval(text, None, missing=env_key)

    if not isinstance(lhs_value, str):
        # A set-valued marker (e.g. ``extras``) on the left-hand side is
        # invalid; report it as unknown rather than crashing.
        return AtomEval(text, None, missing=env_key)

    try:
        lhs_norm, rhs_norm = _normalize(lhs_value, rhs_value, key=env_key)
        result = _eval_op(lhs_norm, op, rhs_norm, key=env_key)
    except (UndefinedComparison, InvalidSpecifier):
        return AtomEval(text, None, missing=env_key)

    return AtomEval(text, result)


def _make_conjunction(
    items: list[AtomEval | GroupResult],
) -> ConjunctionResult:
    result: bool | None = True
    for item in items:
        result = _and3(result, item.result)
    return ConjunctionResult(items=items, result=result)


def _make_group(conjunctions: list[ConjunctionResult]) -> GroupResult:
    result: bool | None = False
    for conj in conjunctions:
        result = _or3(result, conj.result)
    return GroupResult(conjunctions=conjunctions, result=result)


def eval_group(markers: list, env: dict[str, Any]) -> GroupResult:
    """Evaluate a marker AST into a :class:`GroupResult` tree.

    The tree mirrors the marker AST: a group is a disjunction of
    conjunctions, each of which contains atoms and nested groups.  The
    conditional path that determines the result is preserved as the tree
    structure.
    """
    conjunctions: list[ConjunctionResult] = []
    current_items: list[AtomEval | GroupResult] = []

    for item in markers:
        if isinstance(item, list):
            current_items.append(eval_group(item, env))
        elif isinstance(item, tuple):
            current_items.append(_eval_atom(item, env))
        elif item == "and":
            pass
        elif item == "or":
            conjunctions.append(_make_conjunction(current_items))
            current_items = []
        else:  # pragma: no cover
            raise TypeError(f"Unexpected marker {item!r}")

    conjunctions.append(_make_conjunction(current_items))
    return _make_group(conjunctions)


def collect_atoms(group: GroupResult) -> list[AtomEval]:
    """Collect all atoms from a marker group, in order."""
    atoms: list[AtomEval] = []
    for conj in group.conjunctions:
        for item in conj.items:
            if isinstance(item, AtomEval):
                atoms.append(item)
            else:
                atoms.extend(collect_atoms(item))
    return atoms


# ---------------------------------------------------------------------------
# Rendering of evaluated marker trees
# ---------------------------------------------------------------------------


def _render_atom(atom: AtomEval) -> str:
    if atom.result is True:
        val = "True"
    elif atom.result is False:
        val = "False"
    elif atom.missing:
        val = f"unknown (missing: {atom.missing})"
    else:
        val = "unknown"
    return f"{atom.text}={val}"


def _render_conjunction(conj: ConjunctionResult, parenthesize: bool) -> str:
    parts: list[str] = []
    for item in conj.items:
        if isinstance(item, AtomEval):
            parts.append(_render_atom(item))
        elif len(item.conjunctions) == 1:
            parts.append(_render_conjunction(item.conjunctions[0], parenthesize=False))
        else:
            parts.append("(" + _render_group(item) + ")")
    inner = " and ".join(parts)
    if parenthesize:
        return "(" + inner + ")"
    return inner


def _render_group(group: GroupResult) -> str:
    if len(group.conjunctions) == 1:
        return _render_conjunction(group.conjunctions[0], parenthesize=False)
    parts = [
        _render_conjunction(conj, parenthesize=True)
        for conj in group.conjunctions
    ]
    return " or ".join(parts)


def render_marker(group: GroupResult | None) -> str:
    """Render an evaluated marker group as a human-readable string."""
    if group is None:
        return "(none)"
    return _render_group(group)


# ---------------------------------------------------------------------------
# Per-requirement evaluation (extras per caller set)
# ---------------------------------------------------------------------------


def _extras_from_env(env: dict[str, Any]) -> list[str]:
    """Extract the caller's extras from an environment mapping.

    Accepts ``extras`` (a list/set) or, as a convenience, a single ``extra``
    string.  Names are canonicalized (PEP 685).
    """
    raw = env.get("extras")
    if raw is None:
        single = env.get("extra")
        if single is None:
            return []
        raw = [single]
    if isinstance(raw, str):
        raw = [raw]
    return [canonicalize_name(e) for e in raw]


@dataclass
class RequirementDiff:
    """The diff of one requirement between two environments."""

    source: str
    name: str
    name_display: str
    specifier: str
    marker_text: str | None
    extras_a: list[str]
    extras_b: list[str]
    # Per-extra evaluated marker trees.  Key "" is the base evaluation.
    # Value is None when the requirement has no marker (always enabled).
    per_extra_a: dict[str, GroupResult | None]
    per_extra_b: dict[str, GroupResult | None]
    enabled_a: bool | None
    enabled_b: bool | None

    def mentions_extra(self) -> bool:
        """Whether the marker references the ``extra`` variable."""
        if self.marker_text is None:
            return False
        return "extra" in self.marker_text


def _eval_for_env(
    req: Requirement, env: dict[str, Any], extras: list[str]
) -> dict[str, GroupResult | None]:
    """Evaluate a requirement's marker for the base env and each extra."""
    marker = req.marker
    if marker is None:
        return {"": None}

    results: dict[str, GroupResult | None] = {}
    base_env = {**env, "extra": ""}
    results[""] = eval_group(marker._markers, base_env)
    for extra in extras:
        extra_env = {**env, "extra": extra}
        results[extra] = eval_group(marker._markers, extra_env)
    return results


def _overall(per_extra: dict[str, GroupResult | None]) -> bool | None:
    vals = [g.result if g is not None else True for g in per_extra.values()]
    if any(v is True for v in vals):
        return True
    if any(v is None for v in vals):
        return None
    return False


def evaluate_requirement(
    source: str, env_a: dict[str, Any], env_b: dict[str, Any]
) -> RequirementDiff:
    """Evaluate one requirement against two environments."""
    try:
        req = Requirement(source)
    except InvalidRequirement as e:
        raise DiffError(f"Invalid requirement {source!r}: {e}") from e

    extras_a = _extras_from_env(env_a)
    extras_b = _extras_from_env(env_b)

    per_extra_a = _eval_for_env(req, env_a, extras_a)
    per_extra_b = _eval_for_env(req, env_b, extras_b)

    return RequirementDiff(
        source=source,
        name=canonicalize_name(req.name),
        name_display=req.name,
        specifier=str(req.specifier),
        marker_text=str(req.marker) if req.marker else None,
        extras_a=extras_a,
        extras_b=extras_b,
        per_extra_a=per_extra_a,
        per_extra_b=per_extra_b,
        enabled_a=_overall(per_extra_a),
        enabled_b=_overall(per_extra_b),
    )


# ---------------------------------------------------------------------------
# Duplicate aggregation and conflict detection
# ---------------------------------------------------------------------------


def _prefix_upper(v: Version) -> Version:
    """Upper bound for a ``==V.*`` prefix match (exclusive)."""
    release = list(v.release)
    if not release:
        return v
    new_release = release[:-1] + [release[-1] + 1]
    return Version(".".join(str(x) for x in new_release))


def _compatible_bounds(v: Version) -> tuple[Version, Version]:
    """Bounds for a ``~=V`` compatible-release specifier.

    Returns ``(lower_closed, upper_open)``.
    """
    release = list(v.release)
    lower = v
    if len(release) <= 1:
        return lower, v
    new_release = release[:-2] + [release[-2] + 1, 0]
    return lower, Version(".".join(str(x) for x in new_release))


def _interval_covered(
    lo_ver: Version | None,
    hi_ver: Version | None,
    lo_closed: bool,
    hi_closed: bool,
    elo: Version,
    ehi: Version,
) -> bool:
    """Whether the feasible interval is fully covered by ``[elo, ehi)``."""
    if lo_ver is not None and lo_ver < elo:
        return False
    if hi_ver is None:
        return False
    if hi_ver < ehi:
        return True
    if hi_ver == ehi and not hi_closed:
        return True
    return False


def specifier_set_satisfiable(specs: SpecifierSet) -> bool | None:
    """Lightweight satisfiability check for a :class:`SpecifierSet`.

    Returns ``True`` if some version satisfies the set, ``False`` if no
    version can, or ``None`` if it cannot be determined (exotic specifiers).

    This is static interval analysis, *not* dependency resolution: it never
    queries PyPI and never consults a version index.
    """
    lowers: list[tuple[Version, bool]] = []
    uppers: list[tuple[Version, bool]] = []
    excluded_points: set[Version] = set()
    excluded_ranges: list[tuple[Version, Version]] = []
    saw_any = False

    for spec in specs:
        saw_any = True
        op = spec.operator
        raw = spec.version
        try:
            if raw.endswith(".*"):
                ver = Version(raw[:-2])
            else:
                ver = Version(raw)
        except InvalidVersion:
            return None

        if op == "==":
            if raw.endswith(".*"):
                lowers.append((ver, True))
                uppers.append((_prefix_upper(ver), False))
            else:
                lowers.append((ver, True))
                uppers.append((ver, True))
        elif op == "!=":
            if raw.endswith(".*"):
                excluded_ranges.append((ver, _prefix_upper(ver)))
            else:
                excluded_points.add(ver)
        elif op == ">=":
            lowers.append((ver, True))
        elif op == "<=":
            uppers.append((ver, True))
        elif op == ">":
            lowers.append((ver, False))
        elif op == "<":
            uppers.append((ver, False))
        elif op == "~=":
            lo, hi = _compatible_bounds(ver)
            lowers.append((lo, True))
            uppers.append((hi, False))
        else:
            return None

    if not saw_any:
        return True

    if lowers:
        lo_ver = max(v for v, _ in lowers)
        lo_closed = all(c for v, c in lowers if v == lo_ver)
    else:
        lo_ver, lo_closed = None, True
    if uppers:
        hi_ver = min(v for v, _ in uppers)
        hi_closed = all(c for v, c in uppers if v == hi_ver)
    else:
        hi_ver, hi_closed = None, False

    if lo_ver is not None and hi_ver is not None:
        if lo_ver > hi_ver:
            return False
        if lo_ver == hi_ver:
            if not (lo_closed and hi_closed):
                return False
            if lo_ver in excluded_points:
                return False
            for elo, ehi in excluded_ranges:
                if elo <= lo_ver < ehi:
                    return False
            return True

    for elo, ehi in excluded_ranges:
        if _interval_covered(
            lo_ver, hi_ver, lo_closed, hi_closed, elo, ehi
        ):
            return False
    return True


@dataclass
class ConflictInfo:
    """A pair of requirements with mutually exclusive specifiers."""

    index_a: int
    index_b: int
    spec_a: str
    spec_b: str


def detect_conflicts(items: list[RequirementDiff]) -> list[ConflictInfo]:
    """Detect pairwise specifier conflicts within a duplicate group."""
    conflicts: list[ConflictInfo] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            si = SpecifierSet(items[i].specifier)
            sj = SpecifierSet(items[j].specifier)
            combined = si & sj
            result = specifier_set_satisfiable(combined)
            if result is False:
                conflicts.append(
                    ConflictInfo(
                        index_a=i,
                        index_b=j,
                        spec_a=items[i].specifier or "(any)",
                        spec_b=items[j].specifier or "(any)",
                    )
                )
    return conflicts


# ---------------------------------------------------------------------------
# Diff orchestration
# ---------------------------------------------------------------------------


@dataclass
class DiffResult:
    """The full diff between two environments for one requirements list."""

    env_a_path: str
    env_b_path: str
    requirements_path: str
    env_a: dict[str, Any]
    env_b: dict[str, Any]
    items: list[RequirementDiff]
    groups: dict[str, list[RequirementDiff]]
    conflicts: dict[str, list[ConflictInfo]]


def load_environment(path: str) -> dict[str, Any]:
    """Load a target environment from a JSON file."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise DiffError(f"Environment file not found: {path}") from e
    except json.JSONDecodeError as e:
        raise DiffError(f"Invalid JSON in environment file {path}: {e}") from e
    if not isinstance(data, dict):
        raise DiffError(f"Environment file {path} must contain a JSON object")
    return data


def load_requirements(path: str) -> list[str]:
    """Load a requirements list from a text file (one requirement per line)."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError as e:
        raise DiffError(f"Requirements file not found: {path}") from e
    sources: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sources.append(line)
    return sources


def diff_environments(
    env_a: dict[str, Any],
    env_b: dict[str, Any],
    sources: list[str],
    *,
    env_a_path: str = "<env-a>",
    env_b_path: str = "<env-b>",
    requirements_path: str = "<requirements>",
) -> DiffResult:
    """Diff two environments for the given requirements list."""
    items = [evaluate_requirement(s, env_a, env_b) for s in sources]

    groups: dict[str, list[RequirementDiff]] = {}
    for item in items:
        groups.setdefault(item.name, []).append(item)

    conflicts: dict[str, list[ConflictInfo]] = {}
    for name, group_items in groups.items():
        if len(group_items) > 1:
            conflicts[name] = detect_conflicts(group_items)

    return DiffResult(
        env_a_path=env_a_path,
        env_b_path=env_b_path,
        requirements_path=requirements_path,
        env_a=env_a,
        env_b=env_b,
        items=items,
        groups=groups,
        conflicts=conflicts,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _status_str(result: bool | None) -> str:
    if result is True:
        return "enabled"
    if result is False:
        return "disabled"
    return "unknown"


def _change_str(a: bool | None, b: bool | None) -> str:
    if a is True and b is False:
        return "enabled->disabled"
    if a is False and b is True:
        return "disabled->enabled"
    if a is True and b is None:
        return "enabled->unknown"
    if a is None and b is True:
        return "unknown->enabled"
    if a is False and b is None:
        return "disabled->unknown"
    if a is None and b is False:
        return "unknown->disabled"
    if a is None and b is None:
        return "unchanged (unknown)"
    return "unchanged"


def _delta_atoms(
    group_a: GroupResult | None, group_b: GroupResult | None
) -> list[tuple[AtomEval, AtomEval]]:
    """Pair up atoms that differ between two evaluated marker groups."""
    if group_a is None or group_b is None:
        return []
    atoms_a = {a.text: a for a in collect_atoms(group_a)}
    atoms_b = {b.text: b for b in collect_atoms(group_b)}
    delta: list[tuple[AtomEval, AtomEval]] = []
    for text, aa in atoms_a.items():
        bb = atoms_b.get(text)
        if bb is not None and aa.result != bb.result:
            delta.append((aa, bb))
    return delta


def _format_item_detail(item: RequirementDiff) -> list[str]:
    lines: list[str] = []
    change = _change_str(item.enabled_a, item.enabled_b)
    lines.append(f"  {item.name_display}  [{change}]")
    lines.append(f"    source:   {item.source}")
    if item.marker_text is not None:
        lines.append(f"    marker:   {item.marker_text}")
    else:
        lines.append("    marker:   (none -- always enabled)")

    base_a = item.per_extra_a.get("")
    base_b = item.per_extra_b.get("")
    if item.marker_text is not None:
        lines.append(f"    A:        {render_marker(base_a)}")
        lines.append(f"    B:        {render_marker(base_b)}")

    if item.mentions_extra() and (item.extras_a or item.extras_b):
        lines.append("    extras:")
        all_extras = ["base"] + sorted(set(item.extras_a) | set(item.extras_b))
        header = "".ljust(10) + "".join(e.ljust(14) for e in all_extras)
        lines.append(header)
        for label, per_extra, extras in (
            ("A:", item.per_extra_a, item.extras_a),
            ("B:", item.per_extra_b, item.extras_b),
        ):
            row = ("      " + label).ljust(10)
            for e in all_extras:
                if e == "base":
                    g = per_extra.get("")
                    row += _status_str(g.result if g else True).ljust(14)
                elif e in extras:
                    g = per_extra.get(e)
                    row += _status_str(g.result if g else True).ljust(14)
                else:
                    row += "(n/a)".ljust(14)
            lines.append(row)

    delta = _delta_atoms(base_a, base_b)
    if delta:
        lines.append("    delta (atoms that changed):")
        for aa, bb in delta:
            lines.append(
                f"      {aa.text}  (A={_status_str(aa.result)}, "
                f"B={_status_str(bb.result)})"
            )
    return lines


def format_diff(diff: DiffResult) -> str:
    """Format a :class:`DiffResult` as human-readable text."""
    lines: list[str] = []
    lines.append("Offline dependency diff explainer")
    lines.append("=" * 34)
    lines.append(f"  env A: {diff.env_a_path}")
    lines.append(f"  env B: {diff.env_b_path}")
    lines.append(f"  requirements: {diff.requirements_path}")

    extras_a = _extras_from_env(diff.env_a)
    extras_b = _extras_from_env(diff.env_b)
    lines.append(
        f"  extras (A): {', '.join(extras_a) if extras_a else '(none)'}"
    )
    lines.append(
        f"  extras (B): {', '.join(extras_b) if extras_b else '(none)'}"
    )
    lines.append("")

    # Summary table
    lines.append("Summary (by canonical name)")
    lines.append("-" * 80)
    header = (
        f"  {'name':<16}{'specifier':<14}{'marker':<42}"
        f"{'A':<10}{'B':<10}{'change'}"
    )
    lines.append(header)
    for item in diff.items:
        marker = item.marker_text or "(none)"
        if len(marker) > 40:
            marker = marker[:37] + "..."
        lines.append(
            f"  {item.name:<16}{(item.specifier or '(any)'):<14}"
            f"{marker:<42}{_status_str(item.enabled_a):<10}"
            f"{_status_str(item.enabled_b):<10}"
            f"{_change_str(item.enabled_a, item.enabled_b)}"
        )
    lines.append("")

    # Details for changed / unknown items
    changed = [
        item
        for item in diff.items
        if _change_str(item.enabled_a, item.enabled_b) != "unchanged"
        or item.enabled_a is None
        or item.enabled_b is None
    ]
    if changed:
        lines.append("Changed / unknown details")
        lines.append("-" * 80)
        for item in changed:
            lines.extend(_format_item_detail(item))
            lines.append("")

    # Duplicate groups
    dup_groups = {
        name: items
        for name, items in diff.groups.items()
        if len(items) > 1
    }
    if dup_groups:
        lines.append("Duplicate groups")
        lines.append("-" * 80)
        for name, group_items in dup_groups.items():
            lines.append(f"  {name}  ({len(group_items)} sources)")
            for item in group_items:
                lines.append(
                    f"    - {item.source}  "
                    f"[A: {_status_str(item.enabled_a)}, "
                    f"B: {_status_str(item.enabled_b)}]"
                )
            conflicts = diff.conflicts.get(name, [])
            if conflicts:
                for c in conflicts:
                    lines.append(
                        f"    CONFLICT: specifiers {c.spec_a!r} and "
                        f"{c.spec_b!r} are mutually exclusive; "
                        f"cannot be aggregated as installable"
                    )
            else:
                lines.append(
                    "    (specifiers are compatible; sources preserved above)"
                )
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _diff_to_json(diff: DiffResult) -> dict[str, Any]:
    """Serialize a :class:`DiffResult` to JSON-safe data."""
    return {
        "env_a": diff.env_a,
        "env_b": diff.env_b,
        "requirements": [
            {
                "source": item.source,
                "name": item.name,
                "specifier": item.specifier,
                "marker": item.marker_text,
                "enabled_a": _status_str(item.enabled_a),
                "enabled_b": _status_str(item.enabled_b),
                "change": _change_str(item.enabled_a, item.enabled_b),
            }
            for item in diff.items
        ],
        "conflicts": {
            name: [
                {"spec_a": c.spec_a, "spec_b": c.spec_b} for c in conflicts
            ]
            for name, conflicts in diff.conflicts.items()
            if conflicts
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="packaging.diffexplain",
        description=(
            "Offline dependency-diff explainer: show which requirements "
            "become enabled / disabled / unknown between two target "
            "environments, based on their markers."
        ),
    )
    parser.add_argument("env_a", help="path to environment A JSON file")
    parser.add_argument("env_b", help="path to environment B JSON file")
    parser.add_argument(
        "requirements",
        help="path to requirements.txt (one requirement per line)",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit machine-readable JSON instead of human-readable text",
    )
    args = parser.parse_args(argv)

    try:
        env_a = load_environment(args.env_a)
        env_b = load_environment(args.env_b)
        sources = load_requirements(args.requirements)
        diff = diff_environments(
            env_a,
            env_b,
            sources,
            env_a_path=args.env_a,
            env_b_path=args.env_b,
            requirements_path=args.requirements,
        )
    except DiffError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.as_json:
        print(json.dumps(_diff_to_json(diff), indent=2, default=str))
    else:
        print(format_diff(diff))
    return 0


if __name__ == "__main__":
    sys.exit(main())
