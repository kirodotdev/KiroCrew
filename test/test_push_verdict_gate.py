"""Tests for the gateway-owned push verdict store and its trusted activation.

The properties under test are the ones the gate's acceptance names, and two of them are
explicit requirements: an agent-written config can neither enable nor disable the gate, and
a trusted activation survives a restart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.security import push_verdict


@pytest.fixture(autouse=True)
def _clean_store() -> None:
    """Each test starts with an empty store.

    The store is module state in the gateway process, so a leaked verdict would make a
    later test pass for the wrong reason.
    """
    push_verdict._VERDICTS.clear()
    yield
    push_verdict._VERDICTS.clear()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


def _activate(home: Path, *, enabled: bool = True) -> Path:
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(json.dumps({"enabled": enabled}), encoding="utf-8")
    return leaf


def _record(session_key: str, **over: str) -> None:
    """Record a verdict with every bound field supplied.

    Every field is required with no default in the store on purpose, so a test that wants
    one value changed says so here rather than each call restating all six.
    """
    fields: dict[str, str] = {
        "gitdir": "/g",
        "worktree": "/some/worktree",
        "head": "a" * 40,
        "base": "main",
        "base_sha": "b" * 40,
        "remote": "origin",
        "source_ref": "feature-x",
    }
    fields.update(over)
    push_verdict.record(session_key, **fields)


def _url(name: str) -> str:
    """A synthetic remote URL. Never a real host and never a /home/<name> path."""
    return f"https://git.example.invalid/{name}.git"


def _credentialed_https(secret: str = "s3cr3t") -> str:
    """A synthetic HTTPS URL carrying an embedded ``user:secret@`` credential.

    Composed from pieces rather than spelled as a literal token URL: the point is only that a
    ``:`` precedes the ``@`` in the authority, which is the embedded-password shape.
    """
    return "https://x-access-token:" + secret + "@git.example.invalid/repo.git"


# ── The store ──


def test_a_session_with_no_verdict_reads_none() -> None:
    assert push_verdict.verdict_for("session-1") is None


def test_a_recorded_verdict_is_readable_by_its_own_session_only() -> None:
    _record("session-1")
    mine = push_verdict.verdict_for("session-1")
    assert mine is not None and mine.head == "a" * 40
    # The key is the CALLING SESSION, so a second session inherits nothing. This is the
    # property that makes "ask about one worktree, publish from another" impossible.
    assert push_verdict.verdict_for("session-2") is None


def test_an_empty_session_key_can_neither_record_nor_read() -> None:
    with pytest.raises(ValueError):
        _record("")
    assert push_verdict.verdict_for("") is None


def test_a_verdict_older_than_the_ceiling_is_not_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    _record("session-1")
    later = push_verdict._VERDICTS["session-1"].recorded_at + push_verdict.MAX_AGE_SECONDS + 1
    monkeypatch.setattr(push_verdict.time, "time", lambda: later)
    assert push_verdict.verdict_for("session-1") is None
    # Expired on read, not merely hidden: a later reader cannot see it either.
    assert "session-1" not in push_verdict._VERDICTS


# ── Trusted activation, the conductor's two required properties ──


def test_activation_is_off_on_an_installation_nobody_activated(home: Path) -> None:
    assert push_verdict.activation_enabled() is False


def test_activation_is_on_when_the_keystone_leaf_says_so(home: Path) -> None:
    _activate(home)
    assert push_verdict.activation_enabled() is True


def test_an_agent_written_config_can_neither_enable_nor_disable_the_gate(home: Path) -> None:
    """config.json may REQUEST activation; only the keystone AUTHORIZES it."""
    config = home / "config.json"
    config.write_text(
        json.dumps({"security": {"push_verdict_required": True, "push_verdict_enabled": True}}),
        encoding="utf-8",
    )
    # Config alone does not enable it.
    assert push_verdict.activation_enabled() is False

    # And config cannot switch OFF what the keystone turned on.
    _activate(home)
    config.write_text(
        json.dumps({"security": {"push_verdict_required": False, "push_verdict_enabled": False}}),
        encoding="utf-8",
    )
    assert push_verdict.activation_enabled() is True


def test_trusted_activation_survives_a_restart(home: Path) -> None:
    """Activation is on disk, so a fresh process still sees it.

    The verdicts are deliberately the opposite: a restart clears them, which costs one
    guard re-run and never leaves a stale pass behind. Both halves are asserted here
    because the pair is the design, and a change that persisted verdicts would pass the
    first assertion alone.
    """
    _activate(home)
    _record("session-1")

    # A restart is a fresh module state: the store is process memory, the leaf is not.
    push_verdict._VERDICTS.clear()

    assert push_verdict.activation_enabled() is True
    assert push_verdict.verdict_for("session-1") is None


def test_a_malformed_activation_leaf_refuses_instead_of_failing_open(home: Path) -> None:
    """An activation leaf that cannot be PARSED must not read as "gating off".

    Absence and unreadability are different facts. Absence means nobody activated gating,
    and false is the honest answer. A leaf that EXISTS but is malformed means the operator's
    activation state is unknown, and answering false there would make corrupting one file a
    way to disable the gate on an installation that had turned it on -- the gate's own off
    switch, reachable by damage rather than by authorization.

    So the store raises and the floor refuses the publish rather than allowing it. An
    installation that never activated never reaches this branch: absence still returns
    false rather than raising, which the neighbouring test pins.
    """
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text("{not json", encoding="utf-8")
    with pytest.raises(push_verdict.ActivationUnreadable):
        push_verdict.activation_enabled()


def test_a_non_object_activation_leaf_refuses_through_the_wrapper_too(home: Path) -> None:
    """The wrapper must not swallow what the reader raises.

    ``activation_enabled()`` is what the floor calls, so a refusal only ``activation()`` raised
    would be a fail-open at the one call site that matters. A suite that agrees with a hole
    cannot find it: while a test asserted ``[true]`` left the gate off, no mutation could be
    killed by the distinction, because both the code and the test read it the same way.
    """
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text("[true]", encoding="utf-8")
    with pytest.raises(push_verdict.ActivationUnreadable):
        push_verdict.activation_enabled()


def test_activation_requires_a_real_json_true(home: Path) -> None:
    """A truthy value is not an enable, and it is not a silent disable either.

    The JSON string ``"false"`` and the number ``1`` are both truthy in Python, so a
    ``bool(...)`` read would activate the gate on either -- that half has always held. The
    other half was wrong: reading them as OFF made a corrupted leaf disable the gate on an
    installation whose operator had enabled it, so a present non-boolean now REFUSES. Only
    two shapes are an operator's: a real ``true`` and a real ``false``. An explicit ``null``
    is indistinguishable from an absent key through ``.get()``, so it keeps the absent
    reading, which is off.
    """
    leaf = _activate(home)
    for corrupted in ("false", 1, "true"):
        leaf.write_text(json.dumps({"enabled": corrupted}), encoding="utf-8")
        with pytest.raises(push_verdict.ActivationUnreadable):
            push_verdict.activation_enabled()
    leaf.write_text(json.dumps({"enabled": None}), encoding="utf-8")
    assert push_verdict.activation_enabled() is False
    leaf.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    assert push_verdict.activation_enabled() is False
    leaf.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    assert push_verdict.activation_enabled() is True


@pytest.mark.parametrize(
    "document",
    [
        "[]",
        "5",
        '"true"',
        "null",
        '{"enabled": 1}',
        '{"enabled": "true"}',
        '{"enabled": "false"}',
    ],
)
def test_a_present_but_malformed_activation_refuses_rather_than_reading_off(
    home: Path, document: str
) -> None:
    """Off is the honest answer for an ABSENT leaf only.

    Unparseable bytes already raised. A document that PARSES but is not an object, or whose
    ``enabled`` is present and not a real boolean, was returning off -- a fail-open with a
    narrower entrance than the parse error: truncating the leaf to ``[]``, or writing
    ``{"enabled": 1}``, silently disabled the gate on an installation whose operator had
    turned it on. Note ``"false"`` is in here too: a STRING is a corrupted write whichever
    word it spells, and guessing the operator meant a disable is the same guess as guessing
    they meant an enable.
    """
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(document, encoding="utf-8")

    with pytest.raises(push_verdict.ActivationUnreadable):
        push_verdict.activation()


def test_the_two_shapes_an_operator_actually_writes_are_honoured(home: Path) -> None:
    """The other side of the refusal above: a real boolean is read, and absence is off."""
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)

    leaf.write_text('{"enabled": false}', encoding="utf-8")
    assert push_verdict.activation().enabled is False, "an operator's disable must be honoured"

    leaf.write_text('{"enabled": true, "guard_sha256": "' + "a" * 64 + '"}', encoding="utf-8")
    assert push_verdict.activation().enabled is True

    leaf.write_text("{}", encoding="utf-8")
    assert push_verdict.activation().enabled is False, "absent enabled is a never-activated leaf"


def test_the_activation_read_is_live_on_every_publish(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activation is re-read per publish, and that is the deliberate choice.

    A reviewer reads the floor's "no expensive I/O" contract and sees an ``open()`` plus a JSON
    parse, which is a fair objection. A stat-keyed cache of the parse was written and removed:
    its key -- modification time, size and inode -- cannot tell two writes of the SAME byte
    length within one clock tick apart, and the value it would serve is an ENABLE decision, so
    the failure mode is the gate reading as off after an operator turned it on. This test is the
    pin that the cheap-but-wrong version does not come back.
    """
    leaf = _activate(home)
    parses = 0
    real_load = push_verdict.json.load

    def _counting_load(handle):
        nonlocal parses
        parses += 1
        return real_load(handle)

    monkeypatch.setattr(push_verdict.json, "load", _counting_load)

    for _ in range(3):
        assert push_verdict.activation().enabled is True
    assert parses == 3, "activation was served from a cache"

    # The property that matters: a change takes effect on the very next read, whatever its size.
    leaf.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    assert push_verdict.activation().enabled is False


