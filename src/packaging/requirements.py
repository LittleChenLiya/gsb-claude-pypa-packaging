# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.
from __future__ import annotations

from typing import TYPE_CHECKING

from ._parser import parse_requirement as _parse_requirement
from ._tokenizer import ParserSyntaxError
from .markers import Marker, _normalize_extra_values
from .specifiers import InvalidSpecifier, SpecifierSet
from .utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "ExtrasEditPlan",
    "InvalidRequirement",
    "Requirement",
]


def __dir__() -> list[str]:
    return __all__


class InvalidRequirement(ValueError):
    """
    An invalid requirement was found, users should refer to PEP 508.

    .. versionadded:: 16.1
    """


class Requirement:
    """Parse a requirement.

    Parse a given requirement string into its parts, such as name, specifier,
    URL, and extras. Raises InvalidRequirement on a badly-formed requirement
    string.

    .. versionadded:: 16.1

    .. versionchanged:: 22.0
        Added equality (``__eq__``) and hashing (``__hash__``) so requirements
        can be compared and stored in sets / dicts.

    .. versionchanged:: 23.2
        Equality and hashing began canonicalizing requirement names, so
        requirements whose names differ only by normalization (e.g.
        ``Requirement("Foo")`` vs ``Requirement("foo")``) now compare and hash
        equal.

    Instances are safe to serialize with :mod:`pickle`. They use a stable
    format so the same pickle can be loaded in future packaging releases.

    .. versionchanged:: 26.2

        Added a stable pickle format. Pickles created with packaging 26.2+ can
        be unpickled with future releases.  Backward compatibility with pickles
        from packaging < 26.2 is supported but may be removed in a future
        release.

    .. versionchanged:: 26.3

        The dedicated pickle support introduced in 26.2 did not preserve the
        specifier's explicit :attr:`~packaging.specifiers.SpecifierSet.prereleases`
        override; it is now included again.

        Equality and hashing normalize requirement names, extras, and
        equivalent specifiers. The string representation still preserves the
        parsed name and extras spelling.
    """

    # TODO: Can we test whether something is contained within a requirement?
    #       If so how do we do that? Do we need to test against the _name_ of
    #       the thing as well as the version? What about the markers?
    # TODO: Can we normalize the name and extra name?

    __slots__ = ("extras", "marker", "name", "specifier", "url")

    def __init__(self, requirement_string: str) -> None:
        try:
            parsed = _parse_requirement(requirement_string)
        except ParserSyntaxError as e:
            raise InvalidRequirement(str(e)) from e

        self.name: str = parsed.name
        self.url: str | None = parsed.url or None
        self.extras: set[str] = set(parsed.extras)
        try:
            self.specifier: SpecifierSet = SpecifierSet(parsed.specifier)
        except InvalidSpecifier as e:
            raise InvalidRequirement(str(e)) from e
        self.marker: Marker | None = None
        if parsed.marker is not None:
            self.marker = Marker.__new__(Marker)
            self.marker._markers = _normalize_extra_values(parsed.marker)

    def plan_extras(self) -> ExtrasEditPlan:
        """Create an explicit plan for editing this requirement's extras.

        The plan records this requirement as its baseline and lets you stage
        canonical extras to add and/or remove. Call :meth:`ExtrasEditPlan.apply`
        to produce a new :class:`Requirement`; this requirement is left
        unchanged.

        .. doctest::

            >>> from packaging.requirements import Requirement
            >>> req = Requirement('foo[a]; python_version < "3.12"')
            >>> req.plan_extras().add("b").apply()
            <Requirement('foo[a,b]; python_version < "3.12"')>
        """
        return ExtrasEditPlan(self)

    def _iter_parts(self, name: str) -> Iterator[str]:
        yield name

        if self.extras:
            formatted_extras = ",".join(sorted(self.extras))
            yield f"[{formatted_extras}]"

        if self.specifier:
            yield str(self.specifier)

        if self.url:
            yield f" @ {self.url}"
            if self.marker:
                yield " "

        if self.marker:
            yield f"; {self.marker}"

    def __getstate__(self) -> tuple[str, bool | None]:
        # Return the requirement string for compactness and stability, paired
        # with the specifier's explicit prereleases override, which is not
        # captured by the string form. Re-parsed on load to reconstruct all
        # other fields.
        return (str(self), self.specifier._prereleases)

    def __setstate__(self, state: object) -> None:
        if isinstance(state, str):
            # Format (26.2): just the requirement string.
            requirement_string: str = state
            prereleases: bool | None = None
        elif (
            isinstance(state, tuple)
            and len(state) == 2
            and isinstance(state[0], str)
            and (state[1] is None or isinstance(state[1], bool))
        ):
            # New format (26.3+): (requirement string, specifier prereleases).
            requirement_string, prereleases = state
        elif isinstance(state, dict) and state.keys() >= set(self.__slots__):
            # Old format (packaging <= 26.1, no __slots__): plain __dict__.
            for key in self.__slots__:
                setattr(self, key, state[key])
            return
        else:
            raise TypeError(f"Cannot restore Requirement from {state!r}")

        try:
            tmp = Requirement(requirement_string)
        except InvalidRequirement as exc:
            raise TypeError(f"Cannot restore Requirement from {state!r}") from exc
        self.name = tmp.name
        self.url = tmp.url
        self.extras = tmp.extras
        self.specifier = tmp.specifier
        self.specifier._prereleases = prereleases
        self.marker = tmp.marker

    def __str__(self) -> str:
        return "".join(self._iter_parts(self.name))

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}({str(self)!r})>"

    def __hash__(self) -> int:
        # Mirror __eq__ by hashing the canonical specifier object rather than
        # its raw string. ``_iter_parts`` yields ``str(self.specifier)``, which
        # is non-canonical, so trailing-zero-equivalent requirements such as
        # ``foo==1.0.0`` and ``foo==1.0.0.0`` (which compare equal) would
        # otherwise hash differently, breaking the hash/__eq__ invariant.
        return hash(
            (
                canonicalize_name(self.name),
                frozenset(canonicalize_name(e) for e in self.extras),
                self.specifier,
                self.url,
                self.marker,
            )
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Requirement):
            return NotImplemented

        # Extras must be normalized before comparison as per PEP 685.
        self_extras = frozenset(canonicalize_name(e) for e in self.extras)
        other_extras = frozenset(canonicalize_name(e) for e in other.extras)
        return (
            canonicalize_name(self.name) == canonicalize_name(other.name)
            and self_extras == other_extras
            and self.specifier == other.specifier
            and self.url == other.url
            and self.marker == other.marker
        )


