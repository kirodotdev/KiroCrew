"""``KEBAB_RE`` only carries its grammar under ``fullmatch``.

The pattern ends in ``$``, which in Python matches at the true end of the string
**and** immediately before a trailing newline. So ``KEBAB_RE.match("alerts\\n")``
succeeds, and every gate written as ``if not KEBAB_RE.match(value)`` admits a
value with a trailing newline while reporting it as valid kebab-case.

Three gates were written that way, and each hands the value straight on:

- an app's notification channel id, joined into ``"<app>.<id>"`` and used as a
  subscription key;
- the cached external-registry entry name;
- the fetched external-registry entry name.

``install_receipt._valid_slug`` already used ``fullmatch`` and is the
reference for the shape.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.install_receipt import _valid_slug
from kiro_crew.apps.manifest import KEBAB_RE, NotificationsConfig

#: Values whose only defect is a trailing newline: `$` lets them through `match`.
NEWLINE_VALUES = ["alerts\n", "build-done\n", "a\n"]


def test_the_pattern_itself_is_why_fullmatch_is_required():
    """Source of truth for the whole file. If this ever fails because the shared
    pattern was re-anchored with ``\\Z``, the gates below are belt-and-braces and
    this file can be simplified."""
    assert KEBAB_RE.match("alerts\n"), "match still accepts a trailing newline"
    assert not KEBAB_RE.fullmatch("alerts\n"), "fullmatch is what rejects it"
    assert KEBAB_RE.fullmatch("alerts"), "an ordinary id is unaffected"


class TestNotificationChannelId:
    @pytest.mark.parametrize("bad_id", NEWLINE_VALUES)
    def test_a_trailing_newline_is_not_kebab_case(self, bad_id):
        cfg = NotificationsConfig.from_dict(
            {"channels": [{"id": bad_id, "name": "Alerts", "label": "Alerts"}]}
        )
        errors = cfg.validate()
        assert any("kebab-case" in e for e in errors), (bad_id, errors)

    def test_an_ordinary_channel_id_still_validates(self):
        cfg = NotificationsConfig.from_dict(
            {"channels": [{"id": "build-done", "name": "Build", "label": "Build done"}]}
        )
        assert cfg.validate() == []

    def test_a_genuinely_non_kebab_id_is_still_rejected(self):
        """Preservation: the existing contract is unchanged for everything that
        was already refused."""
        cfg = NotificationsConfig.from_dict(
            {"channels": [{"id": "Not_Kebab", "name": "X", "label": "X"}]}
        )
        assert any("kebab-case" in e for e in cfg.validate())


class TestRegistryEntryNames:
    """The registry gates drop an entry whose name is not kebab-case, and their
    own comment says why it matters: the name flows to ``app_source_dir(name)``
    and a failed clone calls ``shutil.rmtree`` on that path. A trailing newline
    let the name past the filter that exists to stop exactly that.
    """

    def _entry(self, name):
        return {"name": name, "version": "1.0.0", "displayName": "X", "description": "d"}

    @pytest.mark.parametrize("bad_name", ["evil-app\n", "demo\n"])
    def test_a_cached_entry_with_a_trailing_newline_is_dropped(
        self, bad_name, tmp_path, monkeypatch
    ):
        from kiro_crew.apps import registry

        monkeypatch.setattr(registry, "_manifest_cache_dir", lambda: tmp_path)
        registry._write_external_registry_cache("acme", [self._entry(bad_name)])

        kept = registry._read_external_registry_cache("acme", ignore_ttl=True) or []

        assert [e["name"] for e in kept] == [], f"{bad_name!r} survived the name gate"

    def test_an_ordinary_cached_entry_is_kept(self, tmp_path, monkeypatch):
        """Preservation: the gate refuses a malformed name, not entries."""
        from kiro_crew.apps import registry

        monkeypatch.setattr(registry, "_manifest_cache_dir", lambda: tmp_path)
        registry._write_external_registry_cache("acme", [self._entry("good-app")])

        kept = registry._read_external_registry_cache("acme", ignore_ttl=True) or []

        assert [e["name"] for e in kept] == ["good-app"]

    def test_neither_registry_gate_uses_bare_match(self):
        """Source guard for the fetch path.

        The fetch gate lives inside an async function that performs network I/O,
        so it is not reachable from a unit test without mocking the transport —
        but it is the same one-line check as the cache gate, and the two must not
        drift apart. Pins both instead of testing one and hoping.
        """
        import inspect

        from kiro_crew.apps import registry

        src = inspect.getsource(registry)
        assert "KEBAB_RE.match(" not in src, "a registry name gate still uses bare match()"
        assert src.count("KEBAB_RE.fullmatch(") == 2, "expected exactly two registry name gates"


def test_install_receipt_slug_was_already_correct():
    """The reference implementation, pinned so the three gates above now agree
    with it rather than each carrying their own answer."""
    assert not _valid_slug("demo\n")
    assert _valid_slug("demo")