# ── The guard digest pin, read off the keystone ──


@pytest.mark.parametrize("pinned", ["", "abc123", "z" * 64, "A" * 63, 42, None])
def test_only_a_real_digest_counts_as_a_pin(home: Path, pinned: object) -> None:
    """A malformed pin reads as ABSENT, which is what produces the message that fixes it.

    Absence and "a digest that can never match" behave alike at the comparison, but only
    absence tells the operator to pin one. A 64-character non-hex string is the case a bare
    length check would wave through.
    """
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(json.dumps({"enabled": True, "guard_sha256": pinned}), encoding="utf-8")
    read = push_verdict.activation()
    assert read.enabled is True
    assert read.guard_sha256 == ""


def test_a_real_digest_is_read_back_lowercased(home: Path) -> None:
    """The companion, or the test above passes for an empty reader."""
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(json.dumps({"enabled": True, "guard_sha256": "A" * 64}), encoding="utf-8")
    assert push_verdict.activation().guard_sha256 == "a" * 64


# ── Finding A: the push DESTINATION is bound to an operator pin ──


def test_activation_reads_an_operator_pinned_push_url(home: Path) -> None:
    """The keystone carries the pin, and it is read back verbatim (only whitespace stripped)."""
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(
        json.dumps({"enabled": True, "pinned_push_url": "  " + _url("canonical") + "  "}),
        encoding="utf-8",
    )
    read = push_verdict.activation()
    assert read.enabled is True
    assert read.pinned_push_url == _url("canonical")


