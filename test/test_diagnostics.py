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
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from conftest import make_dir_link, requires_symlinks
from kiro_crew import beacon, diagnostics, release_channel
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


def _pin_provenance(monkeypatch, *, distribution=None, recorded=None) -> None:
    """Answer the two release-provenance probes in-process.

    ``release_channel.provenance`` reads the installed distribution's metadata
    version and the ``$KIROCREW_HOME/channel`` record beside the version stamp.
    Both are host state -- the developer running this suite may follow a lane
    and has SOME ``kirocrew`` dist-info installed -- so every bundle test pins
    them, defaulting to "nothing beyond the version stamp", which is what a
    source checkout and a desktop install with no record look like.
    """
    monkeypatch.setattr(
        release_channel, "distribution_version", lambda: distribution, raising=False
    )
    monkeypatch.setattr(release_channel, "recorded_channel", lambda: recorded, raising=False)


def _isolate(monkeypatch, home: Path) -> None:
    """Point the collector at a temp home and stub host-specific probes."""
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("kiro_crew.diagnostics.config_dir", lambda: home)
    # Pin Path.home() too: on Windows the real host home is a PARENT of tmp_path,
    # so without this the real home would collapse the tmp prefix and the
    # fixtures' home paths would never match. Both the collector's home and
    # data-home resolve to this temp dir, so the test controls the full prefix.
    monkeypatch.setattr(diagnostics.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(diagnostics, "_macos_crash_reports", lambda: [])
    monkeypatch.setattr(diagnostics, "_kiro_cli_chat_log", lambda: None)
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [])
    monkeypatch.setattr(diagnostics, "_kiro_cli_version", lambda: "kiro-cli 2.14.2")
    _pin_provenance(monkeypatch)


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


def test_home_prefixes_collapse_but_system_and_repo_frames_survive(tmp_path, monkeypatch):
    """Only the home/data-home prefix is collapsed to ``~`` in bundle members.

    The bundle's stated destination is a public issue, so the OS login must not
    survive -- but a maintainer still needs the file locations that make a
    bundle useful. Drive the collector end to end with a traceback whose frames
    span the data-home, a system path and a repo-relative path, and assert the
    home frames show as ``~/...`` while ``/usr/lib/...`` and ``src/kiro_crew/...``
    frames are left intact. The ``versions.txt`` ``data_home`` line collapses the
    same way.
    """
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    # Build the home frame with the OS-native separator so the fixture matches
    # how a real traceback on this platform would spell it; assert on a
    # slash-normalized copy so one assertion holds on POSIX and Windows alike.
    home_frame = f'  File "{home}{os.sep}gateway.log", line 3, in handle'
    system_frame = '  File "/usr/lib/python3.12/asyncio/events.py", line 80, in _run'
    repo_frame = '  File "src/kiro_crew/diagnostics.py", line 42, in collect_bundle'
    (home / "gateway.log").write_text(
        f"Traceback (most recent call last):\n"
        f"{home_frame}\n{system_frame}\n{repo_frame}\n"
        f"a perfectly normal log line\n"
    )

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

    with zipfile.ZipFile(r.zip_path) as z:
        gw = z.read("gateway.log").decode()
        versions = z.read("versions.txt").decode()
        manifest = json.loads(z.read("manifest.json"))

    gw_slash = gw.replace("\\", "/")
    # The home/data-home prefix is gone from every text member, replaced by ``~``.
    assert str(home) not in gw, "home prefix survived in gateway.log"
    assert str(home) not in versions, "data_home absolute path survived in versions.txt"
    assert "~/gateway.log" in gw_slash, "home frame should collapse to ~, keeping the tail"
    assert "data_home: ~" in versions, "data_home should collapse to ~"
    # System and repo-relative frames are left intact so the trace stays useful.
    assert "/usr/lib/python3.12/asyncio/events.py" in gw, "system frame must survive"
    assert "src/kiro_crew/diagnostics.py" in gw, "repo-relative frame must survive"
    assert ", line 3, in handle" in gw, "the rest of the home frame must survive"
    assert "a perfectly normal log line" in gw
    # The data_home collapse in versions.txt is a path collapse, not a secret:
    # it is tallied in ``path_collapses``, leaving the secret count untouched.
    assert r.path_collapses >= 1
    assert manifest["total_redactions"] == r.total_redactions


