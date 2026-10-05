from __future__ import annotations

import pytest

from packaging.tags import Tag
from packaging.wheel_url import (
    COMPATIBLE,
    INCOMPATIBLE,
    MARKER_NOT_ENABLED,
    NAME_CONFLICT,
    UNKNOWN,
    verify_wheel_url,
    wheel_filename_from_url,
)

# Local pytest command (run from the repository root, against the local
# source tree rather than an installed copy):
#
#     PYTHONPATH=src python -m pytest tests/test_wheel_url.py -q
#
# On Windows PowerShell:
#
#     $env:PYTHONPATH="src"; python -m pytest tests/test_wheel_url.py -q

PY3_ANY = frozenset({Tag("py3", "none", "any")})
WIN_AMD64 = frozenset({Tag("cp313", "cp313", "win_amd64")})
LINUX_X86_64 = frozenset({Tag("cp313", "cp313", "manylinux_2_17_x86_64")})


# ---------------------------------------------------------------------------
# URL path decoding: the fragment hash and the query string are never
# treated as part of the wheel filename.
# ---------------------------------------------------------------------------


def test_wheel_filename_from_url_uses_path_only() -> None:
    url = "https://example.com/files/bar-1.0-py3-none-any.whl?token=abc#frag"
    assert wheel_filename_from_url(url) == "bar-1.0-py3-none-any.whl"


def test_wheel_filename_from_url_fragment_not_used() -> None:
    # The wheel name sitting in the fragment must not be mistaken for the
    # filename: the path basename is "download", which is not a wheel name.
    url = "https://example.com/download#bar-1.0-py3-none-any.whl"
    assert wheel_filename_from_url(url) == "download"


def test_wheel_filename_from_url_no_path() -> None:
    assert wheel_filename_from_url("https://example.com") is None
    assert wheel_filename_from_url("https://example.com/") is None


def test_wheel_filename_from_url_percent_encoded() -> None:
    url = "https://example.com/files/%62%61%72-1.0-py3-none-any.whl"
    assert wheel_filename_from_url(url) == "bar-1.0-py3-none-any.whl"


def test_wheel_filename_from_url_file_scheme() -> None:
    url = "file:///srv/wheels/bar-1.0-py3-none-any.whl"
    assert wheel_filename_from_url(url) == "bar-1.0-py3-none-any.whl"


# ---------------------------------------------------------------------------
# Three dependency examples.
# ---------------------------------------------------------------------------


def test_example_1_foo_depends_on_bar_wheel_name_conflict() -> None:
    # Example 1: foo depends on a wheel whose filename identity is "bar"
    # -> package name conflict (包名冲突).
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/files/bar-1.0-py3-none-any.whl",
        target_tags=PY3_ANY,
    )
    assert result.status == NAME_CONFLICT
    assert result.is_valid_wheel
    assert result.name == "bar"
    assert result.version is not None
    assert str(result.version) == "1.0"
    assert any("冲突" in reason for reason in result.reasons)


def test_example_2_platform_tags_disjoint_incompatible() -> None:
    # Example 2: the wheel's platform tags do not intersect the target
    # tag set -> incompatible (不兼容).
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/files/foo-1.0-cp313-cp313-win_amd64.whl",
        target_tags=LINUX_X86_64,
    )
    assert result.status == INCOMPATIBLE
    assert result.is_valid_wheel
    assert result.tags == frozenset({Tag("cp313", "cp313", "win_amd64")})
    assert any("不相交" in reason for reason in result.reasons)


def test_example_3_py3_none_any_compatible() -> None:
    # Example 3: a pure py3-none-any wheel -> compatible (兼容).
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/files/foo-1.0-py3-none-any.whl",
        target_tags=PY3_ANY,
    )
    assert result.status == COMPATIBLE
    assert result.is_valid_wheel
    assert result.tags == frozenset({Tag("py3", "none", "any")})
    assert not result.reasons


# ---------------------------------------------------------------------------
# Unknown: a URL without a recognizable wheel name is not a valid wheel,
# and the version must not be guessed by downloading.
# ---------------------------------------------------------------------------


def test_unknown_when_url_has_no_wheel_name() -> None:
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/download",
        target_tags=PY3_ANY,
    )
    assert result.status == UNKNOWN
    assert not result.is_valid_wheel
    assert result.wheel_filename == "download"
    assert result.name is None
    assert result.version is None
    assert result.build is None
    assert result.tags is None


def test_unknown_when_filename_is_not_a_wheel() -> None:
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/files/foo-1.0.tar.gz",
        target_tags=PY3_ANY,
    )
    assert result.status == UNKNOWN
    assert not result.is_valid_wheel
    assert result.wheel_filename == "foo-1.0.tar.gz"


def test_unknown_when_path_is_a_directory() -> None:
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/files/",
        target_tags=PY3_ANY,
    )
    assert result.status == UNKNOWN
    assert not result.is_valid_wheel
    # The version is not guessed even though the requirement pins one.
    assert result.version is None


