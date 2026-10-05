Wheel URL verification
======================

.. currentmodule:: packaging.wheel_url

Verify a wheel *direct URL* against a PEP 508 requirement and a target
set of platform tags, offline. The URL is never fetched: a URL that does
not carry a recognizable wheel filename can only be reported as
``unknown``, so the version is never guessed by downloading anything.

.. versionadded:: 26.4

Usage
-----

.. code-block:: python

    from packaging.tags import Tag
    from packaging.wheel_url import verify_wheel_url

    target_tags = frozenset({Tag("py3", "none", "any")})

    # A matching pure-Python wheel -> compatible (兼容)
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/files/foo-1.0-py3-none-any.whl",
        target_tags=target_tags,
    )
    assert result.status == "兼容"

    # The wheel filename identity belongs to another package
    # -> package name conflict (包名冲突)
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/files/bar-1.0-py3-none-any.whl",
        target_tags=target_tags,
    )
    assert result.status == "包名冲突"

    # The wheel's platform tags do not intersect the target tag set
    # -> incompatible (不兼容)
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/files/foo-1.0-cp313-cp313-win_amd64.whl",
        target_tags=frozenset({Tag("cp313", "cp313", "manylinux_2_17_x86_64")}),
    )
    assert result.status == "不兼容"

    # The URL has no recognizable wheel filename -> unknown (未知);
    # an unknown result is not a valid wheel and is never compatible
    result = verify_wheel_url(
        "foo==1.0",
        "https://example.com/download",
        target_tags=target_tags,
    )
    assert result.status == "未知"
    assert not result.is_valid_wheel

Only the decoded *path* of the URL is inspected: the query string and the
fragment are never treated as part of the filename, so a wheel name
sitting in a URL fragment (e.g. ``/download#foo-1.0-py3-none-any.whl``) is
not mistaken for the filename. Percent-encoded paths are decoded before
the basename is taken.

Markers are evaluated against the environment and reported separately as
``marker未启用`` rather than being conflated with platform incompatibility:

.. code-block:: python

    result = verify_wheel_url(
        "foo==1.0; python_version < '3'",
        "https://example.com/files/foo-1.0-py3-none-any.whl",
        target_tags=target_tags,
    )
    assert result.status == "marker未启用"

The wheel filename identity (canonical name, version, build tag and tags)
is parsed with :func:`packaging.utils.parse_wheel_filename`, and canonical
names are compared with :func:`packaging.utils.canonicalize_name` -- the
real packaging rules rather than hand-rolled string comparisons.

Reference
---------

.. autofunction:: verify_wheel_url

.. autofunction:: wheel_filename_from_url

.. autoclass:: WheelUrlVerification
    :members:
    :undoc-members:
    :exclude-members: __init__, __new__
