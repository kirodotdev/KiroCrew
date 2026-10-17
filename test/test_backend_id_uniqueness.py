"""Every backend id and every model-registry namespace names exactly one harness.

A backend id is a plain string, and the vocabulary in
:mod:`kiro_crew.agent_sdk.backends` is built from sets and dict keys. Two
``ACP_BACKEND_*`` constants spelled with the same string therefore collapse into
one member of ``ACP_BACKENDS_KNOWN`` and one key of every per-backend table with
no error at import, and whichever entry Python reads last silently decides what
that id launches, how it is labelled and which governance rule names it. The same
holds one level down for ``_MODEL_REGISTRY_NAMESPACE_BY_BACKEND``: two harnesses
on one namespace share one advertised-model cache bucket, so either can overwrite
the model list the picker offers for the other.

These tests turn both collapses into a CI failure. The one deliberate share is
the kiro family: kiro-cli and KAS serve one model vocabulary, so they read one
``acp`` namespace and share pins. A new harness takes a fresh id and a fresh
namespace (docs/system-specs/modules/harness-onboarding.md, Stage 1), and a
shipped id is never re-pointed at a different implementation, because saved
``agent.acp_backend`` values and governance rules already name it.

The checks are helpers over plain mappings, so the probe tests below can plant a
duplicate and assert each helper flags it -- a helper that stops detecting the
collapse fails here rather than passing every real check vacuously.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, Mapping, Set

from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.backends import (
    _MODEL_REGISTRY_NAMESPACE_BY_BACKEND,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_KNOWN,
)
from kiro_crew.agent_sdk.capabilities import MODEL_NAMESPACE_ACP

# The id-constant prefix. ``ACP_BACKENDS_*`` (the capability sets) does not
# match it, and the ``ACP_BACKEND_*`` per-backend tables are dicts, so the
# string filter in ``_id_constants`` leaves only the ids.
_ID_PREFIX = "ACP_BACKEND_"

# The only namespace more than one backend may read, and exactly who reads it.
_KIRO_FAMILY_SHARE: Dict[str, Set[str]] = {
    MODEL_NAMESPACE_ACP: {ACP_BACKEND_KIRO, ACP_BACKEND_KAS},
}


def _id_constants(namespace: Mapping[str, object]) -> Dict[str, str]:
    """Return every module-level ``ACP_BACKEND_<NAME>`` string constant."""
    return {
        name: value
        for name, value in namespace.items()
        if name.startswith(_ID_PREFIX) and isinstance(value, str)
    }


def _names_by_value(pairs: Iterable[tuple]) -> Dict[str, Set[str]]:
    """Group the left side of each pair under its right side."""
    grouped: Dict[str, Set[str]] = defaultdict(set)
    for left, right in pairs:
        grouped[right].add(left)
    return dict(grouped)


def _duplicated_ids(constants: Mapping[str, str]) -> Dict[str, Set[str]]:
    """Map each id spelled by more than one constant to the constants spelling it."""
    return {
        value: names
        for value, names in _names_by_value(constants.items()).items()
        if len(names) > 1
    }


def _shared_namespaces(table: Mapping[str, str]) -> Dict[str, Set[str]]:
    """Map each namespace read by more than one backend to the backends reading it."""
    return {
        namespace: backends
        for namespace, backends in _names_by_value(table.items()).items()
        if len(backends) > 1
    }


class TestBackendIdsAreUnique:
    def test_every_id_constant_spells_a_distinct_id(self) -> None:
        duplicated = _duplicated_ids(_id_constants(vars(sdk_backends)))
        assert duplicated == {}, (
            "two ACP_BACKEND_* constants spell the same id, so they collapse into one "
            "member of every backend set and one key of every per-backend table; "
            f"give the newer harness a fresh id: {duplicated}"
        )

    def test_the_id_constants_are_exactly_the_known_ids(self) -> None:
        """Pins the collector as well as the vocabulary.

        An id with no constant, or a constant outside the closed set, both mean the
        uniqueness check above is reading a different vocabulary than the one
        provider construction enforces.
        """
        constants = _id_constants(vars(sdk_backends))
        assert set(constants.values()) == set(ACP_BACKENDS_KNOWN)


class TestModelNamespacesAreDistinct:
    def test_the_namespace_table_names_only_known_backends(self) -> None:
        assert set(_MODEL_REGISTRY_NAMESPACE_BY_BACKEND) <= set(ACP_BACKENDS_KNOWN)

    def test_every_known_backend_names_its_namespace_explicitly(self) -> None:
        """An unmapped id falls back to ``acp`` and joins the kiro family silently."""
        missing = set(ACP_BACKENDS_KNOWN) - set(_MODEL_REGISTRY_NAMESPACE_BY_BACKEND)
        assert missing == set(), f"known backends with no namespace entry: {missing}"

    def test_only_the_kiro_family_shares_a_namespace(self) -> None:
        shared = _shared_namespaces(_MODEL_REGISTRY_NAMESPACE_BY_BACKEND)
        assert shared == _KIRO_FAMILY_SHARE, (
            "a model-registry namespace is read by more than one backend outside the "
            "kiro family, so one harness can overwrite the model list the picker "
            f"offers for another; give the newer harness its own namespace: {shared}"
        )


class TestTheChecksDetectACollapse:
    """Plant each collapse into a copy of the real vocabulary and expect a flag."""

    def test_a_second_constant_with_a_shipped_id_is_flagged(self) -> None:
        planted = dict(vars(sdk_backends))
        planted["ACP_BACKEND_PLANTED_DIRECT"] = sdk_backends.ACP_BACKEND_DEEPSEEK
        duplicated = _duplicated_ids(_id_constants(planted))
        assert {"ACP_BACKEND_DEEPSEEK", "ACP_BACKEND_PLANTED_DIRECT"} <= duplicated.get(
            sdk_backends.ACP_BACKEND_DEEPSEEK, set()
        )

    def test_a_non_string_backend_table_is_not_read_as_an_id(self) -> None:
        planted = {"ACP_BACKEND_PLANTED_TABLE": {"": "x"}, "ACP_BACKEND_PLANTED": "planted"}
        assert _id_constants(planted) == {"ACP_BACKEND_PLANTED": "planted"}

    def test_a_capability_set_is_not_read_as_an_id(self) -> None:
        planted = {"ACP_BACKENDS_PLANTED": "planted"}
        assert _id_constants(planted) == {}

    def test_a_second_backend_on_a_harness_namespace_is_flagged(self) -> None:
        planted = dict(_MODEL_REGISTRY_NAMESPACE_BY_BACKEND)
        harness_namespace = planted[sdk_backends.ACP_BACKEND_DEEPSEEK]
        planted["planted-direct"] = harness_namespace
        shared = _shared_namespaces(planted)
        assert shared != _KIRO_FAMILY_SHARE
        assert {sdk_backends.ACP_BACKEND_DEEPSEEK, "planted-direct"} <= shared.get(
            harness_namespace, set()
        )

    def test_a_third_backend_on_the_kiro_namespace_is_flagged(self) -> None:
        planted = dict(_MODEL_REGISTRY_NAMESPACE_BY_BACKEND)
        planted["planted-family"] = MODEL_NAMESPACE_ACP
        assert _shared_namespaces(planted) != _KIRO_FAMILY_SHARE
