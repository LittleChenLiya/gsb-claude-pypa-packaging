"""Offline dependency marker diff explainer, built on top of packaging.

Given *one* requirements list and *two* explicit target environments, this
tool explains which dependencies become enabled or disabled because of
environment markers, and *why* -- keeping the marker AST condition path that
caused each change.

Design rules (offline, no PyPI, no version resolution):

* The marker AST (Marker._markers) is evaluated by hand so the boolean
  and/or path and every comparison's resolved value are preserved in an
  explanation tree.
* 'extra' is never read from the environment.  It is evaluated separately
  for each caller-provided extras set (--extras fast,cli).
* A marker variable missing from a provided environment is reported as
  UNKNOWN (tri-state logic) -- it is never silently coerced to ""/false.
* Duplicate dependencies are aggregated by canonical name, but conflicting
  specifiers keep their source lines; aggregation never claims the combined
  requirement is installable.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, Specifier
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

# Marker variables that a fully specified target environment should provide
# (mirrors packaging.markers.default_environment, minus "extra").
KNOWN_ENVIRONMENT_KEYS = frozenset(
    {
        "implementation_name",
        "implementation_version",
        "os_name",
        "platform_machine",
        "platform_release",
        "platform_system",
        "platform_version",
        "python_full_version",
        "platform_python_implementation",
        "python_version",
        "sys_platform",
    }
)


# ---------------------------------------------------------------------------
# Tri-state truth values: missing environment data is UNKNOWN, never false.
# ---------------------------------------------------------------------------
class Tri(Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


def tri_and(values: list) -> Tri:
    if Tri.FALSE in values:
        return Tri.FALSE
    if Tri.UNKNOWN in values:
        return Tri.UNKNOWN
    return Tri.TRUE


def tri_or(values: list) -> Tri:
    if Tri.TRUE in values:
        return Tri.TRUE
    if Tri.UNKNOWN in values:
        return Tri.UNKNOWN
    return Tri.FALSE


# ---------------------------------------------------------------------------
# Explanation tree: preserves the marker AST condition path.
# ---------------------------------------------------------------------------
@dataclass
class Leaf:
    """One comparison, e.g. python_version < "3.11"."""

    text: str
    result: Tri
    env_value: str | None = None  # resolved environment value (None if unknown)
    note: str = ""  # extra context: matched extra, unknown-field reason, ...


@dataclass
class Bool:
    """An and/or node with its evaluated children."""

    op: str  # "and" | "or"
    children: list  # list of Leaf | Bool
    result: Tri


def _compare(lhs: str, op: str, rhs: str) -> bool:
    """Mirror packaging.markers comparison semantics, fully offline."""
    try:
        spec = Specifier(f"{op}{rhs}")
    except InvalidSpecifier:
        pass
    else:
        return spec.contains(lhs, prereleases=True)
    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    if op == "in":
        return lhs in rhs
    if op == "not in":
        return lhs not in rhs
    raise ValueError(f"unsupported marker operator: {op!r}")


def _split(items: list, op: str) -> list:
    parts: list = [[]]
    for item in items:
        if isinstance(item, str) and item == op:
            parts.append([])
        else:
            parts[-1].append(item)
    return parts


def evaluate_marker(marker, env: dict, extras: frozenset):
    """Evaluate marker against env and one caller extras set.

    Returns an explanation tree; unknown environment fields yield
    Tri.UNKNOWN leaves instead of being silently treated as false.
    """
    if marker is None:
        return Leaf(text="<no marker>", result=Tri.TRUE, note="always enabled")
    return _eval_group(marker._markers, env, extras)  # noqa: SLF001 - AST access is the point


def _eval_group(items: list, env: dict, extras: frozenset):
    or_parts = _split(items, "or")
    if len(or_parts) > 1:
        children = [_eval_and(part, env, extras) for part in or_parts]
        return Bool("or", children, tri_or([c.result for c in children]))
    return _eval_and(items, env, extras)


def _eval_and(items: list, env: dict, extras: frozenset):
    and_parts = _split(items, "and")
    if len(and_parts) > 1:
        children = [_eval_operand(part, env, extras) for part in and_parts]
        return Bool("and", children, tri_and([c.result for c in children]))
    return _eval_operand(and_parts[0], env, extras)


def _eval_operand(operand: list, env: dict, extras: frozenset):
    item = operand[0]
    if isinstance(item, list):  # parenthesised sub-expression
        return _eval_group(item, env, extras)
    return _eval_comparison(item, env, extras)


def _eval_comparison(node: tuple, env: dict, extras: frozenset) -> Leaf:
    lhs, op, rhs = node
    op_s = op.serialize()
    text = f"{lhs.serialize()} {op_s} {rhs.serialize()}"

    if lhs.__class__.__name__ == "Variable":
        env_key = lhs.value
        var_on_left = True
    else:
        env_key = rhs.value
        var_on_left = False

    def compare_with(env_value: str) -> bool:
        if var_on_left:
            return _compare(env_value, op_s, rhs.value)
        return _compare(lhs.value, op_s, env_value)

    if env_key == "extra":
        # Extras come from the caller's set, never from the environment.
        candidates = sorted(extras) if extras else [""]
        matched = [e for e in candidates if compare_with(e)]
        result = Tri.TRUE if matched else Tri.FALSE
        shown = "{" + ",".join(candidates) + "}" if extras else "{}"
        note = "extras=" + shown + "; matched: " + (", ".join(matched) or "(none)")
        return Leaf(text=text, result=result, env_value=shown, note=note)

    if env_key not in env:
        return Leaf(
            text=text,
            result=Tri.UNKNOWN,
            env_value=None,
            note=(
                f"environment field {env_key!r} not provided -- "
                "reported UNKNOWN, not silently treated as false"
            ),
        )

    env_value = env[env_key]
    result = Tri.TRUE if compare_with(env_value) else Tri.FALSE
    return Leaf(text=text, result=result, env_value=env_value)


def render_node(node, indent: int = 0) -> list:
    pad = "  " * indent
    if isinstance(node, Leaf):
        value = f'"{node.env_value}"' if node.env_value is not None else "<missing>"
        line = f"{pad}- {node.text}  [value: {value}] => {node.result.value}"
        if node.note:
            line += f"  ({node.note})"
        return [line]
    lines = [f"{pad}{node.op} => {node.result.value}"]
    for child in node.children:
        lines.extend(render_node(child, indent + 1))
    return lines


def changed_leaves(before, after) -> list:
    """Parallel walk of two explanation trees for the *same* marker AST."""
    out: list = []

    def walk(a, b) -> None:
        if isinstance(a, Leaf) and isinstance(b, Leaf):
            if a.env_value != b.env_value or a.result != b.result:
                out.append((a, b))
        elif isinstance(a, Bool) and isinstance(b, Bool):
            for ca, cb in zip(a.children, b.children):
                walk(ca, cb)

    walk(before, after)
    return out


# ---------------------------------------------------------------------------
# Requirements parsing, aggregation and specifier conflict detection.
# ---------------------------------------------------------------------------
@dataclass
class ReqOccurrence:
    line_no: int
    raw: str
    requirement: Requirement


def parse_requirements_text(text: str) -> tuple:
    occurrences: list = []
    errors: list = []
    for line_no, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            req = Requirement(stripped)
        except InvalidRequirement:
            if " #" in stripped:  # tolerate requirements.txt inline comments
                try:
                    req = Requirement(stripped.split(" #", 1)[0].strip())
                except InvalidRequirement as exc:
                    errors.append(f"line {line_no}: {exc}")
                    continue
            else:
                errors.append(f"line {line_no}: invalid requirement {stripped!r}")
                continue
        occurrences.append(ReqOccurrence(line_no, stripped, req))
    return occurrences, errors


def aggregate_by_name(occurrences: list) -> dict:
    groups: dict = {}
    for occ in occurrences:
        groups.setdefault(canonicalize_name(occ.requirement.name), []).append(occ)
    return groups


def _bump_last(release: tuple) -> tuple:
    return release[:-1] + (release[-1] + 1,)


def find_specifier_conflicts(occurrences: list) -> list:
    """Detect obviously unsatisfiable specifier combinations.

    Sources (requirement line numbers) are always kept in the messages so a
    conflict is never hidden by aggregation.
    """
    pins: list = []
    neqs: list = []
    lowers: list = []
    uppers: list = []
    problems: list = []

    def src(occ) -> str:
        return f"line {occ.line_no} ({occ.raw})"

    for occ in occurrences:
        for spec in occ.requirement.specifier:
            op, ver = spec.operator, spec.version
            try:
                if op == "==":
                    if ver.endswith(".*"):
                        base = Version(ver[:-2])
                        lowers.append((base, True, occ, str(spec)))
                        uppers.append((Version(_bump_last(base.release)), False, occ, str(spec)))
                    else:
                        pins.append((Version(ver), occ, str(spec)))
                elif op == "===":
                    pins.append((ver, occ, str(spec)))  # arbitrary string equality
                elif op == ">=":
                    lowers.append((Version(ver), True, occ, str(spec)))
                elif op == ">":
                    lowers.append((Version(ver), False, occ, str(spec)))
                elif op == "<=":
                    uppers.append((Version(ver), True, occ, str(spec)))
                elif op == "<":
                    uppers.append((Version(ver), False, occ, str(spec)))
                elif op == "~=":
                    base = Version(ver)
                    if len(base.release) >= 2:
                        upper = Version(_bump_last(base.release[:-1]))
                        lowers.append((base, True, occ, str(spec)))
                        uppers.append((upper, False, occ, str(spec)))
                elif op == "!=":
                    if not ver.endswith(".*"):
                        neqs.append((Version(ver), occ, str(spec)))
            except InvalidVersion:
                problems.append(
                    f"unparsable version in specifier {spec!s} from {src(occ)}; "
                    "cannot check it for conflicts"
                )

    for i, (v1, o1, s1) in enumerate(pins):
        for v2, o2, s2 in pins[i + 1 :]:
            if v1 != v2:
                problems.append(
                    f"conflicting pins {s1!r} [{src(o1)}] and {s2!r} [{src(o2)}]: "
                    "no single version can satisfy both"
                )
        for vn, on, sn in neqs:
            if v1 == vn:
                problems.append(f"pin {s1!r} [{src(o1)}] excluded by {sn!r} [{src(on)}]")
        for lv, inc, lo, sl in lowers:
            if v1 < lv or (v1 == lv and not inc):
                problems.append(
                    f"pin {s1!r} [{src(o1)}] violates lower bound {sl!r} [{src(lo)}]"
                )
        for uv, inc, uo, su in uppers:
            if v1 > uv or (v1 == uv and not inc):
                problems.append(
                    f"pin {s1!r} [{src(o1)}] violates upper bound {su!r} [{src(uo)}]"
                )

    for lv, linc, lo, sl in lowers:
        for uv, uinc, uo, su in uppers:
            if lv > uv or (lv == uv and not (linc and uinc)):
                problems.append(
                    f"empty version range: {sl!r} [{src(lo)}] vs {su!r} [{src(uo)}]"
                )
    return problems


# ---------------------------------------------------------------------------
# Diffing two environments.
# ---------------------------------------------------------------------------
@dataclass
class MarkerChange:
    occ: ReqOccurrence
    extras: frozenset
    before: object
    after: object
    kind: str  # "enabled" | "disabled" | "became-unknown" | "resolved-from-unknown"


def classify(before: Tri, after: Tri) -> str | None:
    if before == after:
        return None
    if before == Tri.TRUE and after == Tri.FALSE:
        return "disabled"
    if before == Tri.FALSE and after == Tri.TRUE:
        return "enabled"
    if after == Tri.UNKNOWN:
        return "became-unknown"
    if before == Tri.UNKNOWN:
        return "resolved-from-unknown"
    return "changed"


@dataclass
class DiffReport:
    env_a_name: str
    env_b_name: str
    changes: list = field(default_factory=list)
    unknown_fields_a: set = field(default_factory=set)
    unknown_fields_b: set = field(default_factory=set)
    conflicts: dict = field(default_factory=dict)
    parse_errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    total_requirements: int = 0


def _collect_unknowns(node, out: set) -> None:
    if isinstance(node, Leaf):
        if node.result == Tri.UNKNOWN and "environment field" in node.note:
            out.add(node.note.split("'")[1])
    else:
        for child in node.children:
            _collect_unknowns(child, out)


def diff_environments(
    requirements_text: str,
    env_a: dict,
    env_b: dict,
    extras_sets: list | None = None,
    env_a_name: str = "env-a",
    env_b_name: str = "env-b",
) -> DiffReport:
    report = DiffReport(env_a_name=env_a_name, env_b_name=env_b_name)
    if extras_sets is None:
        extras_sets = [frozenset()]

    for name, env in ((env_a_name, env_a), (env_b_name, env_b)):
        for key in sorted(env):
            if key == "extra":
                report.warnings.append(
                    f"{name}: 'extra' in environment is ignored; "
                    "extras are evaluated per caller --extras set"
                )
            elif key not in KNOWN_ENVIRONMENT_KEYS:
                report.warnings.append(f"{name}: unknown environment key {key!r} (typo?)")

    occurrences, errors = parse_requirements_text(requirements_text)
    report.parse_errors = errors
    report.total_requirements = len(occurrences)

    for occ in occurrences:
        for extras in extras_sets:
            before = evaluate_marker(occ.requirement.marker, env_a, extras)
            after = evaluate_marker(occ.requirement.marker, env_b, extras)
            _collect_unknowns(before, report.unknown_fields_a)
            _collect_unknowns(after, report.unknown_fields_b)
            kind = classify(before.result, after.result)
            if kind is not None:
                report.changes.append(MarkerChange(occ, extras, before, after, kind))

    for name, group in aggregate_by_name(occurrences).items():
        problems = find_specifier_conflicts(group)
        if problems:
            report.conflicts[name] = problems
    return report


# ---------------------------------------------------------------------------
# Human-readable rendering.
# ---------------------------------------------------------------------------
def _fmt_extras(extras: frozenset) -> str:
    return "{" + ", ".join(sorted(extras)) + "}" if extras else "{}"


def render_report(report: DiffReport) -> str:
    lines: list = []
    lines.append(f"Marker diff: {report.env_a_name} -> {report.env_b_name}")
    lines.append(f"requirements parsed: {report.total_requirements}")
    lines.append("")

    for warning in report.warnings:
        lines.append(f"WARNING: {warning}")
    for error in report.parse_errors:
        lines.append(f"PARSE ERROR: {error}")
    if report.warnings or report.parse_errors:
        lines.append("")

    lines.append(f"== Marker changes ({len(report.changes)}) ==")
    if not report.changes:
        lines.append("  (none)")
    for change in report.changes:
        req = change.occ.requirement
        lines.append(
            f"[{change.kind.upper()}] {req.name}"
            f"  (line {change.occ.line_no}, extras {_fmt_extras(change.extras)})"
        )
        lines.append(f"  marker: {change.occ.requirement.marker}")
        lines.append(f"  {report.env_a_name}:")
        lines.extend(render_node(change.before, 2))
        lines.append(f"  {report.env_b_name}:")
        lines.extend(render_node(change.after, 2))
        for leaf_a, leaf_b in changed_leaves(change.before, change.after):
            va = f'"{leaf_a.env_value}"' if leaf_a.env_value is not None else "<missing>"
            vb = f'"{leaf_b.env_value}"' if leaf_b.env_value is not None else "<missing>"
            lines.append(
                f"  changed condition: {leaf_a.text}  "
                f"value {va} -> {vb}  ({leaf_a.result.value} -> {leaf_b.result.value})"
            )
        lines.append("")

    lines.append("== Unknown environment fields ==")
    if not report.unknown_fields_a and not report.unknown_fields_b:
        lines.append("  (none)")
    for field_name in sorted(report.unknown_fields_a):
        lines.append(f"  {report.env_a_name}: {field_name!r} missing -> UNKNOWN")
    for field_name in sorted(report.unknown_fields_b):
        lines.append(f"  {report.env_b_name}: {field_name!r} missing -> UNKNOWN")
    lines.append("")

    lines.append("== Specifier conflicts (aggregated by canonical name) ==")
    if not report.conflicts:
        lines.append("  (none)")
    for name, problems in sorted(report.conflicts.items()):
        lines.append(f"  {name}: NOT installable as aggregated -- conflicts kept with sources:")
        for problem in problems:
            lines.append(f"    - {problem}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _load_env(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise SystemExit(f"{path}: environment JSON must be an object of string -> string")
    return data


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline dependency marker diff explainer (no PyPI, no resolution)."
    )
    parser.add_argument("requirements", help="path to the requirements file")
    parser.add_argument("--env-a", required=True, help="JSON file describing environment A")
    parser.add_argument("--env-b", required=True, help="JSON file describing environment B")
    parser.add_argument("--name-a", default="env-a", help="display name for environment A")
    parser.add_argument("--name-b", default="env-b", help="display name for environment B")
    parser.add_argument(
        "--extras",
        action="append",
        default=None,
        metavar="SET",
        help="caller extras set, comma-separated (repeatable); default: one empty set",
    )
    args = parser.parse_args(argv)

    if args.extras:
        extras_sets = [
            frozenset(e.strip() for e in s.split(",") if e.strip()) for s in args.extras
        ]
    else:
        extras_sets = [frozenset()]
    requirements_text = Path(args.requirements).read_text(encoding="utf-8")
    report = diff_environments(
        requirements_text,
        _load_env(args.env_a),
        _load_env(args.env_b),
        extras_sets=extras_sets,
        env_a_name=args.name_a,
        env_b_name=args.name_b,
    )
    print(render_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
