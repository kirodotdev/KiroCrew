"""The packaged Remote Crew page's code-coupled claims are pinned to the code.

``scripts/docs_lint.py`` gates a doc's paths and links, never what it claims, so
``src/kiro_crew/docs/remote-crew.md`` is free to quote a config key that was
renamed, a default that moved, or a diagnosis code that was retired, with the lint
green. The claims most exposed to that are the ones a reader ACTS on — the nine
``instances.*`` keys the Tuning table tells them to set, the numbers beside them,
the transport names, the two Feature Previews toggles the peer-session sections
send them to, and the diagnosis codes the troubleshooting table tells them to read
off the Diagnose button.

Each assertion is paired with the row in the page that states it, and asserted
against the code rather than against a second copy of the prose: a default is
imported from the constants module that owns it, and a name is read from the
declaration that defines it.

Prose claims are matched against ``doc_flat``, the page with its whitespace
collapsed, so that re-wrapping a paragraph is never a test failure — the claim is
the words, not the column the line breaks at.
"""

import dataclasses
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "src" / "kiro_crew" / "docs" / "remote-crew.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def doc_flat(doc_text: str) -> str:
    """The page as one whitespace-normalized string, for prose assertions."""
    return re.sub(r"\s+", " ", doc_text)


def test_every_config_key_the_tuning_table_names_is_still_on_the_record(doc_text: str) -> None:
    """§Tuning tells the reader to `kirocrew config set` each of these."""
    from kiro_crew.config.sections import InstancesConfig

    names = {f.name for f in dataclasses.fields(InstancesConfig)}
    for key in (
        "enabled",
        "warm_set_cap",
        "tunnel_base_port",
        "ssh_compression",
        "connect_timeout_secs",
        "mint_timeout_secs",
        "max_recovery_attempts",
        "recover_backoff_max_secs",
        "probe_failure_threshold",
    ):
        assert key in names, f"the page's Tuning table names instances.{key}, which was dropped"
        assert f"`instances.{key}`" in doc_text, f"the page no longer names instances.{key}"


def test_the_opt_in_is_still_off_by_default(doc_flat: str) -> None:
    """The page opens by saying the feature is off until you turn it on."""
    from kiro_crew.config.sections import InstancesConfig

    assert InstancesConfig().enabled is False, "the page says Remote Crew is opt-in"
    assert "off by default" in doc_flat


def test_the_defaults_the_tuning_table_quotes_are_the_real_ones(doc_flat: str) -> None:
    """Each number in §Tuning is the constant the runtime resolves.

    Asserted as a KEY-AND-DEFAULT PAIR, on the table row itself, rather than as a
    bare occurrence of the number anywhere in the page. Two rows whose defaults
    were swapped for each other would satisfy a presence check — both numbers are
    still somewhere in the document — while telling the reader to expect 8 failed
    probes and 3 recovery attempts. The pairing is the whole point of the pin.
    """
    from kiro_crew.instances import constants as c

    rows = {
        "instances.warm_set_cap": f"`{c.DEFAULT_WARM_SET_CAP}`",
        "instances.tunnel_base_port": f"`{c.DEFAULT_TUNNEL_BASE_PORT}`",
        "instances.ssh_compression": f"`{str(c.DEFAULT_SSH_COMPRESSION).lower()}`",
        "instances.connect_timeout_secs": (
            f"SSH {int(c.DEFAULT_CONNECT_TIMEOUT_SECS)}s, "
            f"SSM {int(c.DEFAULT_SSM_CONNECT_TIMEOUT_SECS)}s"
        ),
        "instances.mint_timeout_secs": (
            f"SSH {int(c.DEFAULT_MINT_TIMEOUT_SECS)}s, "
            f"SSM {int(c.DEFAULT_SSM_MINT_TIMEOUT_SECS)}s"
        ),
        "instances.max_recovery_attempts": f"`{c.DEFAULT_MAX_RECOVERY_ATTEMPTS}`",
        "instances.recover_backoff_max_secs": f"`{c.DEFAULT_RECOVER_BACKOFF_MAX_SECS}`",
        "instances.probe_failure_threshold": f"`{c.DEFAULT_PROBE_FAILURE_THRESHOLD}`",
    }
    for key, default in rows.items():
        assert f"| `{key}` | {default} |" in doc_flat, (
            f"§Tuning's row for {key} no longer reads its default as {default} — the "
            "page and the constant have drifted, or two rows' defaults were swapped"
        )


def test_the_two_keys_the_page_says_need_a_restart_are_the_two_marked_restart(
    doc_flat: str,
) -> None:
    """§Tuning closes by naming exactly which keys a restart is required for.

    Read off the config metadata rather than a doc-side list, so a key that gains
    or loses ``restart=True`` fails here instead of quietly making the page wrong.
    """
    import dataclasses

    from kiro_crew.config.sections import InstancesConfig

    needs_restart = {
        f.name
        for f in dataclasses.fields(InstancesConfig)
        if f.metadata.get("restart") or f.metadata.get("requires_restart")
    }
    assert needs_restart == {"enabled", "tunnel_base_port"}, (
        "the page names exactly these two keys as needing a restart; the set of "
        f"restart-marked instances.* fields is now {sorted(needs_restart)}"
    )
    assert "`instances.enabled` and `instances.tunnel_base_port` both need a restart" in doc_flat