def test_credential_after_a_path_prefix_is_still_redacted(tmp_path, monkeypatch):
    """A header sitting right after a ``file:line:`` prefix must still be redacted.

    The home collapse runs LAST, after the credential rules, so a ``grep``-shaped
    line (``<home>/req.txt:12:Authorization: Basic …``) has its credential removed
    first and the home prefix collapsed to ``~`` second -- the credential never
    rides along into the output.
    """
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    secret = "dXNlcjpwYXNzd29yZA=="
    (home / "gateway.log").write_text(
        f"{home}{os.sep}app{os.sep}req.txt:12:Authorization: Basic {secret}\nplain\n"
    )

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

    with zipfile.ZipFile(r.zip_path) as z:
        gw = z.read("gateway.log").decode()
    assert secret not in gw, "credential survived: ordering let the prefix pass run first"
    assert str(home) not in gw, "home prefix survived"
    assert "~/app/req.txt" in gw.replace("\\", "/"), "the home prefix should collapse to ~"
    assert "plain" in gw


def test_issue_body_has_no_absolute_zip_path(tmp_path, monkeypatch):
    """Ruling 1: the pre-filled issue body must not embed the absolute zip path.

    ``result.zip_path`` lives under ``<data_home>/diagnostics``, so printing it
    into a public issue leaks the OS login -- the exact thing the member scrub
    strips out. The filename alone matches the attached .zip. The body must carry
    the login-free ``result.filename`` and NOT the absolute path.
    """
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    (home / "gateway.log").write_text("a perfectly normal log line\n")

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    body = parse_qs(urlsplit(r.github_issue_url).query)["context"][0]

    assert str(r.zip_path) not in body, "absolute zip path leaked into the issue body"
    assert str(home) not in body, "home prefix leaked into the issue body"
    assert r.filename in body, "the login-free filename should still identify the bundle"
    assert "secret(s) auto-redacted" in body


def test_collapse_skips_root_and_single_segment_and_respects_boundary(monkeypatch):
    """Ruling 2: a root/single-segment HOME is skipped; a prefix match is segment-bounded.

    A bare root or a single-segment HOME carries no login and must never
    collapse the leading separator of every path. And a login HOME must not
    rewrite a longer login it is a strict prefix of -- the prefix collapses when
    the login segment ends, i.e. the next character does not continue it. A
    separator, quote, whitespace, end-of-token AND a delimiter such as ``:``,
    ``,``, ``)`` or a sentence-final ``.`` all end the segment and collapse.
    """
    # Home-path strings are assembled from a parent and a segment so no literal
    # home path appears on a source line (the internal-content scan flags those).
    parent = "/" + "home"
    short = f"{parent}/a1"  # a login
    longer = f"{parent}/a1b2"  # a different login that has `short` as a prefix

    # Root HOME: nothing is a login, so nothing collapses.
    monkeypatch.setattr(diagnostics.Path, "home", classmethod(lambda cls: Path("/")))
    monkeypatch.setattr(diagnostics, "config_dir", lambda: Path("/"))
    assert diagnostics._home_prefixes() == []
    out, n = diagnostics._collapse_home_prefixes("/usr/lib/x and /opt/y")
    assert out == "/usr/lib/x and /opt/y" and n == 0

    # Single-segment HOME (the shared parent): still skipped.
    monkeypatch.setattr(diagnostics.Path, "home", classmethod(lambda cls: Path(parent)))
    assert diagnostics._home_prefixes() == []

    # A real login prefix, but a strict-prefix sibling must survive.
    monkeypatch.setattr(diagnostics.Path, "home", classmethod(lambda cls: Path(short)))
    out, n = diagnostics._collapse_home_prefixes(f"{short}/log and {longer}/log")
    assert out == f"~/log and {longer}/log", out
    assert n == 1, "only the bounded login match should collapse"
    # End-of-token and quote boundaries also count.
    out, n = diagnostics._collapse_home_prefixes(f'see "{short}" at {short}')
    assert out == 'see "~" at ~' and n == 2
    # Non-separator delimiters that end the login segment must also collapse:
    # a PATH list separator, a trailing comma, a closing paren, a sentence dot.
    cases = [
        (f"PATH={short}:/usr/bin", "PATH=~:/usr/bin"),
        (f"cwd={short},", "cwd=~,"),
        (f"({short})", "(~)"),
        (f"logged at {short}.", "logged at ~."),
    ]
    for raw, want in cases:
        out, n = diagnostics._collapse_home_prefixes(raw)
        assert out == want and n == 1, f"delimiter boundary failed: {raw!r} -> {out!r}"
    # But a continuing character still protects a longer login: the shorter
    # prefix must NOT collapse the longer one even next to such a delimiter.
    out, n = diagnostics._collapse_home_prefixes(f"{longer}:/usr/bin")
    assert out == f"{longer}:/usr/bin" and n == 0, out


