"""Offline verification of wheel URLs attached to install requirements.

This module correlates three identities that appear when a dependency is
pinned to a direct URL (PEP 508) or listed in a lock file:

1. the distribution name of the :class:`~packaging.requirements.Requirement`
   (the dependency that was asked for),
2. the project name encoded in the wheel file name in the URL path
   (PEP 427, parsed with :func:`packaging.utils.parse_wheel_filename`), and
3. the set of target compatibility :class:`~packaging.tags.Tag` objects that
   the installing interpreter supports.

Everything is *offline*: a URL is only parsed as text.  Nothing is ever
downloaded, so a URL that does not end in a recognizable wheel file name can
never be used to guess a project's name or version -- its result is simply
``unknown``.

Results
-------

Each check returns a :class:`WheelVerification` with one of these statuses:

``valid``
    The wheel name agrees with the requirement name and at least one of its
    tags is supported by the target tag set.

``name-conflict``
    The wheel file name identifies a different distribution than the
    requirement asks for (e.g. ``foo`` depends on ``bar`` but the URL serves
    ``foo-...whl``).

``incompatible``
    The names agree but the wheel's (interpreter, ABI, platform) tag set is
    disjoint from the target tag set.

``unknown``
    No wheel identity could be established: the requirement has no URL, the
    URL path does not end in ``.whl``, or the wheel file name is malformed.
    ``unknown`` is *not* a usable wheel.

``skipped-marker``
    The requirement carries an environment marker that evaluates to false in
    the given environment, so the requirement is not active.  Markers are
    reported separately and never evaluated implicitly.

Canonical names (PEP 503) and build tags (PEP 427) are handled by the real
rules in :mod:`packaging.utils`; this module never re-implements them.

Examples
--------
Three small dependency examples (also exercised by the test-suite)::

    # 1. compatible universal wheel -> valid
    "foo @ https://example.com/foo-1.2.3-py3-none-any.whl"

    # 2. foo's dependency on bar is served a foo wheel -> name-conflict
    "bar @ https://example.com/foo-1.2.3-py3-none-any.whl"

    # 3. platform tags do not intersect -> incompatible
    "foo @ https://example.com/foo-1.0-cp312-cp312-win_amd64.whl"
    # checked against a pure manylinux target tag set

Run the focused tests locally with::

    # Windows PowerShell
    $env:PYTHONPATH="src"; python -m pytest tests/test_wheel_url.py -q
    # Windows cmd
    set PYTHONPATH=src&& python -m pytest tests/test_wheel_url.py -q
    # POSIX shell
    PYTHONPATH=src python -m pytest tests/test_wheel_url.py -q
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from .requirements import Requirement
from .utils import (
    BuildTag,
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from .tags import Tag

__all__ = [
    "STATUS_INCOMPATIBLE",
    "STATUS_NAME_CONFLICT",
    "STATUS_SKIPPED_MARKER",
    "STATUS_UNKNOWN",
    "STATUS_VALID",
    "WheelVerification",
    "verify_requirements",
    "verify_wheel_url",
    "wheel_filename_from_url",
]

STATUS_VALID = "valid"
STATUS_NAME_CONFLICT = "name-conflict"
STATUS_INCOMPATIBLE = "incompatible"
STATUS_UNKNOWN = "unknown"
STATUS_SKIPPED_MARKER = "skipped-marker"


def wheel_filename_from_url(url: str) -> str | None:
    """Extract the percent-decoded wheel file name from *url*, if present.

    Only the *path* component of the URL is considered.  The URL fragment
    (``#sha256=...``) is never treated as part of the file name, and neither
    is the ``?query``.  Percent-encoded path segments (for example spaces or
    non-ASCII names encoded as ``%20``) are decoded exactly once, matching
    how an HTTP server would resolve them.

    :returns: the decoded basename if the path ends with ``.whl`` (case
        sensitive, as the wheel extension is defined to be lowercase),
        otherwise ``None``.
    """
    # ``urlsplit`` cleanly separates path from query and fragment, so a hash
    # fragment can never leak into the file name even without a manual split.
    path = urlsplit(url).path
    if not path:
        return None
    name = path.rsplit("/", 1)[-1]
    name = unquote(name)
    if not name.endswith(".whl"):
        return None
    return name


@dataclass(frozen=True)
class WheelVerification:
    """Outcome of checking one requirement against one target environment.

    :ivar status: one of the ``STATUS_*`` constants.
    :ivar requirement: canonicalized name of the requirement being checked.
    :ivar wheel_name: canonicalized name parsed from the wheel file name, or
        ``None`` when no valid wheel identity was available.
    :ivar version: version string parsed from the wheel file name, or
        ``None`` for non-wheel results.
    :ivar build: PEP 427 build tag as produced by
        :func:`packaging.utils.parse_wheel_filename` (``()`` when absent).
    :ivar wheel_tags: tags encoded by the wheel file name (empty when
        unknown).
    :ivar reason: human-readable explanation of the status.
    """

    status: str
    requirement: str
    wheel_name: str | None = None
    version: str | None = None
    build: BuildTag = ()
    wheel_tags: frozenset[Tag] = field(default_factory=frozenset)
    reason: str = ""

    @property
    def is_valid_wheel(self) -> bool:
        """True only when the URL resolves to a usable compatible wheel."""
        return self.status == STATUS_VALID


def _unknown(requirement_name: str, reason: str) -> WheelVerification:
    return WheelVerification(
        status=STATUS_UNKNOWN, requirement=requirement_name, reason=reason
    )


def verify_wheel_url(
    requirement: Requirement | str,
    target_tags: Iterable[Tag],
    *,
    environment: Mapping[str, str] | None = None,
) -> WheelVerification:
    """Verify a single direct-URL requirement against *target_tags*, offline.

    :param requirement: a :class:`~packaging.requirements.Requirement` or a
        PEP 508 requirement string.  A direct URL (``name @ url``) is where
        the wheel identity comes from.
    :param target_tags: tags supported by the target interpreter, as produced
        by :func:`packaging.tags.sys_tags` or any other iterable of
        :class:`~packaging.tags.Tag` (an explicit fixture keeps checks
        deterministic and offline).
    :param environment: optional marker environment passed to
        :meth:`packaging.markers.Marker.evaluate`; ``None`` means the current
        interpreter environment.  Markers are never silently enabled or
        ignored -- a false marker yields ``skipped-marker``.
    """
    if isinstance(requirement, str):
        requirement = Requirement(requirement)

    req_name = canonicalize_name(requirement.name)

    # A disabled Requirement is reported separately; its URL/tags are
    # irrelevant for this environment.
    if requirement.marker is not None and not requirement.marker.evaluate(environment):
        return WheelVerification(
            status=STATUS_SKIPPED_MARKER,
            requirement=req_name,
            reason=f"marker not satisfied: {requirement.marker}",
        )

    if requirement.url is None:
        return _unknown(req_name, "requirement has no direct URL")

    filename = wheel_filename_from_url(requirement.url)
    if filename is None:
        # No recognizable wheel name: report unknown instead of downloading
        # anything to guess at the name/version.
        return _unknown(
            req_name,
            "URL path does not identify a .whl file; refusing to guess by downloading",
        )

    try:
        # Real PEP 427/503 rules for name normalization, version parsing and
        # build tag parsing, including compressed tag sets.
        wheel_name, wheel_version, build, wheel_tags = parse_wheel_filename(filename)
    except InvalidWheelFilename as exc:
        return _unknown(req_name, f"unrecognized wheel file name {filename!r}: {exc}")

    if wheel_name != req_name:
        return WheelVerification(
            status=STATUS_NAME_CONFLICT,
            requirement=req_name,
            wheel_name=wheel_name,
            version=str(wheel_version),
            build=build,
            wheel_tags=wheel_tags,
            reason=(
                f"requirement {req_name!r} does not match wheel distribution "
                f"name {wheel_name!r} from {filename!r}"
            ),
        )

    supported = frozenset(target_tags)
    if wheel_tags.isdisjoint(supported):
        return WheelVerification(
            status=STATUS_INCOMPATIBLE,
            requirement=req_name,
            wheel_name=wheel_name,
            version=str(wheel_version),
            build=build,
            wheel_tags=wheel_tags,
            reason=(
                f"wheel tags {sorted(str(t) for t in wheel_tags)} are not "
                f"supported by the target tag set"
            ),
        )

    matched = sorted(str(t) for t in wheel_tags & supported)
    return WheelVerification(
        status=STATUS_VALID,
        requirement=req_name,
        wheel_name=wheel_name,
        version=str(wheel_version),
        build=build,
        wheel_tags=wheel_tags,
        reason=f"compatible wheel; matching target tag(s): {matched}",
    )


def verify_requirements(
    requirements: Iterable[Requirement | str],
    target_tags: Iterable[Tag],
    *,
    environment: Mapping[str, str] | None = None,
) -> list[WheelVerification]:
    """Verify many requirements against one target tag set.

    See :func:`verify_wheel_url` for details.  *target_tags* is materialized
    once, so passing a one-shot generator is safe.
    """
    tags = tuple(target_tags)
    return [
        verify_wheel_url(req, tags, environment=environment) for req in requirements
    ]
