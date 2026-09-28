"""The opt-in ACP frame recorder that produces a replay-corpus fixture.

``kiro_crew.acp._frame_record.record_frame`` sits on the hot path of every frame
of every session, so the properties worth pinning are the ones whose absence
would be a production incident rather than a wrong fixture: silent when off, off
the event loop when on, silent when broken, scrubbed when it writes, and
owner-only on disk.

The replay corpus itself (``test/fixtures/acp_frames/``) and its snapshot test
are ``test/test_acp_frame_replay.py``; this module covers only the recorder.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import stat
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from kiro_crew import security_posture
from kiro_crew.acp import _frame_record
from kiro_crew.acp import client as client_module
from kiro_crew.acp import runtime as runtime_module
from kiro_crew.acp_backends import ACP_BACKEND_KIRO, ACP_BACKENDS_KNOWN

# The recorder is POSIX-only (see the module docstring of ``_frame_record``):
# every test here exercises the pinned-descriptor path. The one Windows
# behaviour -- stand down with a named reason -- is platform-neutral and lives
# in ``test_acp_frame_record_windows.py`` so it runs on every runner.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="ACP frame recorder is POSIX-only")


@pytest.fixture(autouse=True)
def _isolated_recorder(monkeypatch):
    """Every test starts with the latch clear and no writer, and ends the same way.

    Sync, by this repo's convention (the pinned pytest-asyncio does not collect
    async fixtures), so it cannot await the drain task's cancellation; the async
    tests run their body inside :func:`_live_writer` for that.
    """
    _frame_record._reset_for_tests()
    monkeypatch.delenv(_frame_record.ENV_RECORD_FRAMES, raising=False)
    _pin_acl_gate_open(monkeypatch)
    yield
    _frame_record._reset_for_tests()


def _pin_acl_gate_open(monkeypatch) -> None:
    """Run the recorder's LOGIC on any POSIX host; only the gate tests flip it back.

    Production refuses to record anywhere but Linux (``_require_acl_inspectable``:
    only Linux exposes ACLs through ``os.listxattr``). Left unpinned, that gate made
    75 tests in this module and its provider-safety sibling FAIL on every macOS dev
    box while CI (Linux) was green, a platform-dependent failure rather than a skip. The
    redaction, the owner-only modes, the symlink and hardlink refusals and the drain
    are POSIX-generic and are exactly what a developer on a Mac wants checked before
    pushing, so the gate is pinned to the platform the feature targets and, where the
    host has no ``os.listxattr`` at all, an inspector that reports no ACLs (a Linux
    filesystem without xattrs). The two tests OF the gate set ``IS_LINUX`` False
    themselves and are unaffected.
    """
    monkeypatch.setattr(_frame_record.platform_compat, "IS_LINUX", True)
    if not hasattr(os, "listxattr"):
        monkeypatch.setattr(_frame_record.os, "listxattr", lambda *_a, **_k: [], raising=False)


@asynccontextmanager
async def _live_writer():
    """Await the drain task's cancellation on the way out, even when the body fails.

    An ``async with`` helper rather than an ``@pytest_asyncio.fixture``, by this
    repo's convention. The teardown has to await: a task still pending when the
    test's loop closes is reported by pytest as destroyed, and its executor write
    can still be touching the test's directory. ``try/finally`` so an assertion
    failure in the body does not skip it.
    """
    try:
        yield
    finally:
        await _frame_record.stop_for_tests()


def _clean(monkeypatch):
    """Kept as an explicit call at the top of each test for readability."""
    _frame_record._reset_for_tests()
    monkeypatch.delenv(_frame_record.ENV_RECORD_FRAMES, raising=False)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ── off by default ───────────────────────────────────────────────────────────


def test_unset_env_writes_nothing(monkeypatch):
    """The state of every ordinary run and every CI run."""
    _clean(monkeypatch)
    assert _frame_record.recording_destination() == ""


def test_blank_env_is_off(monkeypatch):
    """A blank value is off, not a directory named "" in the cwd."""
    _clean(monkeypatch)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, "   ")
    assert _frame_record.recording_destination() == ""


def test_a_blank_destination_is_refused_not_resolved_to_the_cwd(monkeypatch, tmp_path):
    """Path("") is the CWD, so a blank dest would write into the checkout.

    pytest's CWD is the repository root, which is how a stray kas.jsonl once
    appeared there.
    """
    _clean(monkeypatch)
    monkeypatch.chdir(tmp_path)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, "")
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, "   ")
    assert list(tmp_path.iterdir()) == [], "a blank destination wrote a file"


@pytest.mark.asyncio
async def test_record_frame_does_not_touch_the_disk_when_off(monkeypatch):
    """The async entry point must return before doing any work."""
    _clean(monkeypatch)
    async with _live_writer():
        calls: list[tuple] = []
        monkeypatch.setattr(_frame_record, "write_frame", lambda *a: calls.append(a))
        await _frame_record.record_frame("kas", {"jsonrpc": "2.0"})
        assert calls == []


# ── off the event loop when on ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_frame_offloads_the_write_off_the_event_loop(monkeypatch, tmp_path):
    """A filesystem syscall on a reader loop stalls every session on it.

    The write must reach a worker thread, so this asserts it runs on a
    DIFFERENT thread than the loop -- not merely that the bytes landed.
    """
    _clean(monkeypatch)
    async with _live_writer():
        monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
        _frame_record.start_recorder()
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real_write = _frame_record.write_frame

        def spy(backend, frame, dest):
            seen.append(threading.get_ident())
            real_write(backend, frame, dest)

        monkeypatch.setattr(_frame_record, "write_frame", spy)
        await _frame_record.record_frame("kas", {"jsonrpc": "2.0", "id": 1})
        await _frame_record.flush_for_tests()
        assert seen, "the write never ran"
        assert seen[0] != loop_thread, "the write ran on the event loop thread"
        assert json.loads((tmp_path / "kas.jsonl").read_text(encoding="utf-8"))["id"] == 1


# ── what lands on disk ───────────────────────────────────────────────────────


def test_a_frame_lands_in_the_backend_file(monkeypatch, tmp_path):
    _clean(monkeypatch)
    dest = str(tmp_path / "frames")
    _frame_record.write_frame(ACP_BACKEND_KIRO, {"jsonrpc": "2.0", "id": 1}, dest)
    _frame_record.write_frame(ACP_BACKEND_KIRO, {"jsonrpc": "2.0", "id": 2}, dest)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0", "id": 3}, dest)

    # kiro-cli's id is "", so its file is named through POLICY_ID_BY_BACKEND.
    kiro_file = Path(dest) / "kiro.jsonl"
    assert kiro_file.exists(), sorted(p.name for p in Path(dest).iterdir())
    lines = kiro_file.read_text(encoding="utf-8").splitlines()
    assert [json.loads(ln)["id"] for ln in lines] == [1, 2]
    assert json.loads((Path(dest) / "kas.jsonl").read_text(encoding="utf-8"))["id"] == 3


def test_a_credential_is_scrubbed_before_it_is_written(monkeypatch, tmp_path):
    _clean(monkeypatch)
    secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyzAB"
    _frame_record.write_frame("kas", {"params": {"nested": [{"token": secret}]}}, str(tmp_path))
    written = (tmp_path / "kas.jsonl").read_text(encoding="utf-8")
    assert secret not in written
    assert "REDACTED" in written


def test_the_recording_home_directory_is_scrubbed(monkeypatch, tmp_path):
    _clean(monkeypatch)
    home = tmp_path / "home" / "someone"
    home.mkdir(parents=True)
    monkeypatch.setattr(_frame_record.Path, "home", staticmethod(lambda: home))
    _frame_record.write_frame("kas", {"params": {"path": f"{home}/notes.md"}}, str(tmp_path))
    written = (tmp_path / "kas.jsonl").read_text(encoding="utf-8")
    assert str(home) not in written
    assert "~/notes.md" in written


def test_a_sibling_of_the_home_directory_is_not_mangled(monkeypatch, tmp_path):
    """``$HOME=/home/al`` must not turn ``/home/alice/x`` into ``~ice/x``.

    A bare substring replace matched the prefix; only a whole path component
    (followed by a separator or the end of the string) is ``$HOME``."""
    _clean(monkeypatch)
    home = tmp_path / "home" / "al"
    home.mkdir(parents=True)
    sibling = tmp_path / "home" / "alice" / "x"
    monkeypatch.setattr(_frame_record.Path, "home", staticmethod(lambda: home))
    frame = {"params": {"mine": f"{home}/notes.md", "theirs": str(sibling), "bare": str(home)}}
    written = json.loads(_frame_record.scrub_frame(frame))["params"]
    assert written == {"mine": "~/notes.md", "theirs": str(sibling), "bare": "~"}, written


def test_a_credential_nested_under_a_header_key_is_still_redacted(monkeypatch, tmp_path):
    """``{"Authorization": ["Bearer …"]}``: the token sits in a list, so it is
    not a dict value and a bare ``Bearer <opaque>`` is not a credential pattern
    on its own. The enclosing key must travel down into the container."""
    _clean(monkeypatch)
    token = "Bearer sk-nested-0123456789abcdef0123456789abcdef"
    frame = {"headers": {"Authorization": [token, [token]]}}
    out = json.loads(_frame_record.scrub_frame(frame))
    assert token not in json.dumps(out), out
    assert out["headers"]["Authorization"][0].startswith("[REDACTED"), out
    assert out["headers"]["Authorization"][1][0].startswith("[REDACTED"), out


def test_a_credential_under_a_nested_dict_keeps_the_outer_key_context(monkeypatch):
    """``{"Authorization": {"value": "Bearer …"}}``: the inner dict's own key
    (``value``) is not credential-shaped. It must ADD to the ancestor context,
    not replace it, or the token is written verbatim."""
    _clean(monkeypatch)
    token = "Bearer sk-inner-0123456789abcdef0123456789abcdef"
    frame = {"headers": {"Authorization": {"value": token, "items": [{"v": token}]}}}
    out = json.loads(_frame_record.scrub_frame(frame))
    assert token not in json.dumps(out), out
    assert out["headers"]["Authorization"]["value"].startswith("[REDACTED"), out
    assert out["headers"]["Authorization"]["items"][0]["v"].startswith("[REDACTED"), out
    # Unrelated siblings are not over-redacted by the inherited context.
    plain = json.loads(_frame_record.scrub_frame({"headers": {"X-Trace": "hello"}}))
    assert plain == {"headers": {"X-Trace": "hello"}}, plain


@pytest.mark.parametrize(
    "key, value",
    [
        ("Authorization", "Basic dXNlcjpwYXNzd29yZA=="),
        ("authorization", "Digest username=al, response=abc"),
        ("Proxy-Authorization", "Basic dXNlcjpwYXNz"),
        ("Cookie", "session=opaque-not-a-known-shape"),
        ("Set-Cookie", "sid=abc; HttpOnly"),
        ("X-Api-Key", "not-a-recognised-shape"),
        ("api_key", "plainlooking"),
        ("password", "hunter2"),
        ("client_secret", "shortsecret"),
        ("refresh_token", "opaque"),
        ("accessToken", "short-opaque-value"),
        ("refreshToken", "opaque"),
        ("clientSecret", "opaque"),
        ("_authToken", "opaque"),
        ("apiKey", "opaque"),
        ("ACCESS-TOKEN", "opaque"),
        ("SLACK_BOT_TOKEN", "opaque"),
        ("GITHUB_ENTERPRISE_TOKEN", "opaque"),
        ("X-Internal-Secret", "opaque"),
        ("dbPassword", "opaque"),
        ("JIRA_API_TOKEN", "opaque"),
        ("consumerSecret", "opaque"),
        ("apiSecret", "opaque"),
        ("authorizationToken", "opaque"),
        ("otp", "482913"),
        ("pin", "1234"),
        ("verificationCode", "482913"),
        ("authCode", "opaque"),
        ("mfa_code", "482913"),
        ("recovery_code", "opaque"),
        ("_auth", "dXNlcjpwYXNzd29yZA=="),
        ("auth", "dXNlcjpwYXNzd29yZA=="),
        ("basic_auth", "opaque"),
        ("proxyAuth", "Basic dXNlcjpwYXNz"),
        ("registry_auth", "opaque"),
    ],
)
def test_a_value_under_a_credential_bearing_key_is_redacted_whatever_its_shape(
    monkeypatch, key, value
):
    """The pattern redactor knows credential SHAPES; ``Authorization: Basic
    <base64>`` has none it recognises and is the password, reversibly. Under a
    key that names a credential the value is replaced by the key alone, at any
    depth, whatever it looks like."""
    _clean(monkeypatch)
    frame = {"headers": {key: value}, "deep": {key: {"v": [value]}}}
    out = json.loads(_frame_record.scrub_frame(frame))
    assert value not in json.dumps(out), out
    assert out["headers"][key] == "[REDACTED: credential]", out
    assert out["deep"][key]["v"][0] == "[REDACTED: credential]", out


def test_an_oauth_configuration_block_is_not_redacted(monkeypatch):
    """``oauth`` ends in ``auth`` but is a configuration block, not a blob:
    its client id, issuer and scopes are what a replay needs. Only the
    secret-shaped leaves inside it are touched."""
    _clean(monkeypatch)
    frame = {
        "oauth": {
            "clientId": "abc-123",
            "issuer": "https://issuer.example",
            "scopes": ["openid", "email"],
            "clientSecret": "s3cr3t",
        }
    }
    out = json.loads(_frame_record.scrub_frame(frame))
    assert out["oauth"]["clientId"] == "abc-123", out
    assert out["oauth"]["issuer"] == "https://issuer.example", out
    assert out["oauth"]["scopes"] == ["openid", "email"], out
    assert out["oauth"]["clientSecret"] == "[REDACTED: credential]", out


@pytest.mark.parametrize(
    "name_field, name, value",
    [
        ("name", "Authorization", "Basic dXNlcjpwYXNz"),
        ("key", "X-Api-Key", "not-a-shape"),
        ("header", "Cookie", "sid=opaque"),
        ("name", "password", "hunter2"),
        ("Name", "SLACK_BOT_TOKEN", "opaque"),
    ],
)
def test_a_name_value_pair_naming_a_credential_is_redacted(monkeypatch, name_field, name, value):
    """Header lists arrive as ``[{"name": ..., "value": ...}]``: the credential
    key is a VALUE, so the walk must borrow it as context for the siblings.
    The name itself is kept so the recording still says which header it was."""
    _clean(monkeypatch)
    frame = {"headers": [{name_field: name, "value": value, "enabled": True, "source": "cfg"}]}
    out = json.loads(_frame_record.scrub_frame(frame))
    entry = out["headers"][0]
    assert entry[name_field] == name, entry
    assert entry["value"] == "[REDACTED: credential]", entry
    # Metadata siblings are not the secret and must survive intact.
    assert entry["enabled"] is True and entry["source"] == "cfg", entry
    assert value not in json.dumps(out), out


@pytest.mark.parametrize(
    "pair",
    [
        ["Authorization", "Basic dXNlcjpwYXNz"],
        ["X-Api-Key", "not-a-shape"],
        ["password", 123456],
    ],
)
def test_a_two_item_name_value_array_naming_a_credential_is_redacted(monkeypatch, pair):
    """``headers: [["Authorization", "Basic ..."]]`` is the other common
    encoding: the first item names the header, the second is its value."""
    _clean(monkeypatch)
    out = json.loads(_frame_record.scrub_frame({"headers": [pair, ["Accept", "text/plain"]]}))
    assert out["headers"][0] == [pair[0], "[REDACTED: credential]"], out
    assert out["headers"][1] == ["Accept", "text/plain"], out
    assert str(pair[1]) not in json.dumps(out), out


@pytest.mark.parametrize(
    "second, secret",
    [
        (["Basic dXNlcjpwYXNz"], "dXNlcjpwYXNz"),
        ({"value": "Basic dXNlcjpwYXNz"}, "dXNlcjpwYXNz"),
        ([["nested", "opaque-blob"]], "opaque-blob"),
    ],
)
def test_a_two_item_pair_with_a_container_value_is_still_redacted(monkeypatch, second, secret):
    """``["Authorization", ["Basic ..."]]``: the name lends its context to a
    container second item too, so every leaf inside it is judged under the
    credential name rather than escaping because of its type."""
    _clean(monkeypatch)
    out = json.loads(_frame_record.scrub_frame({"headers": [["Authorization", second]]}))
    assert out["headers"][0][0] == "Authorization", out
    assert secret not in json.dumps(out), out
    assert "[REDACTED: credential]" in json.dumps(out), out


@pytest.mark.parametrize(
    "frame",
    [
        {"Authorization": {"Basic dXNlcjpwYXNz": True}},
        {"Authorization": {"nested": {"Basic dXNlcjpwYXNz": 1}}},
        {"headers": [{"name": "Authorization", "value": {"Basic dXNlcjpwYXNz": True}}]},
        {"headers": [["Authorization", {"n": {"Basic dXNlcjpwYXNz": 1}}]]},
    ],
)
def test_a_credential_encoded_as_an_object_key_under_a_credential_name_is_redacted(
    monkeypatch, frame
):
    """A dict KEY is a string leaf too. Under an inherited credential context
    the secret may be the key itself, so keys are judged against the enclosing
    chain the same way values are -- through every structural wrapper."""
    _clean(monkeypatch)
    out = _frame_record.scrub_frame(frame)
    json.loads(out)
    assert "dXNlcjpwYXNz" not in out, out
    assert "[REDACTED: credential]" in out, out


def test_a_key_under_a_credential_name_is_judged_against_the_chain_not_itself(monkeypatch):
    """``value`` under ``Authorization`` is a key NAME: it stays legible while
    the secret it points at is replaced, so the recording still says where the
    header value lived."""
    _clean(monkeypatch)
    out = json.loads(_frame_record.scrub_frame({"Authorization": {"value": "Basic dXNlcjpwYXNz"}}))
    assert out == {"Authorization": {"value": "[REDACTED: credential]"}}, out


def test_many_keys_collapsing_to_one_spelling_cost_linear_work(monkeypatch):
    """A frame whose every key is a secret under ``Authorization`` collapses
    them all to ``[REDACTED: credential]``; the ``#n`` suffixing must not
    rescan every taken suffix per key, or such a frame pins the writer.
    Rescanning from ``#2`` each time is quadratic (about n*n/2 probes: ~2e8
    for this frame, tens of seconds); one probe per key is well under a
    second even on a slow CI box."""
    _clean(monkeypatch)
    n = 20000
    frame = {"Authorization": {f"Basic blob{i} {i}": i for i in range(n)}}
    started = time.monotonic()
    out = json.loads(_frame_record.scrub_frame(frame))
    elapsed = time.monotonic() - started
    inner = out["Authorization"]
    assert len(inner) == n, len(inner)
    assert all(k.startswith("[REDACTED: credential]") for k in inner), list(inner)[:3]
    assert elapsed < 5.0, elapsed


def test_a_name_value_pair_naming_a_plain_field_is_not_redacted(monkeypatch):
    """Only a credential-bearing name lends context; ``Accept`` does not."""
    _clean(monkeypatch)
    frame = {"headers": [{"name": "Accept", "value": "text/plain"}, {"name": "count", "value": 3}]}
    out = json.loads(_frame_record.scrub_frame(frame))
    assert out == frame, out


def test_a_credential_bearing_key_does_not_redact_its_siblings(monkeypatch):
    """The key-name rule applies to the value UNDER the key, not to its
    neighbours: a request that carries an ``Authorization`` header still
    records its method and path."""
    _clean(monkeypatch)
    frame = {"method": "GET", "headers": {"Authorization": "Basic x", "Accept": "text/plain"}}
    out = json.loads(_frame_record.scrub_frame(frame))
    assert out["method"] == "GET" and out["headers"]["Accept"] == "text/plain", out
    assert out["headers"]["Authorization"] == "[REDACTED: credential]", out


@pytest.mark.parametrize(
    "key",
    [
        "tokenizer",
        "tokens",
        "secretary",
        "passwords_hint",
        "cookieJar",
        "inputTokens",
        "outputTokens",
        "max_tokens",
        "context_window_tokens",
        "cachedReadTokens",
        "NextToken",
        "max_token",
        "stop_token",
        "authenticated",
        "author",
        "auth_method",
        "authorized",
        "oauth",
        "oauth2",
        "preauth",
        "requires_auth",
    ],
)
def test_a_key_that_merely_contains_a_credential_word_is_not_redacted(monkeypatch, key):
    """The key-name rule is a SUFFIX match on the canonical spelling. Usage
    counters are plural (``inputTokens``) and pagination cursors and model
    settings are named out (``NextToken``, ``max_token``); ``tokenizer`` and
    ``secretary`` never end in the noun. All are ordinary fields."""
    _clean(monkeypatch)
    out = json.loads(_frame_record.scrub_frame({key: "plain", "usage": {key: 42}}))
    assert out == {key: "plain", "usage": {key: 42}}, out


@pytest.mark.parametrize("value", [123456, 4.2, 0, True])
def test_a_numeric_value_under_a_credential_key_is_redacted(monkeypatch, value):
    """A PIN or an all-digit token is a number after JSON decoding; the type
    says nothing about whether it is secret. Only the key does."""
    _clean(monkeypatch)
    frame = {"password": value, "otp": value, "auth": {"pin_token": [value]}}
    out = json.loads(_frame_record.scrub_frame(frame))
    assert out["password"] == "[REDACTED: credential]", out
    assert out["otp"] == "[REDACTED: credential]", out
    assert out["auth"]["pin_token"][0] == "[REDACTED: credential]", out


@pytest.mark.parametrize("key", ["code", "status_code", "exit_code", "language_code", "zip_code"])
def test_a_bare_code_field_is_not_a_credential(monkeypatch, key):
    """Only the qualified one-time-code names are credentials; ``code`` on its
    own is a status, an exit code, a locale."""
    _clean(monkeypatch)
    out = json.loads(_frame_record.scrub_frame({key: 200}))
    assert out == {key: 200}, out


def test_a_credential_in_a_header_map_leaves_valid_json(monkeypatch, tmp_path):
    """Redaction is per string leaf, never over the SERIALIZED frame: a pattern
    spanning key, quote, colon and value would collapse ``"Authorization":
    "Bearer …"`` to a single token and leave a line that does not parse. Per leaf,
    the header key survives, the value is redacted, and the corpus loader can read
    the line."""
    _clean(monkeypatch)
    frame = {
        "params": {
            "rawInput": {
                "headers": {"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"}
            }
        }
    }
    _frame_record.write_frame("kas", frame, str(tmp_path))
    line = (tmp_path / "kas.jsonl").read_text(encoding="utf-8").splitlines()[0]
    parsed = json.loads(line)  # must not raise
    headers = parsed["params"]["rawInput"]["headers"]
    assert "Authorization" in headers, headers
    assert "abcdefghijklmnop" not in line
    assert "REDACTED" in headers["Authorization"]


def test_every_known_backend_maps_to_a_usable_file_stem():
    names = {_frame_record.fixture_dir_name(b) for b in ACP_BACKENDS_KNOWN}
    assert len(names) == len(ACP_BACKENDS_KNOWN), f"two backends share a file: {names}"
    for name in names:
        assert name
        assert "/" not in name and "\\" not in name and name not in (".", "..")


# ── owner-only on disk ───────────────────────────────────────────────────────


def test_a_fresh_recording_is_created_owner_only(monkeypatch, tmp_path):
    """A recording holds redacted-but-not-guaranteed-clean transcripts.

    The first version of this recorder used a bare ``mkdir()`` and ``open("a")``,
    so a recording took the umask default -- 0o755 / 0o644 on most hosts -- and
    any other local user could read a developer's transcripts. Passing the mode
    to ``mkdir`` and ``os.open`` closes that on a fresh path; the pre-existing
    path is the next test.
    """
    _clean(monkeypatch)
    old = os.umask(0o022)
    try:
        dest = tmp_path / "frames"
        _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(dest))
    finally:
        os.umask(old)
    assert _mode(dest) == _frame_record.DIR_MODE, oct(_mode(dest))
    assert _mode(dest / "kas.jsonl") == _frame_record.FILE_MODE, oct(_mode(dest / "kas.jsonl"))


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o705], ids=["world", "group-only", "other-only"])
def test_a_pre_existing_shared_directory_is_refused_not_tightened(monkeypatch, tmp_path, mode):
    """The env var may name a directory the operator shares on purpose.

    An earlier revision ``fchmod``ed every destination to 0700 on the first
    frame, which locked every other intended user out of a shared directory
    with no undo. A directory this run did not create is checked and refused,
    never changed: its mode must survive and nothing may be written into it.
    """
    _clean(monkeypatch)
    dest = tmp_path / "frames"
    dest.mkdir(mode=mode)
    dest.chmod(mode)

    _frame_record.write_frame("kas", {"jsonrpc": "2.0", "id": 1}, str(dest))

    assert _mode(dest) == mode, "a pre-existing directory was re-moded"
    assert list(dest.iterdir()) == [], "a frame was written into a shared directory"
    assert _frame_record.recording_destination() == "", "a refused destination must stand down"


def _fake_listxattr(names_by_fd_path):
    """Build an ``os.listxattr`` stand-in keyed on the inode of the descriptor.

    ``setfacl`` needs a filesystem that stores ACLs and a helper binary; a test
    cannot rely on either, so the xattr view is faked at the ``os`` boundary
    and keyed by the ``(st_dev, st_ino)`` of the descriptor asked about.
    """

    def listxattr(fd):
        info = os.fstat(fd)
        return list(names_by_fd_path.get((info.st_dev, info.st_ino), ()))

    return listxattr


def _inode(path):
    info = os.stat(path)
    return (info.st_dev, info.st_ino)


@pytest.mark.parametrize("xattr", ["system.posix_acl_access", "system.posix_acl_default"])
def test_a_pre_existing_owner_only_directory_with_an_acl_is_refused(monkeypatch, tmp_path, xattr):
    """``mode & 0o077 == 0`` says nothing about a named ACL entry: a 0700
    directory with ``setfacl -m u:other:rx`` reads owner-only and is not. A
    default ACL is worse, since every file created inside inherits it."""
    _clean(monkeypatch)
    dest = tmp_path / "frames"
    dest.mkdir(mode=0o700)
    dest.chmod(0o700)
    monkeypatch.setattr(_frame_record.os, "listxattr", _fake_listxattr({_inode(dest): [xattr]}))
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(dest))
    assert list(dest.iterdir()) == [], "a frame was written into an ACL-shared directory"
    assert _frame_record.recording_destination() == ""


def test_a_directory_created_by_this_run_that_inherited_an_acl_is_refused(monkeypatch, tmp_path):
    """fchmod 0700 on the leaf does not strip the default ACL it inherited from
    its parent, so a created leaf is checked too, not only a pre-existing one."""
    _clean(monkeypatch)
    dest = tmp_path / "frames"
    seen = {}

    def listxattr(fd):
        info = os.fstat(fd)
        if dest.exists() and (info.st_dev, info.st_ino) == _inode(dest):
            seen["leaf"] = True
            return ["system.posix_acl_access"]
        return []

    monkeypatch.setattr(_frame_record.os, "listxattr", listxattr)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(dest))
    assert seen.get("leaf"), "the created leaf was never asked about its ACL"
    assert not dest.exists() or list(dest.iterdir()) == []
    assert _frame_record.recording_destination() == ""


def test_a_recording_file_that_carries_an_acl_is_refused(monkeypatch, tmp_path):
    """The file descriptor is checked after fchmod: a pre-existing
    ``kas.jsonl`` given an ACL by hand must not receive another frame."""
    _clean(monkeypatch)
    dest = tmp_path / "frames"
    dest.mkdir(mode=0o700)
    dest.chmod(0o700)
    existing = dest / "kas.jsonl"
    existing.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        _frame_record.os,
        "listxattr",
        _fake_listxattr({_inode(existing): ["system.posix_acl_access"]}),
    )
    existing.chmod(0o640)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0", "id": 1}, str(dest))
    assert existing.read_text(encoding="utf-8") == "", "a frame landed in an ACL-shared file"
    # Refused means left as found: fchmod on an ACL-bearing file rewrites its
    # mask entry, so the tightening must not run before the check.
    assert _mode(existing) == 0o640, oct(_mode(existing))
    assert _frame_record.recording_destination() == ""


def test_a_filesystem_without_xattrs_has_no_acls_and_is_accepted(monkeypatch, tmp_path):
    """tmpfs mounted without xattr support answers ENOTSUP; that is a
    filesystem with no ACLs at all, not one hiding them."""
    _clean(monkeypatch)

    def listxattr(fd):
        raise OSError(errno.ENOTSUP, "not supported")

    monkeypatch.setattr(_frame_record.os, "listxattr", listxattr)
    dest = tmp_path / "frames"
    _frame_record.write_frame("kas", {"jsonrpc": "2.0", "id": 1}, str(dest))
    assert (dest / "kas.jsonl").exists()
    assert not _frame_record._stood_down, "an ACL-less filesystem must not stand the recorder down"


def test_start_recorder_starts_no_thread_where_acls_cannot_be_inspected(monkeypatch, tmp_path):
    """macOS passes the POSIX gate but not the ACL one: ``start_recorder`` must
    stand down before starting the writer and notifier threads, not after."""
    _clean(monkeypatch)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    monkeypatch.setattr(_frame_record.platform_compat, "IS_LINUX", False)
    started = []
    real_start = threading.Thread.start

    def spy(self, *a, **kw):
        started.append(self.name)
        return real_start(self, *a, **kw)

    monkeypatch.setattr(threading.Thread, "start", spy)
    assert _frame_record.start_recorder() is False
    assert started == [], started
    assert _frame_record.recording_destination() == ""


def test_a_platform_that_cannot_inspect_acls_stands_down(monkeypatch, tmp_path, caplog):
    """macOS stores ACLs behind acl_get_file with no os binding: the recorder
    cannot prove owner-only there and refuses, naming the reason once."""
    _clean(monkeypatch)
    monkeypatch.setattr(_frame_record.platform_compat, "IS_LINUX", False)
    dest = tmp_path / "frames"
    with caplog.at_level(logging.WARNING):
        _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(dest))
    assert not dest.exists() or list(dest.iterdir()) == []
    assert _frame_record.recording_destination() == ""
    assert "Linux-only" in caplog.text


def test_a_pre_existing_directory_owned_by_another_user_is_refused(monkeypatch, tmp_path):
    """0700 is not enough: an owner-only directory owned by SOMEONE ELSE is
    theirs, not ours, and writing a recording into it (or reading one out of
    it) is the wrong side of the boundary. A test cannot create a directory
    owned by another uid without privilege, so the recorder's idea of "this
    user" is moved instead."""
    _clean(monkeypatch)
    dest = tmp_path / "frames"
    dest.mkdir(mode=0o700)
    dest.chmod(0o700)
    monkeypatch.setattr(_frame_record.platform_compat, "local_user_id", lambda: -1)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(dest))
    assert list(dest.iterdir()) == [], "a frame was written into another user's directory"
    assert _frame_record.recording_destination() == ""


