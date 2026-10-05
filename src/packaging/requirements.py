# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.
from __future__ import annotations

from typing import TYPE_CHECKING

from ._parser import MarkerList
from ._parser import parse_requirement as _parse_requirement
from ._tokenizer import DEFAULT_RULES as _DEFAULT_RULES
from ._tokenizer import ParserSyntaxError
from ._tokenizer import Tokenizer as _Tokenizer
from .markers import Marker, _normalize_extra_values
from .specifiers import InvalidSpecifier, SpecifierSet
from .utils import canonicalize_name

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "ExtrasEditPlan",
    "InvalidRequirement",
    "Requirement",
    "StaleExtrasEditPlanError",
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


class StaleExtrasEditPlanError(ValueError):
    """
    An extras edit plan was applied to a requirement string that differs
    from the baseline the plan was created from.

    .. versionadded:: 26.4
    """


def _check_extra_name(extra: str) -> None:
    """Validate that ``extra`` is a single, well-formed extra name."""
    if not isinstance(extra, str):
        raise TypeError(f"extra name must be a string, got {type(extra).__name__}")
    tokenizer = _Tokenizer(extra, rules=_DEFAULT_RULES)
    try:
        tokenizer.expect("IDENTIFIER", expected="extra name")
        tokenizer.expect("END", expected="end of extra name")
    except ParserSyntaxError as e:
        raise InvalidRequirement(f"Invalid extra name: {extra!r}") from e


def _marker_text(marker: MarkerList | None) -> str | None:
    """Serialize a parsed marker the same way :class:`Requirement` does."""
    if marker is None:
        return None
    normalized = Marker.__new__(Marker)
    normalized._markers = _normalize_extra_values(marker)
    return str(normalized)


class ExtrasEditPlan:
    """An explicit plan to add or remove extras on a requirement string.

    The plan is bound to the exact requirement string (the *baseline*) it was
    created from. The extras section is located by the real requirement
    parser, so brackets appearing elsewhere (for example in an IPv6 URL such
    as ``http://[::1]/``) are never mistaken for extras. Applying the plan
    only rewrites the extras section: the package name, URL, specifier and
    marker keep their original bytes, and the result is re-parsed to verify
    it is still a valid requirement with an unchanged non-extras payload.

    Extra names are compared and deduplicated per PEP 685 (case-insensitive,
    with ``-``, ``_`` and ``.`` treated as equivalent separators). Kept
    extras retain their original spelling and order.

    .. versionadded:: 26.4

    >>> from packaging.requirements import ExtrasEditPlan
    >>> plan = ExtrasEditPlan('foo[a]; python_version < "3.12"')
    >>> plan.add("b").apply()
    'foo[a,b]; python_version < "3.12"'
    >>> ExtrasEditPlan("foo @ http://[::1]:8080/pkg").add("x").apply()
    'foo[x] @ http://[::1]:8080/pkg'
    >>> ExtrasEditPlan("foo[SECURE,secure,sec_ure,sec-ure]").apply()
    'foo[SECURE,sec_ure]'
    """

    __slots__ = (
        "_added",
        "_extras_span",
        "_kept",
        "_name_span",
        "_parsed",
        "_removed",
        "_source",
    )

    def __init__(self, requirement_string: str) -> None:
        try:
            parsed = _parse_requirement(requirement_string)
        except ParserSyntaxError as e:
            raise InvalidRequirement(str(e)) from e

        self._source = requirement_string
        self._parsed = parsed
        # Spans recorded by the real parser while tokenizing the baseline.
        self._name_span = parsed.name_span
        self._extras_span = parsed.extras_span
        # Existing extras, deduplicated per PEP 685, keeping the first
        # spelling seen for each canonical name.
        self._kept: list[tuple[str, str]] = []
        seen: set[str] = set()
        for extra in parsed.extras:
            canonical = canonicalize_name(extra)
            if canonical not in seen:
                seen.add(canonical)
                self._kept.append((canonical, extra))
        # Planned changes: canonical name -> spelling to emit / removal set.
        self._added: dict[str, str] = {}
        self._removed: set[str] = set()

    @property
    def baseline(self) -> str:
        """The requirement string this plan was created from."""
        return self._source

    @property
    def extras(self) -> frozenset[str]:
        """The canonical (PEP 685) extra names the edited requirement will have."""
        return frozenset(
            [canonical for canonical, _ in self._kept if canonical not in self._removed]
            + [canonical for canonical in self._added if canonical not in self._removed]
        )

    def add(self, *extras: str) -> ExtrasEditPlan:
        """Plan to add each of ``extras`` (canonicalized per PEP 685).

        Adding an extra that is already present (under any equivalent
        spelling) is a no-op. Returns ``self`` so calls can be chained.
        """
        for extra in extras:
            _check_extra_name(extra)
            canonical = canonicalize_name(extra)
            self._removed.discard(canonical)
            if canonical not in {c for c, _ in self._kept}:
                self._added.setdefault(canonical, extra)
        return self

    def remove(self, *extras: str) -> ExtrasEditPlan:
        """Plan to remove each of ``extras`` (canonicalized per PEP 685).

        Removing an extra that is not present is a no-op. Returns ``self``
        so calls can be chained.
        """
        for extra in extras:
            _check_extra_name(extra)
            canonical = canonicalize_name(extra)
            self._added.pop(canonical, None)
            self._removed.add(canonical)
        return self

    def apply(self, requirement_string: str | None = None) -> str:
        """Apply the planned edits and return the edited requirement string.

        If ``requirement_string`` is given it must be identical to the
        baseline; a changed baseline means this plan's recorded offsets are
        stale, so :class:`StaleExtrasEditPlanError` is raised instead of
        silently editing the wrong text.

        The result is re-parsed after editing; if it no longer parses, or
        the name, URL, specifier or marker changed, an error is raised.
        """
        if requirement_string is None:
            requirement_string = self._source
        elif requirement_string != self._source:
            raise StaleExtrasEditPlanError(
                "requirement string changed since this extras edit plan was "
                "created; build a new plan from the current string"
            )

        final = [
            original
            for canonical, original in self._kept
            if canonical not in self._removed
        ]
        final.extend(
            spelling
            for canonical, spelling in self._added.items()
            if canonical not in self._removed
        )

        if final:
            replacement = "[" + ",".join(final) + "]"
            if self._extras_span is not None:
                start, end = self._extras_span
            else:
                # No extras section yet: insert right after the package name.
                assert self._name_span is not None
                start = end = self._name_span[1]
        else:
            if self._extras_span is None:
                # Nothing to remove and nothing to add: no edit at all.
                return requirement_string
            replacement = ""
            start, end = self._extras_span

        edited = requirement_string[:start] + replacement + requirement_string[end:]

        # Re-parse the edited string: it must still be a valid requirement
        # and only the extras section may have changed.
        try:
            reparsed = _parse_requirement(edited)
        except ParserSyntaxError as e:
            raise InvalidRequirement(str(e)) from e
        if (
            reparsed.name != self._parsed.name
            or reparsed.url != self._parsed.url
            or reparsed.specifier != self._parsed.specifier
            or _marker_text(reparsed.marker) != _marker_text(self._parsed.marker)
        ):
            raise InvalidRequirement(
                f"extras edit corrupted the requirement: {edited!r}"
            )

        return edited
