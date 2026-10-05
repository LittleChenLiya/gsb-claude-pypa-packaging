"""Focused, offline tests for :mod:`packaging.wheel_url`.

Local command (run from the repository root)::

    # Windows PowerShell
    $env:PYTHONPATH="src"; python -m pytest tests/test_wheel_url.py -q
    # Windows cmd
    set PYTHONPATH=src&& python -m pytest tests/test_wheel_url.py -q
    # POSIX shell
    PYTHONPATH=src python -m pytest tests/test_wheel_url.py -q
"""

from __future__ import annotations

import pytest

from packaging.requirements import Requirement
from packaging.tags import Tag
from packaging.utils import canonicalize_version
from packaging.wheel_url import (
    STATUS_INCOMPATIBLE,
    STATUS_NAME_CONFLICT,
    STATUS_SKIPPED_MARKER,
    STATUS_UNKNOWN,
    STATUS_VALID,
    verify_requirements,
    verify_wheel_url,
    wheel_filename_from_url,
)

# A small, deterministic offline "manylinux-ish" environment.  sys_tags() is
# deliberately not used: acceptance must not depend on the host machine.
LINUX_TAGS = frozenset(
    [
        Tag("py3", "none", "any"),
        Tag("cp312", "cp312", "manylinux_2_17_x86_64"),
        Tag("cp312", "abi3", "manylinux_2_17_x86_64"),
        Tag("cp312", "none", "manylinux_2_17_x86_64"),
    ]
)

WIN_TAG = Tag("cp312", "cp312", "win_amd64")

# The three dependency examples from the module documentation.
DEPENDENCY_EXAMPLES = [
    "foo @ https://example.com/foo-1.2.3-py3-none-any.whl",
    "bar @ https://example.com/foo-1.2.3-py3-none-any.whl",
    "foo @ https://example.com/foo-1.0-cp312-cp312-win_amd64.whl",
]


# --- URL parsing ---------------------------------------------------------


def test_fragment_hash_is_not_part_of_filename() -> None:
    url = (
        "https://example.com/packages/foo-1.0-py3-none-any.whl"
        "#sha256=deadbeefdeadbeefdeadbeefdeadbeef"
    )
    assert wheel_filename_from_url(url) == "foo-1.0-py3-none-any.whl"


def test_query_string_is_not_part_of_filename() -> None:
    url = "https://example.com/foo-1.0-py3-none-any.whl?token=abc&download=true"
    assert wheel_filename_from_url(url) == "foo-1.0-py3-none-any.whl"


def test_path_is_percent_decoded() -> None:
    url = "https://example.com/foo%20bar-1.0-py3-none-any.whl"
    assert wheel_filename_from_url(url) == "foo bar-1.0-py3-none-any.whl"


def test_non_wheel_extension_returns_none() -> None:
    assert wheel_filename_from_url("https://example.com/foo-1.0.tar.gz") is None
    assert wheel_filename_from_url("https://example.com/foo-1.0.zip") is None
    # Fragment content must never be inspected to find a wheel name.
    assert (
        wheel_filename_from_url("https://example.com/page#foo-1.0-py3-none-any.whl")
        is None
    )


def test_directory_url_without_filename_returns_none() -> None:
    assert wheel_filename_from_url("file:///srv/wheels/") is None


# --- acceptance cases ----------------------------------------------------


def test_py3_none_any_wheel_passes() -> None:
    result = verify_wheel_url(Requirement(DEPENDENCY_EXAMPLES[0]), LINUX_TAGS)
    assert result.status == STATUS_VALID
    assert result.is_valid_wheel
    assert result.requirement == "foo"
    assert result.wheel_name == "foo"
    assert result.version == "1.2.3"
    assert result.build == ()
    assert result.wheel_tags == frozenset([Tag("py3", "none", "any")])


def test_foo_depending_on_bar_wheel_is_name_conflict() -> None:
    # The requirement asks for bar but the URL serves a wheel named foo.
    result = verify_wheel_url(Requirement(DEPENDENCY_EXAMPLES[1]), LINUX_TAGS)
    assert result.status == STATUS_NAME_CONFLICT
    assert not result.is_valid_wheel
    assert result.requirement == "bar"
    assert result.wheel_name == "foo"
    assert result.version == "1.2.3"
    assert "bar" in result.reason
    assert "foo" in result.reason


def test_disjoint_platform_tags_are_incompatible() -> None:
    result = verify_wheel_url(Requirement(DEPENDENCY_EXAMPLES[2]), LINUX_TAGS)
    assert result.status == STATUS_INCOMPATIBLE
    assert not result.is_valid_wheel
    assert result.wheel_tags == frozenset([WIN_TAG])


def test_windows_tag_passes_against_windows_target() -> None:
    result = verify_wheel_url(Requirement(DEPENDENCY_EXAMPLES[2]), [WIN_TAG])
    assert result.status == STATUS_VALID


