Requirements
============

.. currentmodule:: packaging.requirements

Parse a given requirements line for specifying dependencies of a Python
project, using the :ref:`specification of dependency specifiers
<pypug:dependency-specifiers>`, which defines the scheme that has been
implemented by this module.

Usage
-----

.. doctest::

    >>> from packaging.requirements import Requirement
    >>> simple_req = Requirement("name")
    >>> simple_req
    <Requirement('name')>
    >>> simple_req.name
    'name'
    >>> simple_req.url is None
    True
    >>> simple_req.extras
    set()
    >>> simple_req.specifier
    <SpecifierSet('')>
    >>> simple_req.marker is None
    True
    >>> # Requirements can be specified with extras, specifiers and markers
    >>> req = Requirement('name[foo]>=2,<3; python_version>"2.0"')
    >>> req.name
    'name'
    >>> req.extras
    {'foo'}
    >>> req.specifier
    <SpecifierSet('<3,>=2')>
    >>> req.marker
    <Marker('python_version > "2.0"')>
    >>> # Requirements can also be specified with a URL, but may not specify
    >>> # a version.
    >>> url_req = Requirement('name @ https://github.com/pypa ;os_name=="a"')
    >>> url_req.name
    'name'
    >>> url_req.url
    'https://github.com/pypa'
    >>> url_req.extras
    set()
    >>> url_req.marker
    <Marker('os_name == "a"')>
    >>> # You can do simple comparisons between requirement objects:
    >>> Requirement("packaging") == Requirement("packaging")
    True
    >>> Requirement("my_pkg[foo_bar] == 1.0") == Requirement("my-pkg[foo-bar] == 1.0.0")
    True
    >>> str(Requirement("my_pkg[foo_bar] == 1.0"))
    'my_pkg[foo_bar]==1.0'
    >>> # You can also perform simple comparisons between sets of requirements:
    >>> requirements1 = {Requirement("packaging"), Requirement("pip")}
    >>> requirements2 = {Requirement("pip"), Requirement("packaging")}
    >>> requirements1 == requirements2
    True

Editing extras
--------------

.. versionadded:: 26.4

When a requirement string must be modified rather than just parsed, an
explicit *extras edit plan* adds or removes extras while preserving the
original bytes of everything else (package name, URL, specifier and marker).
The extras section is located by the real parser, so brackets inside a URL
(for example an IPv6 literal) are never mistaken for extras, and the edited
string is re-parsed for validation before it is returned. Extra names are
deduplicated per PEP 685 (case-insensitive; ``-``, ``_`` and ``.`` are
equivalent separators).

.. doctest::

    >>> from packaging.requirements import ExtrasEditPlan
    >>> # The marker text stays byte-identical after adding an extra:
    >>> plan = ExtrasEditPlan('foo[a]; python_version < "3.12"')
    >>> plan.add("b").apply()
    'foo[a,b]; python_version < "3.12"'
    >>> # Brackets in an IPv6 URL are not treated as extras:
    >>> ExtrasEditPlan("foo @ http://[::1]:8080/pkg").add("x").apply()
    'foo[x] @ http://[::1]:8080/pkg'
    >>> # Duplicate extras are merged per PEP 685:
    >>> ExtrasEditPlan("foo[SECURE,secure]").apply()
    'foo[SECURE]'
    >>> # Removals are canonicalized too, and dropping the last extra
    >>> # removes the brackets entirely:
    >>> ExtrasEditPlan("foo[a,b]").remove("A").apply()
    'foo[b]'
    >>> # A plan is bound to its baseline string; applying it to a changed
    >>> # string is rejected:
    >>> plan = ExtrasEditPlan("foo[a]")
    >>> plan.apply("foo[c]")
    Traceback (most recent call last):
    ...
    packaging.requirements.StaleExtrasEditPlanError: requirement string changed since this extras edit plan was created; build a new plan from the current string

The tests for this feature can be run on their own with::

    python -m pytest tests/test_requirements.py -k ExtrasEditPlan

.. versionchanged:: 23.2

    When a requirement is specified with a URL, the :class:`Requirement` class
    used to check the URL and reject values containing invalid scheme and
    netloc combinations. This is no longer performed since the specification does
    not have such rules, and the check incorrectly disallows valid requirement
    strings from being parsed.

Reference
---------

.. class:: Requirement(requirement_string)

    This class abstracts handling the details of a requirement for a project.
    Each requirement will be parsed according to the specification.

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

        Equality and hashing normalize extras and equivalent specifiers. The
        string representation still preserves the parsed name and extras
        spelling.

    :param str requirement_string: The string representation of a requirement.
    :raises InvalidRequirement: If the given ``requirement_string`` is not parseable,
                                then this exception will be raised.

    .. attribute:: name

       The name of the requirement.

    .. attribute:: url

      The URL, if any, where to download the requirement from. Can be None.

    .. attribute:: extras

      A set of extras that the requirement specifies.

    .. attribute:: specifier

      A :class:`~.SpecifierSet` of the version specified by the requirement.

    .. attribute:: marker

      A :class:`~.Marker` of the marker for the requirement. Can be None.

.. exception:: InvalidRequirement

    Raised when attempting to create a :class:`Requirement` with a string that
    does not conform to the specification.

    .. versionadded:: 16.1

.. class:: ExtrasEditPlan(requirement_string)

    An explicit plan to add or remove extras on a requirement string, bound
    to the exact string it was created from. See :ref:`Editing extras` above
    for examples.

    .. versionadded:: 26.4

    :param str requirement_string: The string representation of a requirement.
    :raises InvalidRequirement: If the given ``requirement_string`` is not parseable.

    .. attribute:: baseline

      The requirement string this plan was created from.

    .. attribute:: extras

      A frozenset of the canonical (PEP 685) extra names the edited
      requirement will have.

    .. method:: add(*extras)

      Plan to add each of the given extras. Returns ``self`` for chaining.

    .. method:: remove(*extras)

      Plan to remove each of the given extras. Returns ``self`` for chaining.

    .. method:: apply(requirement_string=None)

      Apply the planned edits and return the edited requirement string. If
      ``requirement_string`` is given it must equal the baseline, otherwise
      :class:`StaleExtrasEditPlanError` is raised. The result is re-parsed
      for validation before being returned.

.. exception:: StaleExtrasEditPlanError

    Raised when an :class:`ExtrasEditPlan` is applied to a requirement string
    that differs from the baseline the plan was created from.

    .. versionadded:: 26.4