class ExtrasEditPlan:
    """An explicit, validated plan for editing a :class:`Requirement`'s extras.

    An extras edit plan records the baseline extras of a requirement and a set
    of canonical extras to add and/or remove. Applying the plan splices the
    original requirement string at the extras span reported by the real parser,
    so the package name, URL, version specifier and marker bytes are preserved
    verbatim. The edited string is then re-parsed to validate it before a new
    :class:`Requirement` is returned; the original requirement is never
    modified.

    Extras are canonicalized per PEP 685 (case, and ``-``/``_``/``.``
    separators), so adding ``"B"`` to a requirement that already provides
    ``"b"`` is a no-op rather than a duplicate entry.

    :param requirement: The requirement to edit, either a :class:`Requirement`
        or a requirement string.

    .. doctest::

        >>> from packaging.requirements import ExtrasEditPlan, Requirement
        >>> req = Requirement('foo[a]; python_version < "3.12"')
        >>> edited = ExtrasEditPlan(req).add("b").apply()
        >>> edited
        <Requirement('foo[a,b]; python_version < "3.12"')>
        >>> str(edited.marker)
        'python_version < "3.12"'
        >>> Requirement('foo').plan_extras().add("a").apply()
        <Requirement('foo[a]')>
    """

    __slots__ = ("_baseline", "_operations", "_requirement", "_source")

    def __init__(self, requirement: str | Requirement) -> None:
        if isinstance(requirement, Requirement):
            self._requirement: Requirement | None = requirement
            self._source: str = str(requirement)
            self._baseline: frozenset[str] = frozenset(
                canonicalize_name(extra) for extra in requirement.extras
            )
        elif isinstance(requirement, str):
            self._requirement = None
            self._source = requirement
            try:
                parsed = _parse_requirement(requirement)
            except ParserSyntaxError as e:
                raise InvalidRequirement(str(e)) from e
            self._baseline = frozenset(
                canonicalize_name(extra) for extra in parsed.extras
            )
        else:
            raise TypeError(
                "requirement must be a str or a Requirement, not "
                f"{type(requirement).__name__}"
            )
        self._operations: list[tuple[str, str]] = []

    def add(self, *extras: str) -> ExtrasEditPlan:
        """Stage canonical extras to add when the plan is applied.

        Adding an extra that the baseline already provides (under any case or
        separator spelling) is a no-op.
        """
        for extra in extras:
            self._operations.append(("add", canonicalize_name(extra)))
        return self

    def remove(self, *extras: str) -> ExtrasEditPlan:
        """Stage canonical extras to remove when the plan is applied.

        Removing an extra that the baseline does not provide is a no-op.
        """
        for extra in extras:
            self._operations.append(("remove", canonicalize_name(extra)))
        return self

    def _current_extras(self) -> frozenset[str]:
        if self._requirement is None:
            return self._baseline
        return frozenset(
            canonicalize_name(extra) for extra in self._requirement.extras
        )

    def apply(self) -> Requirement:
        """Apply the plan and return a new, validated :class:`Requirement`.

        The baseline is checked first: if the :class:`Requirement` the plan was
        created from has had its extras changed since the plan was made, the
        plan is rejected rather than applied against a stale baseline.

        Staged operations are replayed in call order, so adding an extra and
        then removing it (or vice versa) yields no net change.

        :raises InvalidRequirement: if the baseline has changed, or if the
            edited requirement cannot be re-parsed with the same name, URL,
            specifier and marker.
        """
        if self._current_extras() != self._baseline:
            raise InvalidRequirement(
                "Cannot apply extras edit plan: the baseline requirement's "
                "extras have changed since the plan was created"
            )

        parsed = _parse_requirement(self._source)
        new_extras_set = set(self._baseline)
        for operation, extra in self._operations:
            if operation == "add":
                new_extras_set.add(extra)
            else:
                new_extras_set.discard(extra)
        new_extras = sorted(new_extras_set)
        start, end = parsed.extras_span
        replacement = f"[{','.join(new_extras)}]" if new_extras else ""
        new_string = self._source[:start] + replacement + self._source[end:]

        # Re-parse the edited string to validate it, and confirm that the bytes
        # outside the extras span are unchanged -- so the name, URL, specifier
        # and marker are preserved verbatim.
        try:
            new_parsed = _parse_requirement(new_string)
        except ParserSyntaxError as e:
            raise InvalidRequirement(str(e)) from e
        if (
            frozenset(canonicalize_name(e) for e in new_parsed.extras)
            != frozenset(new_extras)
            or new_string[:start] != self._source[:start]
            or new_string[start + len(replacement):] != self._source[end:]
        ):
            raise InvalidRequirement(
                "Cannot apply extras edit plan: the edited requirement could "
                "not be re-parsed with the same name, URL, specifier and marker"
            )

        return Requirement(new_string)