def test_three_dependency_examples_together() -> None:
    results = verify_requirements(DEPENDENCY_EXAMPLES, LINUX_TAGS)
    assert [r.status for r in results] == [
        STATUS_VALID,
        STATUS_NAME_CONFLICT,
        STATUS_INCOMPATIBLE,
    ]
    # Only the first one is a usable wheel; unknown/conflict/incompatible
    # results never count.
    assert [r.is_valid_wheel for r in results] == [True, False, False]


# --- unknown: no guessing, no downloading --------------------------------


def test_direct_url_without_recognizable_wheel_name_is_unknown() -> None:
    # A bare project URL or an HTML index page: nothing to parse offline.
    result = verify_wheel_url("foo @ https://example.com/projects/foo/", LINUX_TAGS)
    assert result.status == STATUS_UNKNOWN
    assert result.wheel_name is None
    assert result.version is None
    assert not result.is_valid_wheel


def test_malformed_wheel_filename_is_unknown() -> None:
    result = verify_wheel_url("foo @ https://example.com/not-a-wheel.whl", LINUX_TAGS)
    assert result.status == STATUS_UNKNOWN
    assert result.wheel_name is None
    assert not result.is_valid_wheel


def test_requirement_without_url_is_unknown() -> None:
    result = verify_wheel_url("foo >= 1.0", LINUX_TAGS)
    assert result.status == STATUS_UNKNOWN
    assert "no direct URL" in result.reason


def test_unknown_is_not_a_valid_wheel() -> None:
    # The wheel extension is defined to be lowercase; .WHL is not a wheel
    # identity and must not be treated as one (case-insensitive guessing).
    result = verify_wheel_url(
        "foo @ https://example.com/foo-9.9.9-py3-none-any.WHL", LINUX_TAGS
    )
    assert result.status == STATUS_UNKNOWN
    assert not result.is_valid_wheel


# --- markers are reported separately -------------------------------------


def test_disabled_marker_is_reported_separately() -> None:
    result = verify_wheel_url(
        Requirement(
            "foo @ https://example.com/foo-1.0-py3-none-any.whl ; "
            "platform_system == 'Windows'"
        ),
        LINUX_TAGS,
        environment={"platform_system": "Linux"},
    )
    assert result.status == STATUS_SKIPPED_MARKER
    assert not result.is_valid_wheel
    assert result.wheel_name is None
    assert "platform_system" in result.reason


def test_enabled_marker_proceeds_to_validation() -> None:
    result = verify_wheel_url(
        Requirement(
            "foo @ https://example.com/foo-1.0-py3-none-any.whl ; "
            "platform_system == 'Linux'"
        ),
        LINUX_TAGS,
        environment={"platform_system": "Linux"},
    )
    assert result.status == STATUS_VALID


# --- canonical names and build tags use real packaging rules -------------


@pytest.mark.parametrize(
    "requirement_name",
    ["Foo.Bar", "foo-bar", "foo_bar", "FOO_BAR"],
)
def test_requirement_name_is_canonicalized(requirement_name: str) -> None:
    result = verify_wheel_url(
        f"{requirement_name} @ https://example.com/foo_bar-1.0-py3-none-any.whl",
        LINUX_TAGS,
    )
    assert result.requirement == "foo-bar"
    assert result.wheel_name == "foo-bar"
    assert result.status == STATUS_VALID


def test_build_tag_parsed_by_real_rules() -> None:
    result = verify_wheel_url(
        "foo @ https://example.com/foo-1.0-1abc-py3-none-any.whl",
        LINUX_TAGS,
    )
    assert result.status == STATUS_VALID
    assert result.build == (1, "abc")


def test_invalid_build_tag_is_unknown() -> None:
    # Build tag must start with a digit per PEP 427.
    result = verify_wheel_url(
        "foo @ https://example.com/foo-1.0-abc-py3-none-any.whl",
        LINUX_TAGS,
    )
    assert result.status == STATUS_UNKNOWN


def test_compressed_tag_set_expands() -> None:
    result = verify_wheel_url(
        "foo @ https://example.com/foo-1.0-cp312-abi3-manylinux_2_17_x86_64.whl",
        LINUX_TAGS,
    )
    assert result.status == STATUS_VALID
    assert Tag("cp312", "abi3", "manylinux_2_17_x86_64") in result.wheel_tags


def test_file_url_supported_offline() -> None:
    result = verify_wheel_url(
        "foo @ file:///srv/wheels/foo-2.0-py3-none-any.whl", LINUX_TAGS
    )
    assert result.status == STATUS_VALID
    assert result.version == "2.0"
    assert canonicalize_version(result.version) == "2"  # PEP 625 trailing zero


def test_target_tags_generator_is_safe() -> None:
    def gen() -> object:
        yield Tag("py3", "none", "any")

    result = verify_wheel_url(
        Requirement(DEPENDENCY_EXAMPLES[0]),
        gen(),  # type: ignore[arg-type]
    )
    assert result.status == STATUS_VALID