def test_a_pre_existing_owner_only_directory_is_reused_and_a_loose_file_tightened(
    monkeypatch, tmp_path
):
    """An owner-only directory from an earlier run is the normal second-session
    case: the append lands there, and a loose file inside (an earlier run under
    a wide umask) is still tightened on its descriptor, since the file is the
    recorder's own artefact rather than the operator's directory."""
    _clean(monkeypatch)
    dest = tmp_path / "frames"
    dest.mkdir(mode=0o700)
    dest.chmod(0o700)
    existing = dest / "kas.jsonl"
    existing.write_text('{"jsonrpc": "2.0", "id": 0}\n', encoding="utf-8")
    existing.chmod(0o644)

    _frame_record.write_frame("kas", {"jsonrpc": "2.0", "id": 1}, str(dest))

    assert _mode(existing) == _frame_record.FILE_MODE, oct(_mode(existing))
    ids = [json.loads(ln)["id"] for ln in existing.read_text(encoding="utf-8").splitlines()]
    assert ids == [0, 1], "tightening must append, not truncate"


# ── never waits on the reader loop ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_frame_returns_while_the_writer_is_wedged(monkeypatch, tmp_path):
    """A slow disk or a saturated shared executor must not hold a frame back.

    The reader loop routes a frame only after ``record_frame`` returns, so if
    that awaited the write, a wedged destination would stall every multiplexed
    session until the liveness watchdog killed the process. The write is queued
    instead; this holds the worker on a gate and shows the call still returns.
    """
    _clean(monkeypatch)
    async with _live_writer():
        monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
        _frame_record.start_recorder()
        gate = threading.Event()
        real_write = _frame_record.write_frame

        def slow(backend, frame, dest):
            gate.wait(timeout=10)
            real_write(backend, frame, dest)

        monkeypatch.setattr(_frame_record, "write_frame", slow)
        try:
            for i in range(3):
                await asyncio.wait_for(
                    _frame_record.record_frame("kas", {"jsonrpc": "2.0", "id": i}), timeout=0.5
                )
        finally:
            gate.set()
        await asyncio.wait_for(_frame_record.flush_for_tests(), timeout=10)
        ids = [json.loads(ln)["id"] for ln in (tmp_path / "kas.jsonl").read_text().splitlines()]
        assert ids == [0, 1, 2], "frames must land in arrival order"