def test_an_absent_pin_leaves_the_destination_unconstrained(home: Path) -> None:
    """No pin key, and an explicit JSON null, both mean the operator pinned nothing.

    That is the never-pinned answer -- an empty string -- and the destination is unconstrained
    exactly as on an install that never activated. The pin is an opt-in, not a default.
    """
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    assert push_verdict.activation().pinned_push_url == ""
    leaf.write_text(json.dumps({"enabled": True, "pinned_push_url": None}), encoding="utf-8")
    assert push_verdict.activation().pinned_push_url == ""


@pytest.mark.parametrize("corrupt", [42, ["x"], {"a": 1}, "", "   ", "--upload-pack=/opt/evil"])
def test_a_corrupted_pin_refuses_rather_than_reading_as_absent(home: Path, corrupt: object) -> None:
    """A PRESENT-but-broken pin is corruption, and reading it as "" would be a fail-open.

    Damaging this field would silently drop the destination binding on an installation whose
    operator set one -- the same fail-open the boolean and digest checks refuse. A non-string,
    a blank/whitespace string, and a leading-dash value (git reads ``-`` as an option, never a
    repository) all RAISE. An operator who wants no pin removes the key entirely.
    """
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(json.dumps({"enabled": True, "pinned_push_url": corrupt}), encoding="utf-8")
    with pytest.raises(push_verdict.ActivationUnreadable):
        push_verdict.activation()


