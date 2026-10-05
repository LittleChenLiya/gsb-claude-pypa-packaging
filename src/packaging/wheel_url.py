# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

"""
Offline wheel URL verifier.

This module verifies a wheel *direct URL* against a PEP 508 requirement
(package name and, when present, its marker) and a target set of platform
tags. The check is fully offline: the URL is never fetched, so a URL that
does not carry a recognizable wheel filename can only be reported as
"unknown" -- the version must not be guessed by downloading anything.

The wheel filename identity (canonical name, version, build tag and tags)
is parsed with :func:`packaging.utils.parse_wheel_filename`, and canonical
names are compared with :func:`packaging.utils.canonicalize_name`, i.e. the
real packaging rules rather than hand-rolled string comparisons.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from urllib.parse import unquote, urlsplit

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.tags import Tag, compatible_tags
from packaging.utils import (
    BuildTag,
    InvalidWheelFilename,
    NormalizedName,
    canonicalize_name,
    parse_wheel_filename,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from collections.abc import Set as AbstractSet

    from packaging.version import Version

__all__ = [
    "COMPATIBLE",
    "INCOMPATIBLE",
    "MARKER_NOT_ENABLED",
    "NAME_CONFLICT",
    "UNKNOWN",
    "WheelUrlVerification",
    "verify_wheel_url",
    "wheel_filename_from_url",
]


# Verification statuses. The Chinese labels are the canonical status
# values used by callers and tests.
COMPATIBLE = "兼容"
INCOMPATIBLE = "不兼容"
NAME_CONFLICT = "包名冲突"
UNKNOWN = "未知"
MARKER_NOT_ENABLED = "marker未启用"


def __dir__() -> list[str]:
    return __all__


def wheel_filename_from_url(url: str) -> str | None:
    """
    Return the wheel filename embedded in *url*, or ``None`` when the URL
    path does not end in a filename.

    Only the decoded *path* component of the URL is inspected: the query
    string and the fragment are never considered part of the filename.
    A fragment such as ``#bar-1.0-py3-none-any.whl`` is therefore not
    mistaken for the wheel name, and percent-encoded paths are decoded
    before the basename is taken.

    :param str url: The direct URL to inspect.
    """
    path = urlsplit(url).path
    filename = posixpath.basename(unquote(path))
    return filename or None


@dataclass
class WheelUrlVerification:
    """
    The result of verifying a wheel URL against a requirement and a
    target tag set.

    .. attribute:: status

        One of :data:`COMPATIBLE`, :data:`INCOMPATIBLE`,
        :data:`NAME_CONFLICT`, :data:`UNKNOWN` or
        :data:`MARKER_NOT_ENABLED`.

    .. attribute:: is_valid_wheel

        ``True`` when the URL carried a recognizable wheel filename. An
        :data:`UNKNOWN` result is *not* a valid wheel and must never be
        treated as compatible.
    """

    requirement: Requirement
    url: str
    status: str
    wheel_filename: str | None = None
    name: NormalizedName | None = None
    version: Version | None = None
    build: BuildTag | None = None
    tags: frozenset[Tag] | None = None
    target_tags: frozenset[Tag] = field(default_factory=frozenset)
    reasons: list[str] = field(default_factory=list)

    @property
    def is_valid_wheel(self) -> bool:
        """Whether the URL carried a recognizable wheel filename."""
        return self.status != UNKNOWN


def verify_wheel_url(
    requirement: str | Requirement,
    url: str | None = None,
    *,
    target_tags: Iterable[Tag] | None = None,
    environment: Mapping[str, str | AbstractSet[str]] | None = None,
) -> WheelUrlVerification:
    """
    Verify a wheel *direct URL* against a requirement and a target tag
    set, without downloading anything.

    The requirement's package name is associated with the wheel filename
    identity parsed from the URL path, and the wheel's tags are checked
    for intersection with the target tag set. Markers, when present, are
    evaluated against the environment and reported separately as
    :data:`MARKER_NOT_ENABLED` rather than being conflated with platform
    incompatibility.

    If the URL does not carry a recognizable wheel filename, the result
    is :data:`UNKNOWN` and no attempt is made to guess the version by
    fetching the URL.

    :param requirement: A requirement string or
        :class:`~packaging.requirements.Requirement`.
    :param url: The wheel direct URL. When ``None``, the requirement's own
        URL (PEP 508 ``name @ url``) is used.
    :param target_tags: The platform tags to check against. Defaults to
        the tags compatible with the current interpreter.
    :param environment: Marker environment overrides.
    """
    if isinstance(requirement, str):
        requirement = Requirement(requirement)

    if url is None:
        url = requirement.url or ""

    if target_tags is None:
        target = frozenset(compatible_tags())
    else:
        target = frozenset(target_tags)

    reasons: list[str] = []

    # Markers are reported separately from platform compatibility: a
    # marker that is not enabled means the wheel is not a candidate in
    # this environment, not that its platform tags are incompatible.
    if requirement.marker is not None:
        # default_environment() returns a fresh copy, so mutating it is
        # safe; the cast matches the pattern used in Marker.evaluate.
        current_environment = cast(
            "dict[str, str | AbstractSet[str]]", default_environment()
        )
        if environment is not None:
            current_environment.update(environment)
        if not requirement.marker.evaluate(current_environment, context="requirement"):
            reasons.append(f"marker {requirement.marker!s} 在当前环境未启用")
            return WheelUrlVerification(
                requirement=requirement,
                url=url,
                status=MARKER_NOT_ENABLED,
                target_tags=target,
                reasons=reasons,
            )

    filename = wheel_filename_from_url(url)
    if filename is None:
        reasons.append("URL 路径中没有可识别的 wheel 文件名")
        return WheelUrlVerification(
            requirement=requirement,
            url=url,
            status=UNKNOWN,
            target_tags=target,
            reasons=reasons,
        )

    try:
        name, version, build, tags = parse_wheel_filename(filename)
    except InvalidWheelFilename:
        reasons.append(f"无法识别的 wheel 文件名: {filename}")
        return WheelUrlVerification(
            requirement=requirement,
            url=url,
            status=UNKNOWN,
            wheel_filename=filename,
            target_tags=target,
            reasons=reasons,
        )

    req_name = canonicalize_name(requirement.name)
    if name != req_name:
        reasons.append(
            f"wheel 包名 {name!r} 与 requirement 包名 {req_name!r} 冲突"
        )
        return WheelUrlVerification(
            requirement=requirement,
            url=url,
            status=NAME_CONFLICT,
            wheel_filename=filename,
            name=name,
            version=version,
            build=build,
            tags=tags,
            target_tags=target,
            reasons=reasons,
        )

    if not tags & target:
        reasons.append(
            f"wheel 标签 {sorted(str(tag) for tag in tags)} "
            f"与目标标签集合 {sorted(str(tag) for tag in target)} 不相交"
        )
        return WheelUrlVerification(
            requirement=requirement,
            url=url,
            status=INCOMPATIBLE,
            wheel_filename=filename,
            name=name,
            version=version,
            build=build,
            tags=tags,
            target_tags=target,
            reasons=reasons,
        )

    return WheelUrlVerification(
        requirement=requirement,
        url=url,
        status=COMPATIBLE,
        wheel_filename=filename,
        name=name,
        version=version,
        build=build,
        tags=tags,
        target_tags=target,
        reasons=reasons,
    )