@pytest.mark.asyncio
async def test_a_full_queue_drops_and_stands_down_instead_of_waiting(monkeypatch, tmp_path):
    _clean(monkeypatch)
    async with _live_writer():
        monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
        monkeypatch.setattr(_frame_record, "QUEUE_LIMIT", 2)  # before the queue is built
        _frame_record.start_recorder()
        gate = threading.Event()
        monkeypatch.setattr(_frame_record, "write_frame", lambda *a: gate.wait(timeout=10))
        try:
            for i in range(4):
                await asyncio.wait_for(_frame_record.record_frame("kas", {"id": i}), timeout=0.5)
        finally:
            gate.set()
        assert _frame_record.recording_destination() == "", "an overflow must stand recording down"


@pytest.mark.asyncio
async def test_a_burst_of_large_frames_is_bounded_by_bytes_not_count(monkeypatch, tmp_path):
    """The transports admit frames of up to 10 MiB, so a count-only bound would
    let a burst hold gigabytes of parsed JSON behind one wedged writer.

    The byte bound counts the WIRE length the reader loop already has, not a
    re-serialization on the loop. Three 1 KiB "frames" under a 2.5 KiB cap:
    the third must be dropped and stand recording down, well under
    ``QUEUE_LIMIT``.
    """
    _clean(monkeypatch)
    async with _live_writer():
        monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
        _frame_record.start_recorder()
        monkeypatch.setattr(_frame_record, "QUEUE_BYTES_LIMIT", 2560)
        gate = threading.Event()
        monkeypatch.setattr(_frame_record, "write_frame", lambda *a: gate.wait(timeout=10))
        try:
            for i in range(3):
                await asyncio.wait_for(
                    _frame_record.record_frame("kas", {"id": i}, wire_bytes=1024), timeout=0.5
                )
                if i < 2:
                    assert _frame_record.recording_destination() != "", f"frame {i} was refused"
        finally:
            gate.set()
        assert _frame_record.recording_destination() == "", "the byte bound did not trip"