@pytest.mark.parametrize(
    "credentialed",
    [
        _credentialed_https(),
        # scp-like ``user:secret@host:path`` (no scheme) is the SSH embedded-password shape.
        "git-user:s3cr3t@git.example.invalid:repo.git",
        # Codex F1: a USERNAME-ONLY non-SSH URL (no ``:secret``) is still an unpinnable
        # credential carrier for HTTP(S) -- a pin needs no userinfo there -- so it is refused.
        "https://x-access-token@git.example.invalid/repo.git",
        "https://someuser@git.example.invalid/repo.git",
    ],
)
def test_a_pin_carrying_an_embedded_credential_is_refused(home: Path, credentialed: str) -> None:
    """The finding this closes: the activation leaf is readable in-sandbox, so REQUIRING a
    credentialed pin to publish would force a token into a leaf any ``open()`` can read.

    A URL whose userinfo carries a PASSWORD/TOKEN (a ``:`` before the ``@`` in the authority)
    is refused with ``ActivationUnreadable``. Mutation check: on pre-fix code ``_pinned_push_url``
    returned the credentialed value verbatim (no userinfo check), so ``activation()`` did NOT
    raise and this ``pytest.raises`` block failed.
    """
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(
        json.dumps({"enabled": True, "pinned_push_url": credentialed}), encoding="utf-8"
    )
    with pytest.raises(push_verdict.ActivationUnreadable):
        push_verdict.activation()


@pytest.mark.parametrize(
    "credential_free",
    [
        "https://git.example.invalid/repo.git",
        # A bare ``user@host`` username with NO ``:`` password is allowed -- it is not a secret.
        "git@git.example.invalid:repo.git",
        "ssh://git@git.example.invalid/repo.git",
    ],
)
def test_a_credential_free_pin_is_accepted(home: Path, credential_free: str) -> None:
    """A URL that names a destination without embedding a password/token is accepted verbatim.

    The gateway authenticates the publish from its OWN configured credentials, so the pin need
    only name the repository. A bare ``user@host`` (SSH username, no secret) is NOT a credential
    and is allowed.
    """
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(
        json.dumps({"enabled": True, "pinned_push_url": credential_free}), encoding="utf-8"
    )
    assert push_verdict.activation().pinned_push_url == credential_free


def test_the_embedded_credential_refusal_message_is_actionable(home: Path) -> None:
    """The refusal tells the operator what to do: pin a credential-free URL."""
    leaf = home / push_verdict.ACTIVATION_LEAF
    leaf.parent.mkdir(parents=True, exist_ok=True)
    leaf.write_text(
        json.dumps({"enabled": True, "pinned_push_url": _credentialed_https()}), encoding="utf-8"
    )
    with pytest.raises(push_verdict.ActivationUnreadable) as excinfo:
        push_verdict.activation()
    message = str(excinfo.value)
    assert "embedded credential" in message
    assert "no embedded credentials" in message
    assert "own configured credentials" in message


# ── The URL helpers the destination pin is compared through ──


def test_unparseable_port_helper_and_credential_free_url_do_not_raise() -> None:
    """The port helper detects a bad port and the credential-free compare never raises on one."""
    assert push_verdict._url_has_unparseable_port("https://h.invalid:notaport/r.git") is True
    assert push_verdict._url_has_unparseable_port("https://h.invalid:99999/r.git") is True
    assert push_verdict._url_has_unparseable_port("https://h.invalid:443/r.git") is False
    assert push_verdict._url_has_unparseable_port("https://h.invalid/r.git") is False
    # Defensive: the pure helper must not raise even if handed a bad-port URL directly.
    push_verdict._credential_free_url("https://h.invalid:notaport/r.git")


