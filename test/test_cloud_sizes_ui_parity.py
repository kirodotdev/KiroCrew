"""The dashboard's size cards must agree with ``cloud/sizes.py``.

``RemoteCrewPanel.tsx`` renders the size picker from its own literal ``SIZE_TIERS`` /
``X86_TIERS`` tables while the gateway actually launches from ``cloud/sizes.py``. That
is two sources of truth for the same facts, and the re-ladder that introduced these
tiers is itself proof they move: a shape edited on one side only would show the user
"32 GB · 8 vCPU" while provisioning something else entirely, with no test failing.

Serving the catalog over HTTP would remove the duplication outright, but it adds a
runtime surface for data that is compile-time constant. A parity gate is the cheaper
equivalent and matches how this repo already guards its i18n catalogs: the duplication
stays, drifting becomes unmergeable.

Only the *facts* are compared. The copy (tier names, the sub-agent headline) is
deliberately frontend-owned, because it is translated.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew.cloud import sizes

PANEL = (
    Path(__file__).resolve().parent.parent
    / "website"
    / "src"
    / "pages"
    / "settings"
    / "RemoteCrewPanel.tsx"
)

# One object literal per tier, e.g.
#   { key: 'light', family: 'light', arch: 'arm64', instanceType: 't4g.xlarge',
#     vcpu: 4, ramGb: 16, diskGb: 40, subagents: 3 },
_ROW = re.compile(
    r"\{\s*key:\s*'(?P<key>[a-z0-9-]+)'.*?"
    r"arch:\s*'(?P<arch>[a-z0-9_]+)'.*?"
    r"instanceType:\s*'(?P<instance_type>[a-z0-9.]+)'.*?"
    r"vcpu:\s*(?P<vcpu>\d+).*?"
    r"ramGb:\s*(?P<ram_gb>\d+).*?"
    r"diskGb:\s*(?P<disk_gb>\d+)",
    re.DOTALL,
)


def _panel_tiers() -> dict[str, dict[str, object]]:
    text = PANEL.read_text(encoding="utf-8")
    found: dict[str, dict[str, object]] = {}
    for m in _ROW.finditer(text):
        d = m.groupdict()
        found[d["key"]] = {
            "arch": d["arch"],
            "instance_type": d["instance_type"],
            "vcpu": int(d["vcpu"]),
            "ram_gb": int(d["ram_gb"]),
            "disk_gb": int(d["disk_gb"]),
        }
    return found


def test_the_panel_tables_were_parsed() -> None:
    """Guard the guard: a shape change that defeats the regex must not pass silently."""
    parsed = _panel_tiers()
    assert parsed, f"parsed no size rows from {PANEL.name} — the literal shape changed"
    # Both lanes are present, so a dropped table cannot look like agreement.
    assert len(parsed) == len(sizes.TIERS_BY_KEY), (
        f"{PANEL.name} declares {sorted(parsed)}; sizes.py declares "
        f"{sorted(sizes.TIERS_BY_KEY)} — one side added or removed a tier"
    )


@pytest.mark.parametrize("key", sorted(sizes.TIERS_BY_KEY))
def test_panel_tier_matches_sizes_py(key: str) -> None:
    parsed = _panel_tiers()
    assert key in parsed, f"{key} is launchable but has no card in {PANEL.name}"
    spec = sizes.TIERS_BY_KEY[key]
    ui = parsed[key]
    assert ui["instance_type"] == spec.instance_type, f"{key}: instance type drifted"
    assert ui["vcpu"] == spec.vcpu, f"{key}: vCPU drifted"
    assert ui["ram_gb"] == spec.ram_gb, f"{key}: RAM drifted"
    assert ui["disk_gb"] == spec.disk_gb, f"{key}: disk drifted"
    assert ui["arch"] == spec.arch, f"{key}: architecture drifted"