@pytest.mark.asyncio
async def test_record_frame_never_waits_on_the_byte_budget_lock(monkeypatch, tmp_path):
    """The budget lock is shared with the drain thread. If its holder is
    preempted, a reader loop must not block behind it: the frame is dropped,
    recording stands down, and ``record_frame`` returns promptly."""
    _clean(monkeypatch)
    async with _live_writer():
        monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
        assert _frame_record.start_recorder()
        writer = _frame_record._writer
        assert writer is not None
        # Hold the lock for the whole call, as a preempted holder would.
        assert writer.lock.acquire(blocking=False)
        try:
            started = time.monotonic()
            await _frame_record.record_frame("kas", {"jsonrpc": "2.0", "method": "x"}, 32)
            elapsed = time.monotonic() - started
        finally:
            writer.lock.release()
        assert elapsed < 0.5, f"record_frame waited {elapsed:.3f}s on the budget lock"
        assert _frame_record.recording_destination() == "", "a busy lock must stand down, not wait"
        assert not writer.items, "the frame must be dropped, not queued behind the lock"


@pytest.mark.asyncio
async def test_a_reader_side_stand_down_is_logged_from_the_writer_thread(monkeypatch, tmp_path):
    """An overflow is detected on the reader loop. The log line for it must
    NOT be emitted there -- ``logger.warning`` takes the handler lock and runs
    the handlers -- but from the drain thread, shortly after."""
    _clean(monkeypatch)
    async with _live_writer():
        monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
        assert _frame_record.start_recorder()
        emitted: list[str] = []
        logged = threading.Event()

        def spy(*_a, **_k):
            emitted.append(threading.current_thread().name)
            logged.set()

        monkeypatch.setattr(_frame_record.logger, "warning", spy)
        monkeypatch.setattr(_frame_record, "QUEUE_LIMIT", 0)  # every admission overflows
        await _frame_record.record_frame("kas", {"jsonrpc": "2.0"}, 16)
        assert _frame_record.recording_destination() == "", "the overflow did not stand down"
        assert emitted == [], f"the reader loop emitted the log line itself: {emitted}"
        assert await asyncio.to_thread(logged.wait, 5), "the notifier never emitted the line"
        assert emitted == ["acp-frame-recorder-notify"], emitted