def test_unknown_does_not_guess_version() -> None:
    # Even with a pinned requirement version, an unrecognizable wheel
    # name stays "unknown": nothing is downloaded to guess the version.
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/files/foo-latest.whl",
        target_tags=PY3_ANY,
    )
    assert result.status == UNKNOWN
    assert not result.is_valid_wheel
    assert result.version is None


def test_unknown_when_wheel_filename_malformed() -> None:
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/files/foo-1.0-notatag.whl",
        target_tags=PY3_ANY,
    )
    assert result.status == UNKNOWN
    assert not result.is_valid_wheel


# ---------------------------------------------------------------------------
# Markers: a marker that is not enabled is reported separately, not
# conflated with platform incompatibility.
# ---------------------------------------------------------------------------


def test_marker_not_enabled_reported_separately() -> None:
    result = verify_wheel_url(
        "foo==1.0; python_version < '3'",
        "https://example.com/files/foo-1.0-py3-none-any.whl",
        target_tags=PY3_ANY,
    )
    assert result.status == MARKER_NOT_ENABLED
    # The wheel itself is recognizable; only the marker is off.
    assert result.is_valid_wheel
    assert any("未启用" in reason for reason in result.reasons)


def test_marker_enabled_falls_through_to_compatible() -> None:
    result = verify_wheel_url(
        "foo==1.0; python_version >= '3'",
        "https://example.com/files/foo-1.0-py3-none-any.whl",
        target_tags=PY3_ANY,
    )
    assert result.status == COMPATIBLE


def test_marker_not_enabled_with_environment_override() -> None:
    result = verify_wheel_url(
        "foo==1.0; sys_platform == 'win32'",
        "https://example.com/files/foo-1.0-py3-none-any.whl",
        target_tags=PY3_ANY,
        environment={"sys_platform": "win32"},
    )
    assert result.status == COMPATIBLE


# ---------------------------------------------------------------------------
# Canonical names and build tags follow packaging's real rules.
# ---------------------------------------------------------------------------


def test_canonical_name_conflict_resolved() -> None:
    # Foo_Bar and foo_bar canonicalize to the same name, so this is
    # compatible rather than a name conflict.
    result = verify_wheel_url(
        "Foo_Bar==1.0",
        "https://example.com/files/foo_bar-1.0-py3-none-any.whl",
        target_tags=PY3_ANY,
    )
    assert result.status == COMPATIBLE
    assert result.name == "foo-bar"


def test_canonical_name_conflict_still_detected() -> None:
    result = verify_wheel_url(
        "Foo==1.0",
        "https://example.com/files/bar-1.0-py3-none-any.whl",
        target_tags=PY3_ANY,
    )
    assert result.status == NAME_CONFLICT
    assert result.name == "bar"


def test_build_tag_parsed() -> None:
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/files/foo-1.0-2-py3-none-any.whl",
        target_tags=PY3_ANY,
    )
    assert result.status == COMPATIBLE
    assert result.build == (2, "")


def test_build_tag_with_suffix_parsed() -> None:
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/files/foo-1.0-2local-py3-none-any.whl",
        target_tags=PY3_ANY,
    )
    assert result.status == COMPATIBLE
    assert result.build == (2, "local")


# ---------------------------------------------------------------------------
# The requirement's own direct URL is used when no URL is given.
# ---------------------------------------------------------------------------


def test_requirement_own_url_used() -> None:
    result = verify_wheel_url(
        "foo @ https://example.com/files/foo-1.0-py3-none-any.whl",
        target_tags=PY3_ANY,
    )
    assert result.status == COMPATIBLE


def test_requirement_own_url_unknown() -> None:
    result = verify_wheel_url(
        "foo @ https://example.com/download",
        target_tags=PY3_ANY,
    )
    assert result.status == UNKNOWN
    assert not result.is_valid_wheel


def test_no_url_at_all_is_unknown() -> None:
    result = verify_wheel_url("foo==1.0", target_tags=PY3_ANY)
    assert result.status == UNKNOWN
    assert not result.is_valid_wheel


# ---------------------------------------------------------------------------
# Default target tags: the current interpreter's compatible tags.
# ---------------------------------------------------------------------------


def test_default_target_tags() -> None:
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/files/foo-1.0-py3-none-any.whl",
    )
    assert result.status == COMPATIBLE
    assert result.target_tags  # non-empty


@pytest.mark.parametrize(
    ("requirement", "url", "expected"),
    [
        (
            "foo==1.0",
            "https://example.com/files/foo-1.0-py3-none-any.whl",
            COMPATIBLE,
        ),
        (
            "foo==1.0",
            "https://example.com/files/bar-1.0-py3-none-any.whl",
            NAME_CONFLICT,
        ),
        ("foo==1.0", "https://example.com/download", UNKNOWN),
    ],
)
def test_status_parametrized(
    requirement: str, url: str, expected: str
) -> None:
    result = verify_wheel_url(requirement, url, target_tags=PY3_ANY)
    assert result.status == expected