def test_nested_data_home_keeps_relative_tail(tmp_path, monkeypatch):
    """Ruling 3: a data home nested under home collapses to ``~/.kiro/crew/...``.

    When data home is nested under home, only the HOME prefix is registered, so
    ``<home>/.kiro/crew/gateway.log`` collapses to ``~/.kiro/crew/gateway.log``
    with the ``.kiro/crew`` tail kept rather than swallowed down to a bare ``~``.
    This drives the collector with a data home that DIFFERS from home.
    """
    home = tmp_path / "home"
    data_home = home / ".kiro" / "crew"
    data_home.mkdir(parents=True, exist_ok=True)
    # Pin config_dir to the nested data home and Path.home to home. Set the host
    # probes directly (not via _isolate, which would pin Path.home to data_home).
    monkeypatch.setattr("kiro_crew.diagnostics.config_dir", lambda: data_home)
    monkeypatch.setattr(diagnostics.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(diagnostics, "_macos_crash_reports", lambda: [])
    monkeypatch.setattr(diagnostics, "_kiro_cli_chat_log", lambda: None)
    monkeypatch.setattr(diagnostics, "_kiro_cli_extra_logs", lambda: [])
    monkeypatch.setattr(diagnostics, "_kiro_cli_version", lambda: "kiro-cli 2.14.2")
    _pin_provenance(monkeypatch)
    (data_home / "gateway.log").write_text(
        f'  File "{data_home}{os.sep}gateway.log", line 3, in handle\n'
        f"a perfectly normal log line\n"
    )

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    with zipfile.ZipFile(r.zip_path) as z:
        gw = z.read("gateway.log").decode()

    gw_slash = gw.replace("\\", "/")
    assert str(home) not in gw, "home prefix survived"
    assert "~/.kiro/crew/gateway.log" in gw_slash, "nested data-home tail must be preserved"
    assert "~/gateway.log" not in gw_slash, "the .kiro/crew tail must NOT be swallowed"
    assert ", line 3, in handle" in gw


def test_separate_data_home_collapses_on_its_own(monkeypatch):
    """Ruling 3 (other branch): a data home NOT under home collapses on its own."""
    home = "/" + "home" + "/a1"  # assembled so no literal home path is on a line
    monkeypatch.setattr(diagnostics.Path, "home", classmethod(lambda cls: Path(home)))
    monkeypatch.setattr(diagnostics, "config_dir", lambda: Path("/var/lib/kirocrew"))
    out, n = diagnostics._collapse_home_prefixes(
        f"{home}/notes.txt and /var/lib/kirocrew/gateway.log and /usr/bin/x"
    )
    assert out == "~/notes.txt and ~/gateway.log and /usr/bin/x", out
    assert n == 2


def test_windows_json_escaped_and_case_insensitive(monkeypatch):
    """Ruling 4: JSON-escaped and case-folded Windows home spellings all collapse.

    Some bundle members (kiro-chat.log, crash.log) embed paths inside JSON
    strings, where a Windows home path appears with doubled backslashes; tools
    that normalise separators print the forward-slash form; and the drive/user
    casing varies. All must collapse so the login never ships.
    """
    # Assemble the Windows home base and each spelling from parts so no literal
    # Windows home path appears on a source line (the internal-content scan
    # flags those). seg is the login segment.
    seg = "Nm1"
    back = "\\"
    base = "C:" + back + "Users" + back + seg  # native backslash form
    fwd = base.replace(back, "/")  # forward-slash form
    monkeypatch.setattr(diagnostics.Path, "home", classmethod(lambda cls: Path(base)))
    monkeypatch.setattr(
        diagnostics, "config_dir", lambda: Path(base + back + ".kiro" + back + "crew")
    )

    # Native backslash spelling.
    out, n = diagnostics._collapse_home_prefixes(base + back + "kiro-chat.log")
    assert out == "~" + back + "kiro-chat.log" and n == 1
    # Forward-slash spelling.
    out, n = diagnostics._collapse_home_prefixes(fwd + "/crash.log")
    assert out == "~/crash.log" and n == 1
    # JSON-escaped (doubled backslashes) spelling, as embedded in a JSON string.
    doubled = base.replace(back, back + back)
    out, n = diagnostics._collapse_home_prefixes(
        '{"path": "' + doubled + back + back + 'crash.log"}'
    )
    assert out == '{"path": "~' + back + back + 'crash.log"}', out
    assert n == 1
    # Case-insensitive drive and user segment.
    out, n = diagnostics._collapse_home_prefixes(base.lower() + back + "kiro-chat.log")
    assert out == r"~\kiro-chat.log" and n == 1


def test_path_collapses_counted_apart_from_secrets(tmp_path, monkeypatch):
    """Ruling 5: path collapses are tallied separately from secret redactions.

    A home-prefix collapse hides an OS login, not a secret, so it must not inflate
    ``total_redactions`` / the "N secret(s) auto-redacted" line. The collapse count
    lives in ``result.path_collapses`` and the issue body reports it apart.
    """
    home = tmp_path / "home"
    _isolate(monkeypatch, home)
    # One real secret + a couple of home-path frames (no secret in them).
    secret = "AKIAIOSFODNN7EXAMPLE"
    (home / "gateway.log").write_text(
        f"AWS key {secret}\n"
        f'  File "{home}{os.sep}gateway.log", line 1, in a\n'
        f'  File "{home}{os.sep}app{os.sep}x.py", line 2, in b\n'
    )

    r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
    with zipfile.ZipFile(r.zip_path) as z:
        gw = z.read("gateway.log").decode()
        manifest = json.loads(z.read("manifest.json"))
    body = parse_qs(urlsplit(r.github_issue_url).query)["context"][0]

    assert secret not in gw
    assert str(home) not in gw
    # The secret count excludes the path collapses.
    assert r.redaction_summary["gateway.log"] >= 1, "the AWS key must be counted as a secret"
    assert r.path_collapses >= 2, "both home frames should be counted as collapses"
    # The manifest's secret tally excludes the path collapses (no collapse key).
    assert manifest["total_redactions"] == r.total_redactions
    assert "path_collapses" not in manifest
    # The issue body reports secrets and collapses as distinct numbers.
    assert f"{r.total_redactions} secret(s) auto-redacted" in body
    assert f"{r.path_collapses} path(s) collapsed" in body


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
    ("request used Authorization: Basic <b64>"). A rule anchored
    with ``^`` would let those credentials reach the bundle and the pre-filled
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
    offsets would not match. Asserted by capturing the real flags, so the
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


@requires_symlinks
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


@requires_symlinks
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


class TestTerminalIssueUrlSurvivesRedaction:
    """The link `kirocrew doctor` PRINTS must reach the user intact.

    The dashboard renders `github_issue_url` from a JSON response no redactor
    scans, so it keeps the full pre-filled body. A link printed to stdout has no
    such guarantee — selected into chat, captured from a shell tool, or relayed
    by an agent, it crosses `redact_exfiltration_urls`, and the pre-filled
    variant's ~600-char query trips the query-length heuristic and is replaced
    wholesale by `[REDACTED: suspicious URL to github.com]`.
    """

    @pytest.fixture(autouse=True)
    def _pinned_version_probe(self, monkeypatch):  # type: ignore[no-untyped-def]
        """Pin the kiro-cli version probe the issue body embeds.

        Both URL variants render ``_kiro_cli_version()`` into the body, and the
        real probe pins and SPAWNS the host's kiro-cli binary (``--version``).
        These tests are about URL shape and redaction, so the seam is answered
        in-process, the same way ``_isolate`` answers it for the bundle tests.
        """
        from kiro_crew import diagnostics

        monkeypatch.setattr(diagnostics, "_kiro_cli_version", lambda: "kiro-cli 2.14.2")
        _pin_provenance(monkeypatch)

    def _result(self, tmp_path):  # type: ignore[no-untyped-def]
        from kiro_crew import diagnostics

        name = "kirocrew-diagnostics-20260811.zip"
        return diagnostics.BundleResult(
            zip_path=tmp_path / name,
            filename=name,
            redaction_summary={"config.json": 3},
        )

    def test_prefilled_variant_is_the_one_that_gets_eaten(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Guard the premise — if this stops being redacted the fix is moot."""
        from kiro_crew import diagnostics
        from kiro_crew.security import scan_exfiltration_urls

        rich = diagnostics._issue_url(self._result(tmp_path), "something broke")
        assert len(rich.split("?", 1)[1]) >= 200
        assert scan_exfiltration_urls(rich) != []

    def test_terminal_variant_is_not_redacted(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from kiro_crew import diagnostics
        from kiro_crew.security import redact_exfiltration_urls, scan_exfiltration_urls

        url = diagnostics.terminal_issue_url(self._result(tmp_path), "something broke")
        assert scan_exfiltration_urls(url) == []
        assert redact_exfiltration_urls(url)[0] == url

    def test_terminal_variant_query_is_bounded(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A long note must not push the query back over the threshold.

        Dropping only `context` would leave the length at the mercy of the user's
        note, so a budget that merely fits today's fields is not enough — the
        free-form fields are dropped so the query is bounded by construction.
        """
        from kiro_crew import diagnostics
        from kiro_crew.security import scan_exfiltration_urls

        result = self._result(tmp_path)
        for note in ("", "short note", "x" * 5000, "multi\nline\nnote " * 200):
            url = diagnostics.terminal_issue_url(result, note)
            assert len(url.split("?", 1)[1]) < 200
            assert scan_exfiltration_urls(url) == []

    def test_terminal_variant_keeps_the_triage_form_fields(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Shortening must not cost the answers automatic triage reads."""
        from urllib.parse import parse_qs, urlsplit

        from kiro_crew import diagnostics

        params = parse_qs(urlsplit(diagnostics.terminal_issue_url(self._result(tmp_path))).query)
        assert params["template"] == ["bug_report.yml"]
        for field_name in ("labels", "title", "version", "channel"):
            assert params.get(field_name), field_name
        assert "context" not in params
        assert "what-happened" not in params


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
    """The report's lane must follow desktop SemVer AND wheel PEP 440 stamps.

    The SemVer half mirrors ``auto-update.js`` ``channelForVersion``; the PEP
    440 half exists because ``build-wheel.yml`` rewrites ``__version__`` to the
    wheel version, which spells the same prerelease without a hyphen. With no
    metadata and no record, the pipeline's own spelling is the whole evidence.
    """
    monkeypatch.setattr(diagnostics, "__version__", version)
    _pin_provenance(monkeypatch)
    assert diagnostics._provenance().channel == expected


def test_prerelease_wheel_is_never_reported_as_stable(monkeypatch):
    """Regression guard for the bug this change fixes.

    A CLI insider install reports ``1.2.3rc4`` and a CLI nightly reports
    ``1.2.3.dev<stamp>``. Both were classified stable, so their bug reports
    arrived indistinguishable from a supported build's — the opposite of the
    point of having a prerelease channel.
    """
    _pin_provenance(monkeypatch)
    for version in ("0.1.4rc4", "0.1.4.dev20260807061500"):
        monkeypatch.setattr(diagnostics, "__version__", version)
        assert diagnostics._provenance().channel != "stable", version


def test_channel_never_returns_the_old_prerelease_name(monkeypatch):
    """``"prerelease"`` named a channel no feed, label, or doc uses.

    An insider build could report it, so its bug reports would arrive tagged with
    a lane nobody triages by. Every answer must be a key of the label map.
    """
    _pin_provenance(monkeypatch)
    for version in ("0.1.4", "0.1.4-insider.1", "0.1.4-nightly.20260807t0615"):
        monkeypatch.setattr(diagnostics, "__version__", version)
        assert diagnostics._provenance().channel in diagnostics._CHANNEL_LABELS


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


# ── Trusted release-channel provenance ───────────────────────────────────────
#
# A packaged build can carry TWO identities: the running ``__version__`` may be
# a repackager's plain four-part build stamp (``0.7.0.5``, the shape the
# BUILD_VERSION rule admits -- and that rule REFUSES a stamp over a prerelease
# base, so stamping strips the marker by construction), while the installed
# distribution's metadata still says ``0.7.0rc7``. Classifying the stamp alone
# reports every such insider build as Stable, exposes the wrapper-internal
# stamp in the public version field, and attaches ``channel: stable`` at
# create time, where the reporter cannot take it back.


def _prefill(url: str) -> tuple[list[str], list[str], list[str]]:
    params = parse_qs(urlsplit(url).query)
    return params["version"], params["channel"], params["labels"]


class TestReportProvenance:
    """The prefilled report comes from trusted provenance, never from a guess."""

    def test_packaged_insider_build_reports_its_public_release(self, tmp_path, monkeypatch):
        """The reporter's case: plain wrapper stamp, rc distribution, insider record."""
        _isolate(monkeypatch, tmp_path / "home")
        monkeypatch.setattr(diagnostics, "__version__", "0.7.0.5")
        _pin_provenance(monkeypatch, distribution="0.7.0rc7", recorded="insider")

        r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

        assert _prefill(r.github_issue_url) == (
            ["0.7.0"],
            ["Insider (prerelease)"],
            ["bug,channel: insider"],
        )
        assert "0.7.0.5" not in r.github_issue_url

    def test_packaged_insider_build_needs_no_channel_record(self, tmp_path, monkeypatch):
        """A desktop install writes no ``channel`` file; the rc marker in the
        distribution metadata is provenance enough for an insider build."""
        _isolate(monkeypatch, tmp_path / "home")
        monkeypatch.setattr(diagnostics, "__version__", "0.7.0.5")
        _pin_provenance(monkeypatch, distribution="0.7.0rc7", recorded=None)

        r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

        assert _prefill(r.github_issue_url) == (
            ["0.7.0"],
            ["Insider (prerelease)"],
            ["bug,channel: insider"],
        )

    def test_absent_provenance_prefills_not_sure_and_no_channel_label(self, tmp_path, monkeypatch):
        """A build stamp says nothing about its lane. With no distribution
        metadata and no record, the honest dropdown answer is the form's own
        ``Not sure`` -- and no ``channel:`` label, because a create-time label
        outlives whatever the reporter later picks in the dropdown."""
        _isolate(monkeypatch, tmp_path / "home")
        monkeypatch.setattr(diagnostics, "__version__", "0.7.0.5")
        _pin_provenance(monkeypatch, distribution=None, recorded=None)

        r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

        assert _prefill(r.github_issue_url) == (["0.7.0"], ["Not sure"], ["bug"])

    def test_contradictory_provenance_prefills_not_sure(self, tmp_path, monkeypatch):
        """A stable record over a prerelease build -- a promoted-stable install
        (promotion never re-stamps) or a lane switch not yet applied -- is a
        contradiction, and the report guesses neither side."""
        _isolate(monkeypatch, tmp_path / "home")
        monkeypatch.setattr(diagnostics, "__version__", "0.7.0rc7")
        _pin_provenance(monkeypatch, distribution="0.7.0rc7", recorded="stable")

        r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

        assert _prefill(r.github_issue_url) == (["0.7.0rc7"], ["Not sure"], ["bug"])

    def test_stable_release_is_unchanged(self, tmp_path, monkeypatch):
        """Pin: a real stable release still files as Stable with its label."""
        _isolate(monkeypatch, tmp_path / "home")
        monkeypatch.setattr(diagnostics, "__version__", "0.7.0")
        _pin_provenance(monkeypatch, distribution="0.7.0", recorded=None)

        r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

        assert _prefill(r.github_issue_url) == (["0.7.0"], ["Stable"], ["bug,channel: stable"])

    @pytest.mark.parametrize(
        ("version", "option", "label"),
        [
            ("0.7.0-insider.4", "Insider (prerelease)", "channel: insider"),
            ("0.7.0-nightly.20260907t061500", "Nightly", "channel: nightly"),
        ],
    )
    def test_desktop_prerelease_keeps_its_lane_over_unstamped_metadata(
        self, tmp_path, monkeypatch, version, option, label
    ):
        """Pin: ``packaging/build-desktop.sh`` pip-installs the checkout, whose
        ``pyproject.toml`` version the desktop lanes never stamp, so a desktop
        insider or nightly build carries a plain ``0.7.0`` in its dist-info
        beside the stamped ``__version__``. Plain metadata is silence, not a
        stable claim: the build's own marker decides, and the public version
        stays the pipeline's spelling."""
        _isolate(monkeypatch, tmp_path / "home")
        monkeypatch.setattr(diagnostics, "__version__", version)
        _pin_provenance(monkeypatch, distribution="0.7.0", recorded=None)

        r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

        assert _prefill(r.github_issue_url) == ([version], [option], [f"bug,{label}"])

    def test_terminal_link_carries_the_same_provenance(self, tmp_path, monkeypatch):
        """``kirocrew doctor --bundle`` prints the bounded variant; it must not
        answer differently from the dashboard's link."""
        _isolate(monkeypatch, tmp_path / "home")
        monkeypatch.setattr(diagnostics, "__version__", "0.7.0.5")
        _pin_provenance(monkeypatch, distribution="0.7.0rc7", recorded=None)

        r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
        url = diagnostics.terminal_issue_url(r)

        assert _prefill(url) == (["0.7.0"], ["Insider (prerelease)"], ["bug,channel: insider"])
        assert "0.7.0.5" not in url

    def test_private_bundle_keeps_the_wrapper_stamp(self, tmp_path, monkeypatch):
        """The stamp is not lost, it moves: the bundle the reporter drags in is
        private, so it records both identities and the resolved lane."""
        _isolate(monkeypatch, tmp_path / "home")
        monkeypatch.setattr(diagnostics, "__version__", "0.7.0.5")
        _pin_provenance(monkeypatch, distribution="0.7.0rc7", recorded=None)

        r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

        with zipfile.ZipFile(r.zip_path) as z:
            versions = z.read("versions.txt").decode()
            manifest = json.loads(z.read("manifest.json"))
        for line in (
            "kirocrew_version: 0.7.0.5",
            "release: 0.7.0",
            "distribution_version: 0.7.0rc7",
            "channel: insider",
            "channel_record: absent",
        ):
            assert line in versions, line
        assert manifest["kirocrew_version"] == "0.7.0.5"
        assert manifest["release"] == "0.7.0"
        assert manifest["distribution_version"] == "0.7.0rc7"
        assert manifest["channel"] == "insider"
        assert manifest["channel_record"] is None
        # Private keeps the stamp; the public field is still the folded release.
        assert _prefill(r.github_issue_url)[0] == ["0.7.0"]
        assert "0.7.0.5" not in r.github_issue_url

    def test_private_bundle_records_the_channel_record(self, tmp_path, monkeypatch):
        """The ``$KIROCREW_HOME/channel`` record is evidence, so both private
        surfaces name it verbatim -- the manifest as the same value the
        resolver weighed, not a re-derivation."""
        _isolate(monkeypatch, tmp_path / "home")
        monkeypatch.setattr(diagnostics, "__version__", "0.7.0.5")
        _pin_provenance(monkeypatch, distribution="0.7.0rc7", recorded="insider")

        r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

        with zipfile.ZipFile(r.zip_path) as z:
            versions = z.read("versions.txt").decode()
            manifest = json.loads(z.read("manifest.json"))
        assert "channel_record: insider" in versions
        assert manifest["channel_record"] == "insider"
        assert manifest["distribution_version"] == "0.7.0rc7"
        assert manifest["channel"] == "insider"

    def test_private_bundle_names_an_unknown_lane_as_unknown(self, tmp_path, monkeypatch):
        _isolate(monkeypatch, tmp_path / "home")
        monkeypatch.setattr(diagnostics, "__version__", "0.7.0.5")
        _pin_provenance(monkeypatch, distribution=None, recorded=None)

        r = diagnostics.collect_bundle(output_dir=tmp_path / "out")

        with zipfile.ZipFile(r.zip_path) as z:
            versions = z.read("versions.txt").decode()
            manifest = json.loads(z.read("manifest.json"))
        assert "channel: unknown" in versions
        assert "distribution_version: unavailable" in versions
        assert manifest["channel"] == "unknown"
        # A silent source is JSON null in the manifest, a sentinel word only in
        # the text file a human reads.
        assert manifest["distribution_version"] is None
        assert manifest["channel_record"] is None

    def test_one_bundle_is_written_from_one_resolution(self, tmp_path, monkeypatch):
        """``set_release_channel`` is served on the event loop while
        ``collect_bundle`` runs in a worker thread, so the ``channel`` record
        can change between two reads of it. The bundle must not carry a
        ``versions.txt``, a ``manifest.json`` and an issue link that each read
        the record at a different moment: one resolution, taken before any
        member is written, feeds all three (and the terminal link)."""
        _isolate(monkeypatch, tmp_path / "home")
        before = release_channel.Provenance(
            version="0.7.0rc7",
            release="0.7.0rc7",
            distribution="0.7.0rc7",
            recorded="insider",
            channel="insider",
        )
        # The same build after a stable record landed mid-collection: the
        # sources now disagree, so a later read resolves to no lane at all.
        after = before._replace(recorded="stable", channel=None)
        calls: list[int] = []

        def flipping() -> release_channel.Provenance:
            calls.append(1)
            return before if len(calls) == 1 else after

        monkeypatch.setattr(diagnostics, "_provenance", flipping)

        r = diagnostics.collect_bundle(output_dir=tmp_path / "out")
        terminal = diagnostics.terminal_issue_url(r)

        with zipfile.ZipFile(r.zip_path) as z:
            versions = dict(
                line.split(": ", 1) for line in z.read("versions.txt").decode().splitlines()
            )
            manifest = json.loads(z.read("manifest.json"))
        seen = (
            versions["channel"],
            versions["channel_record"],
            manifest["channel"],
            manifest["channel_record"],
            _prefill(r.github_issue_url)[1],
            _prefill(terminal)[1],
        )
        assert seen == (
            "insider",
            "insider",
            "insider",
            "insider",
            ["Insider (prerelease)"],
            ["Insider (prerelease)"],
        ), f"one bundle, mixed provenance (versions.txt, manifest, url, terminal): {seen}"
        assert len(calls) == 1, f"provenance resolved {len(calls)} times for one bundle"
        assert r.provenance == before


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
    # The fallback for unknown or contradictory provenance is the form's OWN
    # option, so it is subject to the same verbatim-match pin.
    assert release_channel.UNKNOWN_CHANNEL_FORM_OPTION in options


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


# ── collector link guard ──────────────────────────────────────────


class TestUsableDirRejectsEveryDirLink:
    """``_usable_dir`` is the collector's one guard against enumerating THROUGH a
    linked log directory: ``Path.is_dir()`` follows the link, and the real files
    found on the other side are not themselves links, so they sail past the
    per-file check and get packaged into a bundle destined for a public issue.

    ``Path.is_symlink()`` does not report a Windows junction, and a junction is
    the only directory link an unprivileged Windows user can create — a symlink
    needs ``SeCreateSymbolicLinkPrivilege`` — so a symlink-only guard is open on
    exactly the platform where planting one is easiest.
    """

    def test_a_real_directory_link_is_not_usable(self, tmp_path: Path) -> None:
        # A junction on Windows, a directory symlink on POSIX — the same
        # traversal either way, and no privilege needed on either.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "id_rsa").write_text("secret", encoding="utf-8")
        link = tmp_path / "kiro-log"
        make_dir_link(link, outside)

        assert link.is_dir()  # the link resolves — this is what makes it dangerous
        assert list(link.glob("*"))  # and enumerating it reaches the target's files
        assert diagnostics._usable_dir(link) is False

    def test_a_plain_directory_stays_usable(self, tmp_path: Path) -> None:
        # The guard must not reject the ordinary case it exists to allow.
        real = tmp_path / "kiro-log"
        real.mkdir()
        assert diagnostics._usable_dir(real) is True

    def test_guard_routes_through_the_shared_link_predicate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pinned at the seam as well as end to end: the junction-aware predicate
        # is the shared one, so this guard cannot drift from the rest of the
        # tree on which reparse types count as a link.
        real = tmp_path / "kiro-log"
        real.mkdir()
        monkeypatch.setattr(diagnostics.platform_compat, "is_link_or_junction", lambda p: True)
        assert diagnostics._usable_dir(real) is False


# ── API handlers (mode-independent: stub request + asyncio.run) ──────────────


_OWNER = "owner-subject"


class _OwnerReq(dict):
    """A stub request carrying the dashboard owner's claims.

    Both diagnostics handlers are owner-gated, so the dashboard-user request these
    tests model carries ``app == ""`` and the configured owner's subject.
    """

    def __init__(self) -> None:
        super().__init__(app="", user=_OWNER)
        self.app = {"state": SimpleNamespace(owner_id=_OWNER)}


class _DownloadReq(_OwnerReq):
    def __init__(self, filename: str) -> None:
        super().__init__()
        self.match_info = {"filename": filename}


class _CollectReq(_OwnerReq):
    def __init__(self, body: dict) -> None:
        super().__init__()
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
    # The response carries the secret tally but not the path-collapse count:
    # no client reads a collapse key, so it is kept out of the payload shape.
    assert "total_redactions" in body
    assert "path_collapses" not in body


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