@pytest.mark.asyncio
async def test_a_reader_side_stand_down_is_logged_even_when_the_writer_is_wedged(
    monkeypatch, tmp_path
):
    """A hung filesystem holds the writer inside ``write`` forever. The one
    warning that says frames are now being dropped must not wait behind it:
    it is emitted by a thread that never touches the file."""
    _clean(monkeypatch)
    async with _live_writer():
        monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
        assert _frame_record.start_recorder()
        wedged = threading.Event()
        release = threading.Event()

        def hung(*_a):
            wedged.set()
            release.wait(timeout=30)

        monkeypatch.setattr(_frame_record, "write_frame", hung)
        emitted: list[str] = []
        logged = threading.Event()

        def spy(*_a, **_k):
            emitted.append(threading.current_thread().name)
            logged.set()

        monkeypatch.setattr(_frame_record.logger, "warning", spy)
        try:
            await _frame_record.record_frame("kas", {"id": 0}, 16)
            assert await asyncio.to_thread(wedged.wait, 5), "the writer never took the frame"
            monkeypatch.setattr(_frame_record, "QUEUE_LIMIT", 0)
            await _frame_record.record_frame("kas", {"id": 1}, 16)
            assert _frame_record.recording_destination() == "", "the overflow did not stand down"
            assert await asyncio.to_thread(
                logged.wait, 5
            ), "warning waited behind the wedged writer"
            assert emitted == ["acp-frame-recorder-notify"], emitted
        finally:
            release.set()


@pytest.mark.asyncio
async def test_queued_bytes_are_released_as_frames_are_written(monkeypatch, tmp_path):
    """The bound is on the BACKLOG, not a lifetime total: a recording of any
    length must fit as long as the writer keeps up."""
    _clean(monkeypatch)
    async with _live_writer():
        monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
        _frame_record.start_recorder()
        monkeypatch.setattr(_frame_record, "QUEUE_BYTES_LIMIT", 2560)
        for i in range(6):
            await _frame_record.record_frame("kas", {"jsonrpc": "2.0", "id": i}, wire_bytes=1024)
            await _frame_record.flush_for_tests()
        assert _frame_record.recording_destination() != "", "released bytes were still counted"
        ids = [json.loads(ln)["id"] for ln in (tmp_path / "kas.jsonl").read_text().splitlines()]
        assert ids == list(range(6))


def test_the_lockdown_is_applied_to_the_opened_descriptor_not_the_path(monkeypatch, tmp_path):
    """A path-based chmod after the open protects whatever the path names NOW.

    If another local user swaps the parent between the directory check and the
    open, the descriptor holds their file while the path has been restored, so
    ``chmod(path)`` tightens the wrong inode and the frame lands attacker-
    readable. ``fchmod`` on the descriptor cannot be redirected. This pins the
    mechanism: a chmod by path must not be what tightens the file.
    """
    _clean(monkeypatch)
    by_path: list = []
    real_chmod = os.chmod
    monkeypatch.setattr(
        os, "chmod", lambda p, m, *a, **k: (by_path.append(p), real_chmod(p, m, *a, **k))
    )
    monkeypatch.setattr(
        _frame_record.platform_compat,
        "restrict_to_owner",
        lambda p: pytest.fail(f"restrict_to_owner(path) used on POSIX for {p}"),
    )
    dest = tmp_path / "frames"
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(dest))
    target = dest / "kas.jsonl"
    assert target.exists(), "the frame was not written"
    assert _mode(target) == _frame_record.FILE_MODE, oct(_mode(target))
    assert not any(Path(p) == target for p in by_path), "the file was tightened by path"


