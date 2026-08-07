"""Tests for the diagnostics collector engine + dashboard API handlers.

Focus areas:
  * secrets are scrubbed from every text member before zipping (security-critical)
  * missing sources are skipped, never fatal
  * include_logs=False produces a lighter bundle
  * the pre-filled GitHub issue URL is well-formed
  * the download handler rejects path traversal / non-zip names
  * the collect handler returns a download_url + issue url
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from kiro_crew import beacon, diagnostics
from kiro_crew.dashboard.handlers import diagnostics as dh
from kiro_crew.diagnostics import BundleResult

_GATEWAY = (
    "09:00 boot ok\n"
    "Authorization: Bearer sk-ant-SECRETtoken1234567890abcXYZ\n"
    "Set-Cookie: mc_token_5476=supersecretcookievalueABCDEF123456\n"
    "Set-Cookie: mc_refresh_5476=REFRESHsecretVALUE9876543210\n"
    "aws_secret_access_key=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY\n"
    "a perfectly normal log line\n"
)

_SECRETS = (
    "sk-ant-SECRETtoken1234567890abcXYZ",
    "supersecretcookievalueABCDEF123456",
    "REFRESHsecretVALUE9876543210",
    "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
)


def _isolate(monkeypatch, home: Path) -> None:
    """Point the collector at a temp home and stub host-specific probes."""
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("kiro_crew.diagnostics.config_dir", lambda: home)
    monkeypatch.setattr(diagnostics, "_macos_crash_reports", lambda: [])
    monkeypatch.setattr(diagnostics, "_kiro_cli_chat_log", lambda: None)
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [])
    monkeypatch.setattr(diagnostics, "_kiro_cli_version", lambda: "kiro-cli 2.14.2")


def test_collect_bundle_redacts_and_zips(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text(_GATEWAY)

    r = diagnostics.collect_bundle(note="every message fails", output_dir=tmp_path / "out")

    assert r.zip_path.is_file()
    with zipfile.ZipFile(r.zip_path) as z:
        names = set(z.namelist())
        assert {"versions.txt", "manifest.json", "gateway.log"} <= names
        gw = z.read("gateway.log").decode()
        manifest = json.loads(z.read("manifest.json"))

    for secret in _SECRETS:
        assert secret not in gw, f"secret leaked into bundle: {secret!r}"
    assert "a perfectly normal log line" in gw
    assert r.total_redactions >= 3
    assert r.redaction_summary["gateway.log"] >= 3
    assert manifest["total_redactions"] == r.total_redactions
    assert manifest["note"] == "every message fails"


def test_authorization_scheme_credential_fully_redacted(tmp_path, monkeypatch):
    """A non-Bearer scheme + raw token must be fully redacted (not just the scheme)."""
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text(
        "Authorization: Token abc-123-def-456-ghijklmno\nplain\n"
    )
    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    with zipfile.ZipFile(r.zip_path) as z:
        gw = z.read("gateway.log").decode()
    assert "abc-123-def-456-ghijklmno" not in gw
    assert "[REDACTED]" in gw


def test_authorization_comma_delimited_fully_redacted(tmp_path, monkeypatch):
    """Comma-delimited creds on one Authorization line must ALL be redacted."""
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text(
        "Authorization: OAuth oauth_token=LEAKtok111abc, "
        "oauth_signature=SIGsecret222xyz\nplain\n"
    )
    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    with zipfile.ZipFile(r.zip_path) as z:
        gw = z.read("gateway.log").decode()
    assert "LEAKtok111abc" not in gw
    assert "SIGsecret222xyz" not in gw
    assert "plain" in gw


def test_midline_authorization_header_is_redacted(tmp_path, monkeypatch):
    """A header embedded MID-LINE must be redacted, not just one at line start.

    Log lines and user notes routinely quote a header inside a sentence
    ("request used Authorization: Basic <b64>"). The rule used to be anchored
    with ``^``, so those credentials reached the bundle and the pre-filled
    GitHub issue URL verbatim.
    """
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text(
        "ERROR request failed; request used Authorization: Basic "
        "TWlkTGluZUxFQUsxMjNhYmM=\n"
        "note: the x-api-key: MIDLINEkey456xyz was rejected\nplain\n"
    )
    r = diagnostics.collect_bundle(
        note="repro: sent Authorization: Bearer NOTEleak789tok",
        output_dir=tmp_path / "out",
    )
    with zipfile.ZipFile(r.zip_path) as z:
        gw = z.read("gateway.log").decode()
        versions = z.read("versions.txt").decode()
    assert "TWlkTGluZUxFQUsxMjNhYmM=" not in gw
    assert "MIDLINEkey456xyz" not in gw
    assert "plain" in gw
    assert "NOTEleak789tok" not in versions
    assert "NOTEleak789tok" not in r.github_issue_url


def test_archive_is_opened_in_binary_mode(tmp_path, monkeypatch):
    """The zip fd must carry O_BINARY, or every Windows bundle is corrupt.

    ``os.open`` defaults to TEXT mode on Windows and ``os.fdopen(fd, "wb")``
    cannot change the translation mode of an fd handed to it, so each 0x0A in
    the DEFLATE stream would be written as 0x0D 0x0A and the central-directory
    offsets would no longer match. Asserted by capturing the real flags, so the
    guard is verifiable on POSIX (where ``O_BINARY`` is absent and the expected
    contribution is 0) instead of only on a Windows runner.
    """
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text("hello\n")

    real_open = os.open
    seen: list[int] = []

    def _spy(path, flags, *a, **kw):
        if str(path).endswith(".zip"):
            seen.append(flags)
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", _spy)
    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

    assert seen, "collect_bundle did not open the archive via os.open"
    expected = getattr(os, "O_BINARY", 0)
    assert seen[0] & os.O_CREAT and seen[0] & os.O_WRONLY and seen[0] & os.O_TRUNC
    assert seen[0] & expected == expected, (
        "archive fd is missing O_BINARY — bundles would be corrupt on Windows"
    )
    # And the archive it produced is actually readable back.
    with zipfile.ZipFile(r.zip_path) as z:
        assert "gateway.log" in z.namelist()


def test_tail_truncation_keeps_credentials_attached_to_their_anchor(
    tmp_path, monkeypatch
):
    """A byte-offset tail must not strip a header name off its own credential.

    ``_EXTRA_REDACTIONS`` anchors on the header NAME and redacts to end-of-line,
    so a cut landing *inside* `Authorization: Basic <b64>` would keep the secret
    and drop the token that redacts it. The tail therefore starts on a line
    boundary. Built with a tiny max_bytes so the boundary is exercised without
    writing 4 MiB.
    """
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    monkeypatch.setattr(diagnostics, "_MAX_MEMBER_BYTES", 200)

    secret = "TAILboundaryLEAK123456789abcdefXYZ"
    # Pad so the 200-byte window starts partway through the Authorization line.
    log = (
        "x" * 300
        + "\n"
        + f"Authorization: Basic {secret}\n"
        + "tail marker line\n"
    )
    (home / "gateway.log").write_text(log)

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    with zipfile.ZipFile(r.zip_path) as z:
        gw = z.read("gateway.log").decode()

    assert "tail marker line" in gw, "tail did not include the end of the log"
    assert secret not in gw, "credential survived: tail cut it off its redaction anchor"


def test_chat_log_override_refuses_a_sensitive_path(tmp_path, monkeypatch):
    """`KIRO_CHAT_LOG_FILE` must not be able to aim the collector at a secret store.

    The override names an arbitrary path, and redaction is no backstop here — a
    `.netrc`/`.pem` body does not match the credential patterns — so a sensitive
    target is refused outright rather than scrubbed.
    """
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    netrc = tmp_path / ".netrc"
    netrc.write_text("machine example.com login bob password NETRCsecret9876\n")
    monkeypatch.setenv("KIRO_CHAT_LOG_FILE", str(netrc))

    assert diagnostics._kiro_cli_chat_log() is None

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    with zipfile.ZipFile(r.zip_path) as z:
        blob = b"".join(z.read(n) for n in z.namelist())
    assert b"NETRCsecret9876" not in blob


def test_quoted_serialized_headers_are_redacted(tmp_path, monkeypatch):
    """A JSON/dict-serialized header must redact like a raw one.

    Structured logs write `"authorization": "Basic <b64>"`. The `"` sitting
    between the header name and the `:` defeated a bare `name[ \\t]*[:=]`
    pattern, so the credential reached both the zip and the issue URL.
    """
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text(
        '{"headers": {"authorization": "Basic UVVPVEVEbGVha0FCQzEyMw=="}}\n'
        "{'x-api-key': 'QUOTEDkeyLEAK456xyz'}\n"
        '"set-cookie" : "sid=QUOTEDcookieLEAK789"\n'
        "keep this line\n"
    )
    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    with zipfile.ZipFile(r.zip_path) as z:
        gw = z.read("gateway.log").decode()

    assert "UVVPVEVEbGVha0FCQzEyMw==" not in gw
    assert "QUOTEDkeyLEAK456xyz" not in gw
    assert "QUOTEDcookieLEAK789" not in gw
    assert "keep this line" in gw


def test_symlinked_dump_directory_is_not_enumerated(tmp_path, monkeypatch):
    """A symlinked crash-dumps DIRECTORY must not be walked.

    `Path.is_dir()` follows links, and the files found through the link are not
    themselves symlinks — so the per-file `is_symlink()` guard passes them and
    off-tree content lands in the bundle. The guard has to sit on the directory.
    """
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text("ok\n")

    off_tree = tmp_path / "elsewhere"
    off_tree.mkdir()
    (off_tree / "secrets.txt").write_text("OFFTREEdumpLEAK4242\n")

    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "crash-dumps").symlink_to(off_tree, target_is_directory=True)

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    with zipfile.ZipFile(r.zip_path) as z:
        names = z.namelist()
        blob = b"".join(z.read(n) for n in names)

    assert not any(n.startswith("crash-dumps/") for n in names), names
    assert b"OFFTREEdumpLEAK4242" not in blob


def test_symlinked_source_is_not_followed(tmp_path, monkeypatch):
    """A symlinked source must be skipped, not followed to an off-tree target."""
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    secret = tmp_path / "off_tree_secret.txt"
    secret.write_text("TOPSECRETsymlinkVALUE123")
    (home / "gateway.log").symlink_to(secret)
    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    assert "gateway.log" in r.skipped
    with zipfile.ZipFile(r.zip_path) as z:
        assert "gateway.log" not in z.namelist()
        joined = "".join(z.read(n).decode(errors="replace") for n in z.namelist())
    assert "TOPSECRETsymlinkVALUE123" not in joined


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "POSIX mode bits are not the access-control mechanism on Windows: NTFS "
        "uses ACLs, the 0o600 argument to os.open is ignored, and stat() reports "
        "0o666 for any writable file. The bundle is protected there by the ACL "
        "inherited from the user profile directory instead, which this assertion "
        "cannot express."
    ),
)
def test_archive_is_not_world_readable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text("ok\n")
    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    mode = r.zip_path.stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_missing_sources_are_skipped_not_fatal(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _isolate(monkeypatch, home)  # no gateway.log written

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

    assert r.zip_path.is_file()
    assert "gateway.log" in r.skipped
    assert "versions.txt" in r.included
    assert "manifest.json" in r.included


def test_include_logs_false_excludes_gateway(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text(_GATEWAY)

    r = diagnostics.collect_bundle(include_logs=False, output_dir=tmp_path / "out")

    with zipfile.ZipFile(r.zip_path) as z:
        assert "gateway.log" not in z.namelist()
        assert "versions.txt" in z.namelist()


def test_issue_url_is_well_formed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _isolate(monkeypatch, home)

    r = diagnostics.collect_bundle(note="hi", output_dir=tmp_path / "out")

    assert r.github_issue_url.startswith(
        "https://github.com/kirodotdev/KiroCrew/issues/new?"
    )
    # Routes through the issue FORM, not a free-form body: the form is what
    # carries the version / install / channel answers triage reads.
    assert "template=bug_report.yml" in r.github_issue_url
    assert "body=" not in r.github_issue_url


# ── Release channel -> label, the automatic-triage contract ──────────────────


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        # Desktop / SemVer spelling.
        ("0.1.4", "stable"),
        ("1.2.3", "stable"),
        ("0.1.4-nightly.20260807t061500", "nightly"),
        ("0.1.4-insider.2", "insider"),
        ("0.1.4-rc.1", "insider"),
        # Wheel / PEP 440 spelling — build-wheel.yml rewrites __version__ to
        # this form, so it is what a running CLI install actually reports.
        # Neither of the prerelease shapes contains a `-`; a hyphen-only rule
        # called both "stable" and lost every prerelease CLI bug report.
        ("0.1.4rc4", "insider"),
        ("0.1.4b1", "insider"),
        ("0.1.4a2", "insider"),
        ("0.1.4.dev20260807061500", "nightly"),
    ],
)
def test_channel_covers_both_version_spellings(monkeypatch, version, expected):
    """``_channel`` must answer for desktop SemVer AND wheel PEP 440 stamps.

    The SemVer half mirrors ``auto-update.js`` ``channelForVersion``; the PEP
    440 half exists because ``build-wheel.yml`` rewrites ``__version__`` to the
    wheel version, which spells the same prerelease without a hyphen.
    """
    monkeypatch.setattr(diagnostics, "__version__", version)
    assert diagnostics._channel() == expected


def test_prerelease_wheel_is_never_reported_as_stable(monkeypatch):
    """Regression guard for the bug this change fixes.

    A CLI insider install reports ``1.2.3rc4`` and a CLI nightly reports
    ``1.2.3.dev<stamp>``. Both were classified stable, so their bug reports
    arrived indistinguishable from a supported build's — the opposite of the
    point of having a prerelease channel.
    """
    for version in ("0.1.4rc4", "0.1.4.dev20260807061500"):
        monkeypatch.setattr(diagnostics, "__version__", version)
        assert diagnostics._channel() != "stable", version


def test_channel_never_returns_the_old_prerelease_name(monkeypatch):
    """``"prerelease"`` named a channel no feed, label, or doc uses.

    An insider build used to report it, so its bug reports arrived tagged with
    a lane nobody triages by. Every answer must be a key of the label map.
    """
    for version in ("0.1.4", "0.1.4-insider.1", "0.1.4-nightly.20260807t0615"):
        monkeypatch.setattr(diagnostics, "__version__", version)
        assert diagnostics._channel() in diagnostics._CHANNEL_LABELS


@pytest.mark.parametrize(
    ("version", "label"),
    [
        ("0.1.4-nightly.20260807t061500", "channel: nightly"),
        ("0.1.4-insider.2", "channel: insider"),
        ("0.1.4", "channel: stable"),
    ],
)
def test_issue_url_attaches_the_channel_label(tmp_path, monkeypatch, version, label):
    """The whole point: the report is filterable by channel the moment it lands.

    Without this, a maintainer has to read a version string out of the body to
    know whether an incoming bug is a release blocker (insider) or yesterday's
    merge (nightly).
    """
    _isolate(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(diagnostics, "__version__", version)

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

    params = parse_qs(urlsplit(r.github_issue_url).query)
    assert params["labels"] == [f"bug,{label}"]


def test_issue_url_prefills_the_channel_dropdown(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(diagnostics, "__version__", "0.1.4-nightly.20260807t061500")

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

    params = parse_qs(urlsplit(r.github_issue_url).query)
    assert params["channel"] == ["Nightly"]
    assert params["version"] == ["0.1.4-nightly.20260807t061500"]


def test_issue_url_does_not_prefill_platform(tmp_path, monkeypatch):
    """The host OS says where the bug was SEEN, not that it is OS-specific.

    bug_report.yml's own help text warns that a guess in this field becomes a
    wrong ``platform:`` label, so the flow must leave it to the user.
    """
    _isolate(monkeypatch, tmp_path / "home")

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

    assert "platform" not in parse_qs(urlsplit(r.github_issue_url).query)


def test_issue_url_does_not_prefill_the_search_attestation(tmp_path, monkeypatch):
    """Prefilling "I searched open issues" would make the checkbox worthless."""
    _isolate(monkeypatch, tmp_path / "home")

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

    assert "search" not in parse_qs(urlsplit(r.github_issue_url).query)


# ── Template / label-vocabulary drift guards ─────────────────────────────────
#
# Dropdown prefill matches option text VERBATIM and silently no-ops on a miss,
# and `labels=` silently drops a name the repo does not define. Both failures
# are invisible at runtime -- the form just comes up with an empty field -- so
# they are pinned here instead.

_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "ISSUE_TEMPLATE"
    / "bug_report.yml"
)


def _template_dropdown_options(field_id: str) -> list[str]:
    spec = yaml.safe_load(_TEMPLATE.read_text(encoding="utf-8"))
    for block in spec["body"]:
        if block.get("id") == field_id:
            return list(block["attributes"]["options"])
    raise AssertionError(f"bug_report.yml has no dropdown with id {field_id!r}")


def test_channel_options_exist_in_the_issue_template():
    options = _template_dropdown_options("channel")
    for value in diagnostics._CHANNEL_OPTIONS.values():
        assert value in options, (
            f"{value!r} is not an option of bug_report.yml's channel dropdown; "
            "the prefill would silently leave the field empty"
        )
    # Every channel the classifier can return must be prefillable.
    assert set(diagnostics._CHANNEL_OPTIONS) == set(diagnostics._CHANNEL_LABELS)


def test_install_options_exist_in_the_issue_template():
    options = _template_dropdown_options("install")
    for value in diagnostics._INSTALL_OPTIONS.values():
        assert value in options, (
            f"{value!r} is not an option of bug_report.yml's install dropdown"
        )


def test_every_known_distribution_maps_to_an_install_option():
    """A new packaging lane must not silently stop prefilling the form."""
    assert set(beacon.KNOWN_DISTRIBUTIONS) == set(diagnostics._INSTALL_OPTIONS)


def test_triage_workflow_maps_every_channel_dropdown_answer():
    """The workflow's case list and the template's options must not drift.

    ``issue-triage.yml`` maps the dropdown answer to a label with a shell
    ``case``. If someone renames an option in the template, that case stops
    matching and every report from github.com loses its channel label -- with
    no error anywhere, because "no answer" is a legitimate outcome.
    """
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "issue-triage.yml"
    ).read_text(encoding="utf-8")

    for option in _template_dropdown_options("channel"):
        if option == "Not sure":
            continue  # deliberately unmapped: no label is the right answer
        assert f'"{option}")' in workflow, (
            f"issue-triage.yml has no case branch for the {option!r} channel "
            "option; reports filed on github.com would lose the label"
        )

    for label in diagnostics._CHANNEL_LABELS.values():
        assert f'"{label}"' in workflow, (
            f"issue-triage.yml never applies {label!r}, but the dashboard flow "
            "attaches it -- the two paths must agree on the vocabulary"
        )


# ── API handlers (mode-independent: stub request + asyncio.run) ──────────────


class _DownloadReq:
    def __init__(self, filename: str) -> None:
        self.match_info = {"filename": filename}


class _CollectReq:
    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


def _stub_sel(monkeypatch) -> MagicMock:
    """Install a mock SEL logger and return it (download handler audits via it)."""
    sel = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: sel)
    return sel


def test_download_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.diagnostics.config_dir", lambda: tmp_path
    )
    sel = _stub_sel(monkeypatch)
    resp = asyncio.run(dh.api_diagnostics_download(_DownloadReq("../../etc/passwd")))
    assert resp.status == 403
    assert sel.log_tool_invocation.call_args.kwargs["outcome"] == "denied"


def test_download_rejects_non_zip(tmp_path, monkeypatch):
    diag = tmp_path / "diagnostics"
    diag.mkdir()
    (diag / "foo.txt").write_text("not a zip")
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.diagnostics.config_dir", lambda: tmp_path
    )
    sel = _stub_sel(monkeypatch)
    resp = asyncio.run(dh.api_diagnostics_download(_DownloadReq("foo.txt")))
    assert resp.status == 403
    assert sel.log_tool_invocation.call_args.kwargs["outcome"] == "denied"


def test_download_allows_and_audits_valid_zip(tmp_path, monkeypatch):
    diag = tmp_path / "diagnostics"
    diag.mkdir()
    (diag / "b.zip").write_bytes(b"PK\x03\x04zip")
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.diagnostics.config_dir", lambda: tmp_path
    )
    sel = _stub_sel(monkeypatch)
    resp = asyncio.run(dh.api_diagnostics_download(_DownloadReq("b.zip")))
    assert resp.status == 200
    assert sel.log_tool_invocation.call_args.kwargs["outcome"] == "allowed"


def test_collect_handler_returns_download_url(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.diagnostics.config_dir", lambda: tmp_path
    )
    fake = BundleResult(
        zip_path=tmp_path / "b.zip",
        filename="b.zip",
        included=["versions.txt", "manifest.json"],
        skipped=[],
        redaction_summary={"versions.txt": 0},
        github_issue_url="https://github.com/kirodotdev/KiroCrew/issues/new?title=x",
    )
    monkeypatch.setattr(dh.diagnostics, "collect_bundle", lambda **kw: fake)

    resp = asyncio.run(
        dh.api_diagnostics_collect(_CollectReq({"note": "hi", "include_logs": True}))
    )
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["download_url"] == "/api/diagnostics/download/b.zip"
    assert body["github_issue_url"].startswith("https://github.com/")
    assert body["filename"] == "b.zip"


def test_note_is_redacted_everywhere(tmp_path, monkeypatch):
    """A secret pasted into the note must not survive into any output."""
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    secret = "Bearer sk-ant-NOTEsecretVALUE1234567890"
    r = diagnostics.collect_bundle(
        note=f"it broke, my log had {secret} in it",
        output_dir=tmp_path / "out",
    )
    with zipfile.ZipFile(r.zip_path) as z:
        versions = z.read("versions.txt").decode()
        manifest = z.read("manifest.json").decode()
    assert "NOTEsecretVALUE1234567890" not in versions
    assert "NOTEsecretVALUE1234567890" not in manifest
    assert "NOTEsecretVALUE1234567890" not in r.github_issue_url


def test_old_bundles_are_pruned(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    base = time.time()
    for i in range(6):
        p = out / f"kirocrew-diagnostics-2026010{i}-000000.zip"
        p.write_bytes(b"x")
        os.utime(p, (base + i, base + i))  # distinct mtimes; newest = i==5
    diagnostics._prune_old_bundles(out, keep=3)
    kept = {p.name for p in out.glob("kirocrew-diagnostics-*.zip")}
    assert len(kept) == 3
    assert "kirocrew-diagnostics-20260105-000000.zip" in kept  # newest kept
    assert "kirocrew-diagnostics-20260100-000000.zip" not in kept  # oldest pruned


def test_collect_handler_rejects_non_object_body(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.diagnostics.config_dir", lambda: tmp_path
    )
    resp = asyncio.run(dh.api_diagnostics_collect(_CollectReq(["not", "a", "dict"])))
    assert resp.status == 400
