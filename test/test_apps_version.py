"""Tests for kiro_crew.apps.version — parsing and the min-version install gate.

Regression anchor: every insider wheel is stamped with a PEP 440 version like
``0.4.0rc3`` (no separator before ``rc``). The old ``parse_version`` raised
``ValueError`` on that shape, and ``check_min_version`` swallows exceptions, so
the min-version install gate was silently DISABLED on every insider install --
caught by the v0.4.0-insider.3 release-candidate gate
(test_min_version_gate_rejects_before_copying: 201 instead of 400).
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.version import check_min_version, parse_version


class TestParseVersion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.2.3", (1, 2, 3)),
            ("1.0", (1, 0, 0)),  # pads to 3 elements
            ("1", (1, 0, 0)),
            ("v1.2.3", (1, 2, 3)),  # tolerates leading v
            ("1.2.0-rc.1", (1, 2, 0)),  # semver pre-release
            ("1.2.3+build.42", (1, 2, 3)),  # build metadata
            ("0.4.0-insider.3", (0, 4, 0)),  # desktop stamp
            ("0.4.0rc3", (0, 4, 0)),  # PEP 440 wheel stamp -- the regression
            ("0.4.0a1", (0, 4, 0)),
            ("0.4.0.dev5", (0, 4, 0)),  # extra numeric parts beyond 3 are ignored
            ("1.2.3.4", (1, 2, 3)),
        ],
    )
    def test_parses_release_segment(self, raw: str, expected: tuple[int, ...]) -> None:
        assert parse_version(raw) == expected

    @pytest.mark.parametrize("raw", ["", "garbage", "rc3", "-1.2.3"])
    def test_unparseable_raises(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_version(raw)


class TestCheckMinVersion:
    def test_gate_rejects_on_insider_rc_stamp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The exact RC-gate scenario: running version is a PEP 440 rc stamp.
        monkeypatch.setattr("kiro_crew.__version__", "0.4.0rc3")
        err = check_min_version("999.0.0")
        assert err is not None
        assert "999.0.0" in err

    def test_gate_accepts_on_insider_rc_stamp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.__version__", "0.4.0rc3")
        assert check_min_version("0.4.0") is None

    def test_gate_rejects_on_bare_stamp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.__version__", "0.4.0")
        assert check_min_version("999.0.0") is not None

    def test_empty_min_version_passes(self) -> None:
        assert check_min_version("") is None

    @pytest.mark.parametrize("raw", [["1.0.0"], {"min": "1.0.0"}, True])
    def test_a_non_string_min_version_passes_instead_of_raising(self, raw) -> None:
        assert check_min_version(raw) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize(("raw", "refused"), [(999, True), (1.5, False), (0, False)])
    def test_a_bare_number_is_read_as_its_string(
        self, monkeypatch: pytest.MonkeyPatch, raw, refused
    ) -> None:
        # The install itself reads the manifest value through str(), so the
        # preflight must refuse the same apps instead of raising or passing them.
        monkeypatch.setattr("kiro_crew.__version__", "1.6.0")
        assert (check_min_version(raw) is not None) is refused  # type: ignore[arg-type]