@pytest.mark.asyncio
async def test_one_failure_logs_once_and_discards_the_backlog(monkeypatch, tmp_path, caplog):
    """After the destination breaks, the frames already queued must not each be
    retried against it -- that is up to ``QUEUE_LIMIT`` warnings for one fault."""
    _clean(monkeypatch)
    async with _live_writer():
        monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
        _frame_record.start_recorder()
        gate = threading.Event()
        attempts: list[int] = []

        def failing(backend, frame, dest):
            gate.wait(timeout=10)
            attempts.append(frame["id"])
            _frame_record._stand_down(OSError("disk gone"))

        monkeypatch.setattr(_frame_record, "write_frame", failing)
        with caplog.at_level("WARNING", logger=_frame_record.__name__):
            for i in range(5):
                await _frame_record.record_frame("kas", {"id": i}, wire_bytes=10)
            gate.set()
            await asyncio.wait_for(_frame_record.flush_for_tests(), timeout=10)
        assert attempts == [0], f"queued frames were retried after the stand-down: {attempts}"
        warnings = [r for r in caplog.records if "recording failed" in r.getMessage()]
        assert len(warnings) == 1, [r.getMessage() for r in warnings]


# ── links are not followed ───────────────────────────────────────────────────


def test_a_symlinked_destination_is_refused(monkeypatch, tmp_path):
    """The env var names where the recording lives; a link would say otherwise."""
    _clean(monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "frames"
    link.symlink_to(real, target_is_directory=True)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(link))
    assert list(real.iterdir()) == [], "the recorder wrote through a symlinked destination"
    assert _frame_record.recording_destination() == ""
    _frame_record._reset_for_tests()


def test_a_linked_destination_is_refused_before_its_target_is_touched(monkeypatch, tmp_path):
    """The leaf itself is a link to another user's directory.

    A name-based ``mkdir``/``chmod`` would follow it and tighten the target
    before any check ran. The pinned walk opens the leaf with ``O_NOFOLLOW``
    and refuses, so the target's mode and contents are untouched."""
    _clean(monkeypatch)
    real = tmp_path / "real"
    real.mkdir(mode=0o755)
    real.chmod(0o755)
    link = tmp_path / "frames"
    link.symlink_to(real, target_is_directory=True)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(link))
    assert _mode(real) == 0o755, "the link's target was modified"
    assert list(real.iterdir()) == []
    assert _frame_record.recording_destination() == ""


def test_a_planted_link_inside_the_directory_is_not_followed(monkeypatch, tmp_path):
    """Another local user plants ``kas.jsonl -> victim`` in a pre-existing dir.

    Tightening the directory does not remove the link, so the open itself must
    refuse to follow it; otherwise the append lands in the victim file.
    """
    _clean(monkeypatch)
    dest = tmp_path / "frames"
    dest.mkdir(mode=0o755)
    victim = tmp_path / "victim"
    victim.write_text("keep me\n", encoding="utf-8")
    (dest / "kas.jsonl").symlink_to(victim)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(dest))
    assert victim.read_text(encoding="utf-8") == "keep me\n", "the append followed the link"
    assert _frame_record.recording_destination() == ""
    _frame_record._reset_for_tests()


def test_a_linked_ancestor_of_the_destination_is_refused(monkeypatch, tmp_path):
    """The destination path crosses a link ABOVE the leaf: ``<link>/frames``.

    A single ``O_NOFOLLOW`` open of the leaf walks the ancestors by name and
    follows the link, so the recording lands under the link's target -- wherever
    another local user pointed it. Pinning every component by descriptor is
    what refuses it; the frame must not appear under the target.
    """
    _clean(monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    hop = tmp_path / "hop"
    hop.symlink_to(elsewhere, target_is_directory=True)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(hop / "frames"))
    assert list(elsewhere.iterdir()) == [], "the recording followed a linked ancestor"
    assert _frame_record.recording_destination() == ""


def test_a_refused_linked_component_is_named_in_the_stand_down(monkeypatch, tmp_path, caplog):
    """On macOS ``/tmp`` and ``/var`` are OS-provided links, so a destination
    under either is refused by design. The one log line has to tell the
    operator WHICH component, and that the physical path is the fix, or the
    refusal reads as a broken recorder."""
    _clean(monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    hop = tmp_path / "hop"
    hop.symlink_to(elsewhere, target_is_directory=True)
    with caplog.at_level(logging.WARNING, logger=_frame_record.logger.name):
        _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(hop / "frames"))
    text = caplog.text
    assert f"crosses a link at {hop}" in text, text
    assert _frame_record.ENV_RECORD_FRAMES in text and "physical path" in text, text


def test_an_ancestor_swapped_for_a_link_after_the_check_is_refused(monkeypatch, tmp_path):
    """The check-to-use window review found in a version that resolved the
    parent twice: ``_refuse_linked_component`` sees a real directory at
    ``<tmp>/hop``, then another local user replaces ``hop`` with a link to a
    0700 directory they own before the pinned walk runs. If the walk resolves
    the parent again it follows the link and every descriptor check passes on
    the attacker's directory. The walk must therefore pin the LEXICAL parent
    the check approved, so the swapped component fails ``O_NOFOLLOW``."""
    _clean(monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    hop = tmp_path / "hop"
    hop.mkdir(mode=0o700)
    real_check = _frame_record._refuse_linked_component

    def check_then_swap(directory):
        real_check(directory)
        hop.rmdir()
        hop.symlink_to(elsewhere, target_is_directory=True)

    monkeypatch.setattr(_frame_record, "_refuse_linked_component", check_then_swap)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(hop / "frames"))
    assert list(elsewhere.iterdir()) == [], "the walk followed an ancestor swapped after the check"
    assert _frame_record._stood_down, "a redirected destination must stand down"


def test_a_target_unlinked_after_the_open_is_refused_not_written(monkeypatch, tmp_path):
    """A rotator or cleanup that unlinks ``kas.jsonl`` between the open and the
    append leaves a descriptor onto an inode with no name: the write succeeds
    and the bytes vanish on close, silently. ``st_nlink == 0`` is the tell, and
    ``refuse_hardlink_alias`` only asks about MORE than one name, so the
    recorder has to refuse the zero case itself and stand down loudly."""
    _clean(monkeypatch)
    dest = tmp_path / "frames"
    dest.mkdir(mode=0o700)
    dest.chmod(0o700)
    target = dest / "kas.jsonl"
    real_open = os.open

    def open_then_unlink(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        if args and args[0] == "kas.jsonl" and "dir_fd" in kwargs:
            target.unlink()
        return fd

    monkeypatch.setattr(_frame_record.os, "open", open_then_unlink)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(dest))
    assert not target.exists()
    assert _frame_record._stood_down, "an unlinked target was written silently"


def test_a_failing_hardlink_helper_does_not_leak_the_descriptor(monkeypatch, tmp_path):
    """``refuse_hardlink_alias`` closes the fd itself when it REFUSES, but a
    failure inside it (its own ``fstat`` raising) leaves the fd open. The two
    need opposite cleanup; a recorder that treated both alike would either
    double-close or leak one descriptor per stand-down. Both paths must end
    with the descriptor closed exactly once."""
    _clean(monkeypatch)
    dest = tmp_path / "frames"
    dest.mkdir(mode=0o700)
    dest.chmod(0o700)
    opened = []
    real_open = os.open

    def tracking_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        if args and args[0] == "kas.jsonl":
            opened.append(fd)
        return fd

    def boom(fd, **kwargs):
        raise OSError(errno.EIO, "fstat failed")

    monkeypatch.setattr(_frame_record.os, "open", tracking_open)
    monkeypatch.setattr(_frame_record.pinned_fs, "refuse_hardlink_alias", boom)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(dest))
    assert _frame_record._stood_down
    assert len(opened) == 1
    with pytest.raises(OSError) as info:
        os.fstat(opened[0])
    assert info.value.errno == errno.EBADF, "the recording descriptor was leaked"


