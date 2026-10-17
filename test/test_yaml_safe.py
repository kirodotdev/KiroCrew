"""The shared alias-refusing YAML loader, ``kiro_crew.yaml_safe.NoAliasSafeLoader``."""

from __future__ import annotations

import datetime

import pytest
import yaml
from yaml_helpers import load_with

from kiro_crew.agent_spec_format import _FrontmatterLoader
from kiro_crew.yaml_safe import NoAliasSafeLoader


def test_an_alias_is_refused_where_it_appears() -> None:
    with pytest.raises(yaml.composer.ComposerError) as caught:
        load_with(NoAliasSafeLoader, "a: &a 1\nb: *a\n")

    assert caught.value.problem == "found alias, which is not allowed"
    assert (caught.value.problem_mark.line, caught.value.problem_mark.column) == (1, 3)


def test_a_lone_anchor_loads_as_its_plain_value() -> None:
    assert load_with(NoAliasSafeLoader, "&a 1") == 1


def test_a_subclass_constructor_does_not_reach_the_base() -> None:
    """The frontmatter loader reads a timestamp as text; the other readers keep the date.

    PyYAML copies the constructor table into the class that registers one, so
    the frontmatter loader's timestamp rule must stay on that subclass rather
    than change what every other reader of the shared base parses.
    """
    assert load_with(_FrontmatterLoader, "d: 2026-10-16\n") == {"d": "2026-10-16"}
    assert load_with(NoAliasSafeLoader, "d: 2026-10-16\n") == {"d": datetime.date(2026, 10, 16)}
