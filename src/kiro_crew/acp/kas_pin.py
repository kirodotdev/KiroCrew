"""The one KAS engine build this Kiro Crew release runs on the bundle route.

Each Kiro Crew version pins exactly one engine version, and the pin is the
sha256 of that version's published ``manifest.json``. The manifest in turn pins
every artifact (engine tarball and Node build, per platform) by sha256 and size,
so this one digest fixes every byte the installer can lay down: a republished or
tampered manifest is refused before any artifact is fetched.

Why a pin rather than ``latest.json``: the engine speaks a wire protocol Kiro
Crew adapts to (``acp/kas_wire.py``, ``acp/harness/kas.py``), and an engine
newer than the adapter was tested against is a silent behavior change on the
operator's next install. Pinning makes the engine version a property of the Kiro
Crew version, the same contract ``packaging/kiro-cli-version`` holds for a
``BUNDLE_KIRO_CLI=1`` desktop build: an update of Kiro Crew is what moves the engine, and a
rollback of Kiro Crew moves it back (each version is its own directory under
``engines/kas/``, so both stay installed).

Bumping: publish with ``scripts/kas_bundle_publish.py``, test against it, then
change BOTH constants below in one commit. The publish script prints the
digest. ``test/test_kas_pin.py`` refuses a pin whose version or digest is not
well-formed, and the installer refuses a manifest whose bytes do not hash to it.

``KIROCREW_KAS_VERSION`` overrides the version the harness looks for (a
developer testing another engine installed with ``kirocrew kas install
--version X``). It is read from the gateway's own environment, never from
config, which is agent-readable and not where a code-selection switch belongs.

The pin also carries what the engine is KNOWN to accept. On the bundle route the
engine reads the agent specs Kiro Crew writes, so the spec ``permissions`` block
(Crew's auto-approve list in KAS's own currency) is gated on the engine's
version here, by :func:`engine_spec_permissions_supported`, the way the relay
route gates it on kiro-cli's (``kiro_cli.spec_permissions_supported``).
"""

from __future__ import annotations

import os

__all__ = [
    "KAS_PIN_MANIFEST_SHA256",
    "KAS_PIN_VERSION",
    "KAS_SPEC_PERMISSIONS_MIN_ENGINE_VERSION",
    "KAS_VERSION_OVERRIDE_ENV",
    "engine_spec_permissions_supported",
    "pinned_version",
]

#: Engine version this release runs.
KAS_PIN_VERSION = "0.66.33"

#: sha256 of ``<base>/kas/<KAS_PIN_VERSION>/manifest.json`` as published.
KAS_PIN_MANIFEST_SHA256 = "9cbd8dd91092b056ef61893614f6e4deec8b5694a3f6dcfcf9dddc2f659f604a"

#: Operator/developer override of the version the harness resolves.
KAS_VERSION_OVERRIDE_ENV = "KIROCREW_KAS_VERSION"

#: Lowest engine release verified to accept a spec ``permissions`` block. The
#: pinned engine is the one release the block was measured against, so the floor
#: is that release: an engine the override points at that is older is not
#: "probably fine", it is unmeasured, and the gate withholds the block there.
#: Lowering it needs the block confirmed on that older engine; the pin bump test
#: keeps it at or below ``KAS_PIN_VERSION`` so the stock install always passes.
KAS_SPEC_PERMISSIONS_MIN_ENGINE_VERSION: tuple[int, int, int] = (0, 66, 33)


def pinned_version() -> str:
    """The engine version to run: the override when set, else the pin."""
    override = (os.environ.get(KAS_VERSION_OVERRIDE_ENV) or "").strip()
    return override or KAS_PIN_VERSION


def engine_spec_permissions_supported(version: str | None) -> bool:
    """Pure gate: does a bundled engine at *version* accept a spec ``permissions`` block?

    The engine's counterpart to ``kiro_cli.spec_permissions_supported``, with the
    same asymmetry: an unknown version (no engine installed, or a version string
    the pin cannot read) is False, because writing the field on an engine that
    refuses it costs the whole spec and withholding it costs only the KAS
    auto-approve listing. The version is read by the shared
    ``parse_kiro_cli_version``, so a pre-release or build suffix is dropped.
    """
    from kiro_crew.mcp_hot_reload import parse_kiro_cli_version  # noqa: PLC0415

    parsed = parse_kiro_cli_version(version) if version else None
    return parsed is not None and parsed >= KAS_SPEC_PERMISSIONS_MIN_ENGINE_VERSION