def test_a_recording_file_owned_by_another_user_is_refused_and_left_alone(monkeypatch, tmp_path):
    """The directory is refused when another user owns it; the file must be
    too. An owner-only directory can hold a foreign-owned inode (a bind mount,
    a file a privileged run left behind), and ``fchmod`` 0600 on it locks it to
    THAT owner, who then reads every frame appended. ``chown`` needs privilege
    a test does not have, so the uid the recorder compares against is moved
    instead; the file must be neither written nor re-moded."""
    _clean(monkeypatch)
    dest = tmp_path / "frames"
    dest.mkdir(mode=0o700)
    dest.chmod(0o700)
    existing = dest / "kas.jsonl"
    existing.write_text("", encoding="utf-8")
    existing.chmod(0o644)
    me = os.getuid()
    real_fstat = os.fstat

    def foreign_fstat(fd):
        info = real_fstat(fd)
        if (info.st_dev, info.st_ino) == _inode(existing):
            return os.stat_result(
                (
                    info.st_mode,
                    info.st_ino,
                    info.st_dev,
                    info.st_nlink,
                    me + 1,
                    info.st_gid,
                    info.st_size,
                    info.st_atime,
                    info.st_mtime,
                    info.st_ctime,
                )
            )
        return info

    monkeypatch.setattr(_frame_record.os, "fstat", foreign_fstat)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0", "id": 1}, str(dest))
    assert existing.read_text(encoding="utf-8") == "", "a frame landed in a foreign-owned file"
    assert _mode(existing) == 0o644, "a foreign-owned file was re-moded"
    assert _frame_record._stood_down


def test_a_dot_dot_component_cannot_climb_out_through_a_link(monkeypatch, tmp_path):
    """``<link>/../frames`` normalises lexically to ``<tmp>/frames``; a physical
    walk that resolved the link first would land the ``..`` inside the link's
    target instead. The lexical collapse happens BEFORE the pinned walk, so the
    frame lands where the operator's path reads, not where the link points."""
    _clean(monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    hop = tmp_path / "hop"
    hop.symlink_to(elsewhere, target_is_directory=True)
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(hop / ".." / "frames"))
    assert (
        tmp_path / "frames" / "kas.jsonl"
    ).exists(), "the frame did not land at the lexical path"
    assert list(elsewhere.iterdir()) == []


def test_concurrent_stand_downs_log_exactly_once(monkeypatch, caplog):
    """The writer thread (a failed write) and the loop thread (the overflow
    that failed write causes) can both reach ``_stand_down`` at once; the latch
    must admit exactly one of them to the log."""
    _clean(monkeypatch)
    start = threading.Barrier(8)

    def racer():
        start.wait(timeout=5)
        _frame_record._stand_down(OSError("disk gone"))

    with caplog.at_level("WARNING", logger=_frame_record.__name__):
        threads = [threading.Thread(target=racer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
    warnings = [r for r in caplog.records if "recording failed" in r.getMessage()]
    assert len(warnings) == 1, len(warnings)
    assert _frame_record.recording_destination() == ""
    _frame_record._reset_for_tests()


def test_a_fifo_planted_at_the_target_does_not_wedge_the_writer(monkeypatch, tmp_path):
    """A FIFO at ``kas.jsonl`` with no reader makes a plain ``O_WRONLY`` open
    block forever, which would park the writer thread for the life of the
    process. ``O_NONBLOCK`` makes the open return, and ``S_ISREG`` refuses it.
    The test would hang rather than fail without the flag, so it runs in a
    thread with a deadline."""
    _clean(monkeypatch)
    dest = tmp_path / "frames"
    dest.mkdir(mode=0o700)
    os.mkfifo(dest / "kas.jsonl")
    worker = threading.Thread(
        target=_frame_record.write_frame,
        args=("kas", {"jsonrpc": "2.0"}, str(dest)),
        daemon=True,  # a regression must fail the test, not wedge the pytest process
    )
    worker.start()
    worker.join(timeout=5)
    try:
        assert not worker.is_alive(), "the open blocked on the FIFO"
        assert _frame_record.recording_destination() == ""
    finally:
        if worker.is_alive():
            # Give the blocked O_WRONLY open a reader so the thread can exit
            # and pytest can remove tmp_path without a writer still inside it.
            reader = os.open(dest / "kas.jsonl", os.O_RDONLY | os.O_NONBLOCK)
            worker.join(timeout=5)
            os.close(reader)


def test_a_hard_link_planted_inside_the_directory_is_refused(monkeypatch, tmp_path):
    """A hard link at ``kas.jsonl`` is a regular file, so ``S_ISREG`` passes and
    ``O_NOFOLLOW`` has nothing to refuse -- yet it shares its inode with the
    victim, so the append (and the chmod) would land in the victim. The link
    count (``pinned_fs.refuse_hardlink_alias`` on the open descriptor) is what
    tells the two apart. The directory is owner-only so the mode check passes
    and the alias check is the one doing the refusing; with a shared directory
    this test would pass for the wrong reason."""
    _clean(monkeypatch)
    dest = tmp_path / "frames"
    dest.mkdir(mode=0o700)
    dest.chmod(0o700)
    victim = tmp_path / "victim"
    victim.write_text("keep me\n", encoding="utf-8")
    victim.chmod(0o644)
    os.link(victim, dest / "kas.jsonl")
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(dest))
    assert (
        victim.read_text(encoding="utf-8") == "keep me\n"
    ), "the append wrote through the hard link"
    assert _mode(victim) == 0o644, "the victim's mode was changed"
    assert _frame_record.recording_destination() == ""


# ── never raises ─────────────────────────────────────────────────────────────


def test_an_unwritable_destination_never_raises(monkeypatch, tmp_path):
    """A recorder fault must not reach a reader loop.

    In ``AcpRuntime._reader_loop`` an escaping exception ends EVERY multiplexed
    session on the process, so a bad path has to degrade to a log line.
    Pointing the destination at a FILE makes mkdir fail.
    """
    _clean(monkeypatch)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    _frame_record.write_frame("kas", {"jsonrpc": "2.0"}, str(blocker))  # must not raise
    assert blocker.read_text(encoding="utf-8") == ""
    assert _frame_record.recording_destination() == "", "a failure must stand recording down"
    _frame_record._reset_for_tests()


def test_an_unknown_backend_stands_down_instead_of_raising(monkeypatch, tmp_path):
    """Construction rejects an id outside ACP_BACKENDS_KNOWN, so this is a
    programming error -- and the recorder still must not kill the reader."""
    _clean(monkeypatch)
    _frame_record.write_frame("no-such-backend", {"jsonrpc": "2.0"}, str(tmp_path))
    assert list(tmp_path.iterdir()) == []
    assert _frame_record.recording_destination() == ""
    _frame_record._reset_for_tests()


def test_a_failed_notifier_start_stops_the_writer_it_already_started(monkeypatch, tmp_path):
    """``_Writer()`` starts two threads. If the second start fails, the first
    must not be left running with no handle to stop it."""
    _clean(monkeypatch)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    real_start = threading.Thread.start
    started: list[threading.Thread] = []

    def flaky(self):
        if self.name == "acp-frame-recorder-notify":
            raise RuntimeError("can't start new thread")
        started.append(self)
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", flaky)
    assert _frame_record.start_recorder() is False
    assert _frame_record.recording_destination() == ""
    assert len(started) == 1 and started[0].name == "acp-frame-recorder", started
    started[0].join(timeout=5)
    assert not started[0].is_alive(), "the writer thread outlived its failed constructor"


def test_a_stand_down_racing_the_notifier_poll_is_never_lost(monkeypatch, tmp_path):
    """The notifier polls the latch every 50 ms. A ``_stand_down`` arriving
    while a poll holds the lock must still latch: the poll must not take the
    lock when nothing is pending."""
    _clean(monkeypatch)
    taken: list[str] = []

    class SpyLock:
        def __init__(self):
            self._lock = threading.Lock()

        def acquire(self, *a, **k):
            taken.append(threading.current_thread().name)
            return self._lock.acquire(*a, **k)

        def release(self):
            self._lock.release()

        __enter__ = acquire

        def __exit__(self, *exc):
            self.release()

    monkeypatch.setattr(_frame_record, "_stand_down_lock", SpyLock())
    for _ in range(20):
        _frame_record._emit_pending_stand_down()
    assert taken == [], f"the notifier took the latch lock with nothing pending: {taken}"
    # And with a reason pending it is consumed exactly once.
    monkeypatch.setattr(_frame_record, "_pending_stand_down", OSError("x"))
    monkeypatch.setattr(_frame_record.logger, "warning", lambda *a, **k: None)
    _frame_record._emit_pending_stand_down()
    assert len(taken) == 1 and _frame_record._pending_stand_down is None


def test_a_writer_thread_that_cannot_start_never_raises(monkeypatch, tmp_path):
    """``Thread.start`` raises ``RuntimeError`` when the process is out of
    threads; ``start_recorder`` must stand down, not propagate."""
    _clean(monkeypatch)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))

    def boom(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", boom)
    assert _frame_record.start_recorder() is False  # must not raise
    assert _frame_record.recording_destination() == ""


def test_teardown_does_not_block_on_a_full_queue(monkeypatch, tmp_path):
    """``_reset_for_tests`` runs from the autouse fixture. With a bounded queue
    a blocking ``put`` of the stop sentinel would hang pytest whenever a test
    left the backlog full; the stop event and the polled ``get`` make the
    thread exit on its own, and the join is bounded."""
    _clean(monkeypatch)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    monkeypatch.setattr(_frame_record, "QUEUE_LIMIT", 2)  # before the queue is built
    _frame_record.start_recorder()
    gate = threading.Event()
    started = threading.Event()

    def held(*_a):
        started.set()
        gate.wait(timeout=10)

    monkeypatch.setattr(_frame_record, "write_frame", held)

    async def enqueue(ids):
        for i in ids:
            await _frame_record.record_frame("kas", {"id": i})

    # One frame first, and wait until the drain thread has TAKEN it (it is now
    # held on the gate); only then two more, which fill the queue (maxsize 2).
    asyncio.run(enqueue([0]))
    assert started.wait(timeout=5), "the drain thread never took the first frame"
    asyncio.run(enqueue([1, 2]))
    writer = _frame_record._writer
    assert writer is not None, "no writer"
    assert writer.queued == _frame_record.QUEUE_LIMIT, "the queue was not filled"
    assert _frame_record.recording_destination() != "", "the fill overflowed"
    gate.set()
    done = threading.Event()

    def reset():
        _frame_record._reset_for_tests()
        done.set()

    threading.Thread(target=reset, daemon=True).start()
    assert done.wait(timeout=15), "_reset_for_tests blocked on the full queue"
    assert not writer.thread.is_alive()


def test_no_recorder_thread_is_ever_started_from_the_event_loop(monkeypatch, tmp_path):
    """``Thread.start()`` waits for the OS to schedule the new thread, which is
    unbounded on a starved host, so no lazy scheme may pay it on a reader
    loop. The writer is started by ``start_recorder`` before any loop exists;
    ``record_frame`` only enqueues. Spy on EVERY thread start while a loop
    records and assert none happened on the loop thread."""
    _clean(monkeypatch)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    assert _frame_record.start_recorder()
    starts: list[tuple[int, str]] = []
    real_start = threading.Thread.start

    def spy(self):
        starts.append((threading.get_ident(), self.name))
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", spy)

    async def go():
        loop_thread = threading.get_ident()
        for i in range(3):
            await _frame_record.record_frame("kas", {"jsonrpc": "2.0", "id": i})
        return loop_thread

    loop_thread = asyncio.run(go())
    on_loop = [name for ident, name in starts if ident == loop_thread]
    assert on_loop == [], f"threads started on the event loop: {on_loop}"
    _frame_record._reset_for_tests()


def test_recording_stands_down_if_the_recorder_was_not_started(monkeypatch, tmp_path, caplog):
    """The env var set after import, with no ``start_recorder`` call: the
    recorder must not start a thread from the loop to compensate. It stands
    down with a line naming the entry point."""
    _clean(monkeypatch)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))

    async def go():
        with caplog.at_level(logging.WARNING, logger=_frame_record.logger.name):
            await _frame_record.record_frame("kas", {"jsonrpc": "2.0"})

    asyncio.run(go())
    assert _frame_record.recording_destination() == ""
    assert "start_recorder" in caplog.text, caplog.text
    assert list(tmp_path.iterdir()) == []