def test_credential_free_url_keeps_distinct_ipv6_destinations_distinct() -> None:
    """Two DISTINCT IPv6 destinations must not collapse to one credential-free identity.

    ``urlsplit(...).hostname`` returns an IPv6 literal WITHOUT its ``[...]`` brackets, so the
    pre-fix helper re-appended ``:port`` to the bare address and lost the host/port boundary:
    ``https://[2001:db8::1]:443/r.git`` and ``https://[2001:db8::1:443]/r.git`` (no port) both
    recomposed to ``https://2001:db8::1:443/r.git`` and compared EQUAL. Because the destination
    pin is a plain string equality over this output, that collision let an agent-chosen IPv6
    destination match the operator pin and be published to (UNBOUNDED). Mutation check: on the
    un-fixed helper the boundary-pair assertion below is EQUAL, so this test FAILS.
    """
    a = "https://[2001:db8::1]:443/r.git"
    b = "https://[2001:db8::1:443]/r.git"  # distinct dest; collided with ``a`` pre-fix
    c = "https://[2001:db8::2]:443/r.git"  # distinct address, same port
    d = "https://[2001:db8::1]:8443/r.git"  # same address, different port
    # Distinct IPv6 destinations stay distinct after normalization.
    assert push_verdict._credential_free_url(a) != push_verdict._credential_free_url(b)
    assert push_verdict._credential_free_url(a) != push_verdict._credential_free_url(c)
    assert push_verdict._credential_free_url(a) != push_verdict._credential_free_url(d)
    # The SAME IPv6 destination still normalizes to one identity (both with and without a port).
    assert push_verdict._credential_free_url(a) == push_verdict._credential_free_url(a)
    assert push_verdict._credential_free_url(
        "https://[2001:db8::1]/r.git"
    ) == push_verdict._credential_free_url("https://[2001:db8::1]/r.git")


def test_credential_free_url_is_unchanged_for_non_ipv6() -> None:
    """HTTP(S) drops userinfo; SSH/scp keeps the username; host/port/path preserved.

    A non-IPv6 hostname never contains ``:``, so the IPv6 re-bracketing never triggers, and the
    HTTP(S) output matches what the helper produced before the F2 fix (the token username is a
    credential carrier there, not identity). The scp-like/SSH forms now KEEP the username.
    """
    # Plain URL: unchanged.
    assert (
        push_verdict._credential_free_url("https://git.example.invalid/repo.git")
        == "https://git.example.invalid/repo.git"
    )
    # scheme URL with an embedded token: userinfo stripped, host/path preserved.
    assert (
        push_verdict._credential_free_url(
            "https://x-access-token:s3cr3t@git.example.invalid/canonical.git"
        )
        == "https://git.example.invalid/canonical.git"
    )
    # Host with an explicit port: port preserved, no brackets added.
    assert (
        push_verdict._credential_free_url("https://git.example.invalid:8443/repo.git")
        == "https://git.example.invalid:8443/repo.git"
    )
    # scp-like ``user@host:path``: the username is IDENTITY (login-relative path) and is KEPT;
    # only a ``:secret`` half would be stripped, and there is none here.
    assert (
        push_verdict._credential_free_url("git@git.example.invalid:path/repo.git")
        == "git@git.example.invalid:path/repo.git"
    )
    # scp-like with an embedded ``user:secret@``: the ``:secret`` is stripped, the username KEPT.
    assert (
        push_verdict._credential_free_url("user:secret@git.example.invalid:path/repo.git")
        == "user@git.example.invalid:path/repo.git"
    )


def test_credential_free_url_keeps_ssh_usernames_distinct() -> None:
    """Two SSH accounts at one host are two identities and must NOT collapse (codex F2).

    For the scp-like ``user@host:path`` form the path is LOGIN-RELATIVE, so
    ``deploy@host:repo`` and ``staging@host:repo`` are two repositories under two accounts.
    The pre-fix helper discarded the whole ``user@`` prefix, collapsing them to one identity so
    an agent-chosen account could match a pin that authorized a different one. The username is
    now preserved for the scp-like and ``ssh://`` forms, so the two stay distinct. Mutation
    check: restoring the old whole-userinfo strip makes both sides equal and this fails.
    """
    # scp-like: two accounts, one host, one path -> two DISTINCT identities.
    assert push_verdict._credential_free_url(
        "deploy@git.example.invalid:repo.git"
    ) != push_verdict._credential_free_url("staging@git.example.invalid:repo.git")
    # ssh:// scheme: username is identity here too and is preserved (only a password stripped).
    assert (
        push_verdict._credential_free_url("ssh://deploy@git.example.invalid/repo.git")
        == "ssh://deploy@git.example.invalid/repo.git"
    )
    assert push_verdict._credential_free_url(
        "ssh://deploy@git.example.invalid/repo.git"
    ) != push_verdict._credential_free_url("ssh://staging@git.example.invalid/repo.git")
    # A password on an ssh:// URL is still a secret: stripped, username kept.
    assert (
        push_verdict._credential_free_url("ssh://deploy:pw@git.example.invalid/repo.git")
        == "ssh://deploy@git.example.invalid/repo.git"
    )