def test_the_record_defaults_the_add_table_quotes_are_the_real_ones(doc_flat: str) -> None:
    """§Adding an instance states the remote port and the token TTL as defaults."""
    from kiro_crew.instances.registry import Instance

    blank = Instance(id="probe", name="probe")
    assert f"`{blank.remote_port}` by default" in doc_flat
    assert f"`{blank.ttl}` by default" in doc_flat


def test_the_three_transports_are_still_the_three_the_page_describes(doc_flat: str) -> None:
    """§SSH or SSM describes two by hand plus Fargate as the one set for you."""
    from kiro_crew.instances.registry import CONNECTION_METHODS

    assert CONNECTION_METHODS == (
        "ssh",
        "ssm",
        "fargate",
    ), "the page describes exactly these three connection methods; the registry's set moved"
    assert "**SSH** (default)" in doc_flat and "**AWS SSM**" in doc_flat
    assert "**Fargate**" in doc_flat


def test_the_three_ssm_actions_the_transport_table_names_are_the_ones_the_mint_needs(
    doc_flat: str,
) -> None:
    """§SSH or SSM tells the reader which IAM actions an SSM crew needs.

    All three, not just the one that opens the tunnel: the token mint runs over
    ``send-command``, so a policy carrying only ``ssm:StartSession`` brings the
    forward up and then fails the mint. Pinned against the launcher's own
    generated policy, which is where the real action set lives.
    """
    iam = (ROOT / "src" / "kiro_crew" / "cloud" / "iam.py").read_text(encoding="utf-8")
    for action in ("ssm:StartSession", "ssm:SendCommand", "ssm:GetCommandInvocation"):
        assert (
            f'"{action}"' in iam
        ), f"the page tells the reader to grant {action}, which the generated policy dropped"
        assert f"`{action}`" in doc_flat, f"the page no longer names {action}"


def test_the_diagnosis_codes_the_troubleshooting_table_quotes_still_exist(doc_flat: str) -> None:
    """§When something is wrong tells the reader to read these off Diagnose."""
    diagnostics = (ROOT / "src" / "kiro_crew" / "instances" / "diagnostics.py").read_text(
        encoding="utf-8"
    )
    for code in ("ssh_unreachable", "ssm_unreachable", "remote_down", "tunnel_down"):
        assert f'"{code}"' in diagnostics, f"the page quotes the diagnosis code {code}"
        assert f"`{code}`" in doc_flat


def test_the_registry_file_the_page_names_is_where_the_code_writes_it(doc_flat: str) -> None:
    """The page tells the reader coordinates live in instances.json, credentials never."""
    registry = (ROOT / "src" / "kiro_crew" / "instances" / "registry.py").read_text(
        encoding="utf-8"
    )
    assert '"instances.json"' in registry
    assert "`~/.kiro/crew/instances.json`" in doc_flat


def test_the_two_feature_previews_the_page_sends_users_to_still_exist(doc_flat: str) -> None:
    """Both peer-session sections start with a Feature Previews toggle, named."""
    flags = (ROOT / "website" / "src" / "utils" / "previewFlags.ts").read_text(encoding="utf-8")
    for flag in ("PREVIEW_REMOTE_CREW_CHAT", "PREVIEW_INSTANCE_SESSIONS"):
        assert f"export const {flag}" in flags, f"the page's preview toggle {flag} was removed"

    panel = (
        ROOT / "website" / "src" / "pages" / "settings" / "FeaturePreviewsSection.tsx"
    ).read_text(encoding="utf-8")
    for key in ("chat_on_a_crew", "remote_instance_sessions"):
        assert (
            f"featurePreviewsTab.{key}'" in panel
        ), f"the page names the {key} card, which Feature Previews no longer renders"

    labels = (ROOT / "website" / "src" / "i18n" / "locales" / "en.json").read_text(encoding="utf-8")
    assert '"remote_instance_sessions": "Remote crew sessions"' in labels
    assert "Feature Previews → Remote crew sessions" in doc_flat

    manual = (ROOT / "website" / "src" / "i18n" / "locales" / "en.manual.json").read_text(
        encoding="utf-8"
    )
    assert '"chat_on_a_crew": "Chat on a crew"' in manual
    assert "Feature Previews → Chat on a crew" in doc_flat


def test_a_remote_bound_session_still_refuses_a_turn_while_its_tunnel_is_down(
    doc_flat: str,
) -> None:
    """§A session that runs on another machine promises a refusal, not a silent failure."""
    handlers = (ROOT / "src" / "kiro_crew" / "dashboard" / "chat_handlers.py").read_text(
        encoding="utf-8"
    )
    assert (
        "reconnecting to the crew running this session" in handlers
    ), "the page says a remote session refuses a turn and says it is reconnecting"
    assert "says it is reconnecting" in doc_flat