def test_two_keys_that_scrub_to_one_spelling_both_survive(monkeypatch, tmp_path):
    """``/home/al/x`` and a literal ``~/x`` both scrub to ``~/x``; keeping the
    last would silently drop a field from the recording."""
    _clean(monkeypatch)
    home = tmp_path / "home" / "al"
    home.mkdir(parents=True)
    monkeypatch.setattr(_frame_record.Path, "home", staticmethod(lambda: home))
    frame = {f"{home}/x": 1, "~/x": 2}
    out = json.loads(_frame_record.scrub_frame(frame))
    assert sorted(out.values()) == [1, 2], out
    assert "~/x" in out and "~/x#2" in out, out


def test_two_event_loops_share_one_writer_and_one_byte_budget(monkeypatch, tmp_path):
    """The two transports read on different threads and loops. A writer bound
    to whichever loop recorded first would be replaced each time the other
    loop recorded, so two writers would append to the same file at once and
    each would count the byte budget from zero. One process-wide queue means
    frames from both loops land in one file in arrival order, and the second
    loop is held to the same backlog bound as the first.

    Both loops here are wedged behind one gate so nothing is written while
    the second loop enqueues; the byte cap is sized so that only the SUM of
    the two loops' frames exceeds it."""
    _clean(monkeypatch)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    _frame_record.start_recorder()
    monkeypatch.setattr(_frame_record, "QUEUE_BYTES_LIMIT", 2560)
    gate = threading.Event()
    real_write = _frame_record.write_frame

    def slow(backend, frame, dest):
        gate.wait(timeout=10)
        real_write(backend, frame, dest)

    monkeypatch.setattr(_frame_record, "write_frame", slow)

    def on_fresh_loop(ids):
        async def go():
            for i in ids:
                await _frame_record.record_frame(
                    "kas", {"jsonrpc": "2.0", "id": i}, wire_bytes=1024
                )

        asyncio.run(go())

    try:
        # Loop A: two frames, 2048 bytes, under the cap.
        t = threading.Thread(target=on_fresh_loop, args=([0, 1],))
        t.start()
        t.join(10)
        writer_after_a = _frame_record._writer
        assert _frame_record.recording_destination() != ""
        # Loop B: one more frame. 3072 bytes total; must trip the SHARED cap.
        t = threading.Thread(target=on_fresh_loop, args=([2],))
        t.start()
        t.join(10)
        assert _frame_record._writer is writer_after_a, "the second loop replaced the writer"
        assert _frame_record.recording_destination() == "", "the byte budget was per-loop"
    finally:
        gate.set()
    _frame_record._reset_for_tests()


# ── wiring ───────────────────────────────────────────────────────────────────


def test_the_recording_switch_is_scrubbed_from_agent_children(monkeypatch) -> None:
    """Both spawn paths build the child env through
    ``scrub_agent_subprocess_env``; the switch must not survive it, or a nested
    Kiro Crew would record into the same per-backend file as this gateway."""
    from kiro_crew.sandbox import scrub_agent_subprocess_env

    env = {_frame_record.ENV_RECORD_FRAMES: "/somewhere", "PATH": "/usr/bin"}
    scrubbed = scrub_agent_subprocess_env(env)
    assert _frame_record.ENV_RECORD_FRAMES not in scrubbed
    assert scrubbed["PATH"] == "/usr/bin"


def test_both_transports_record_their_inbound_frames() -> None:
    """The hook must sit in BOTH reader loops, or a backend records nothing.

    ``AcpRuntime._reader_loop`` serves kiro-cli and KAS; ``AcpClient._read_message``
    serves Claude Code and Codex. A hook in only one of them silently makes the
    other two backends unrecordable, which is invisible until someone tries to
    refresh their corpus.
    """
    for module in (runtime_module, client_module):
        path = Path(module.__file__ or "")
        source = path.read_text(encoding="utf-8")
        assert "await record_frame(" in source, (
            f"{path.name} does not await record_frame; the frames its backends read "
            "cannot be recorded, or the call blocks the event loop"
        )


def test_the_recorder_is_classified_as_capture_side_not_egress() -> None:
    """The recorder calls ``redact_text`` but writes to a local file, not a sink.

    The posture census fails any redactor caller that is neither a registered
    sink nor allowlisted; this pins WHICH bucket it is in, so a later move into
    ``_REDACTION_SINKS`` (which would claim a transport that does not exist) is a
    deliberate change rather than drift.
    """
    assert "acp/_frame_record.py" in security_posture.NON_EGRESS_REDACTION_MODULES
    registered = {module for _label, module, _detail in security_posture._REDACTION_SINKS}
    assert "acp/_frame_record.py" not in registered
