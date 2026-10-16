"""What a failed registry clone tells the installer.

The credential-posture hint fires for an index-originated clone only when git's
output is auth-shaped (softened to a likely cause when git cannot find the
repository), every clone env that feeds the failure classifier pins a
locale that is valid on the running platform, the classifier's markers anchor on
git's own phrasing, and the sibling failure shapes carry the sentence in ``error``
and the slug in ``code``.

``test_external_registry.py`` holds the index cache, fetch and lookup tests and names
every sibling; shared fixtures live in ``external_registry_helpers.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from external_registry_helpers import _catalog_absent  # noqa: F401
from external_registry_helpers import _explicit_registry_execution_admission  # noqa: F401


class TestCloneFailureDiagnostics:
    """A failed clone must tell an index-originated installer WHY: the clone
    ran credential-free because the repo URL differs from the registry URL, so
    a private sibling repo cannot be read. Owner-designated installs keep the
    ambient identity, so their failure must NOT carry the misleading hint.

    The human sentence lives in ``error`` and the machine slug in ``code``: the
    App Store install banner renders ``result.error`` and never
    ``result.message`` (AppDetailPage.tsx), so a slug in ``error`` would be
    shown to the user verbatim.
    """

    @staticmethod
    def _fake_wrap_argv(argv, mode="standard", **_kwargs):
        return list(argv), None

    class _FailProc:
        returncode = 128  # git clone failure

        async def communicate(self):
            return (b"fatal: could not read Username (no credentials)", None)

    @pytest.mark.asyncio
    async def test_index_originated_failure_explains_credential_posture(self, tmp_path):
        """index_originated=True + failed fresh clone → error carries the
        credential-posture explanation and the monorepo-recipe pointer; the
        machine slug is in ``code``."""
        import kiro_crew.apps.registry as reg

        git_url = "https://forge.example.com/owner/private-sibling.git"
        dest = tmp_path / "app-sources" / "myapp"

        async def _fake_create_subprocess(*args, **kwargs):
            # Fresh clone: dest is created by git, then git exits nonzero.
            dest.mkdir(parents=True, exist_ok=True)
            return self._FailProc()

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=self._fake_wrap_argv),
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
        ):
            err = await reg._git_clone_or_pull(
                git_url,
                "main",
                dest,
                log_lines,
                index_originated=True,
            )

        assert err is not None
        assert err["ok"] is False
        # Machine-stable slug is in `code`, NOT `error`.
        assert err["code"] == "git_clone_failed_no_credentials"
        human = err["error"]
        # The human text must NOT be the bare slug (which the banner would show).
        assert human != "git_clone_failed_no_credentials"
        assert human != "git clone failed"
        # It explains the cause (credential-free / withheld) and the remedy.
        low = human.lower()
        assert "credential" in low
        assert "registry" in low
        assert "docs/app-kit/publishing-guide.md" in human
        # No secret/identity material: only the owner's own registry/repo posture
        # is referenced, never the git URL, a token, or ssh identity.
        assert git_url not in human
        assert "ssh" not in low
        assert "token" not in low

    @pytest.mark.asyncio
    async def test_owner_designated_failure_has_no_misleading_hint(self, tmp_path):
        """index_originated=False + failed fresh clone → the bare
        ``git clone failed`` error, with NO credential-posture hint (the clone
        kept the ambient identity, so the hint would be wrong)."""
        import kiro_crew.apps.registry as reg

        git_url = "https://forge.example.com/owner/app.git"
        dest = tmp_path / "app-sources" / "myapp"

        async def _fake_create_subprocess(*args, **kwargs):
            dest.mkdir(parents=True, exist_ok=True)
            return self._FailProc()

        log_lines: list[str] = []
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=self._fake_wrap_argv),
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda a: a),
            patch(
                "kiro_crew.apps.registry.create_subprocess_limited",
                side_effect=_fake_create_subprocess,
            ),
        ):
            err = await reg._git_clone_or_pull(
                git_url,
                "main",
                dest,
                log_lines,
                index_originated=False,
            )

        assert err is not None
        assert err["ok"] is False
        # Bare honest failure with NO credential-posture hint (the clone kept the
        # ambient identity, so the hint would be wrong). The body still carries
        # the machine-readable `code` (the non-2xx invariant in
        # docs/system-specs/common/code-style.md) — a bare slug
        # `git_clone_failed`, distinct from the credential-posture
        # `git_clone_failed_no_credentials`; the `error` sentence stays bare.
        assert err["error"] == "git clone failed"
        assert err["code"] == "git_clone_failed"
        assert err["code"] != "git_clone_failed_no_credentials"
        assert "credential" not in err["error"].lower()


# ---------------------------------------------------------------------------
# Clone-failure diagnostics honesty.
#   Finding 1: the index-originated credential-posture hint must only fire when
#   git's output is auth-shaped (or ambiguously so); a DNS blip or typo'd
#   branch must NOT be told to restructure repositories, and no raw
#   credential-bearing stderr may reach the banner.
#   Finding 2: three sibling failure shapes must carry the human sentence in
#   `error` and the machine slug in `code` (the install banner renders
#   `result.error` verbatim and ignores `code`).
# ---------------------------------------------------------------------------


class _FailingCloneProc:
    """A clone subprocess that exits nonzero with *output* on its merged
    stdout/stderr stream (the real clone runs with ``stderr=STDOUT``)."""

    returncode = 1

    def __init__(self, output: bytes) -> None:
        self._output = output

    async def communicate(self):
        return (self._output, None)


async def _run_index_clone_failure(tmp_path, output: bytes, *, index_originated: bool = True):
    """Drive ``_git_clone_or_pull`` down the fresh-clone path to a nonzero exit
    whose merged output is *output*; return the result dict. ``index_originated``
    selects the confused-deputy (credential-free) posture path (True, default)
    vs the owner-designated path (False) whose only failure shape is the bare
    ``git clone failed`` body."""
    from kiro_crew.apps import registry as reg

    def _fake_wrap_argv(argv, mode="standard", **_kwargs):
        return argv, None

    async def _fake_create(*args, **kwargs):
        return _FailingCloneProc(output)

    dest = tmp_path / "clone-dest"  # does not exist → fresh-clone path
    with (
        patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
        patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
        patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda c: c),
        patch("kiro_crew.apps.registry.create_subprocess_limited", new=_fake_create),
    ):
        result = await reg._git_clone_or_pull(
            "https://github.com/acme/private-sibling.git",
            "main",
            dest,
            [],
            index_originated=index_originated,
        )
    return result


class TestIndexOriginatedCloneFailureHintIsGated:
    """Finding 1: the credential-posture remedy is honest only when withheld
    credentials are a plausible cause."""

    @pytest.mark.asyncio
    async def test_auth_shaped_failure_keeps_the_posture_hint(self, tmp_path):
        result = await _run_index_clone_failure(
            tmp_path,
            b"remote: Permission denied to acme/private-sibling.\n"
            b"fatal: Authentication failed for 'https://github.com/acme/private-sibling.git/'\n",
        )
        assert result is not None and result["ok"] is False
        # Machine slug in `code`, never in the banner-rendered `error`.
        assert result["code"] == "git_clone_failed_no_credentials"
        assert "credentials are withheld" in result["error"]
        assert "Private app repos must live inside the registry repo" in result["error"]
        # A definite auth failure is NOT softened to "a likely cause".
        assert "a likely cause" not in result["error"]

    @pytest.mark.asyncio
    async def test_dns_failure_drops_the_posture_hint(self, tmp_path):
        result = await _run_index_clone_failure(
            tmp_path,
            b"fatal: unable to access 'https://github.com/acme/private-sibling.git/': "
            b"Could not resolve host: github.com\n",
        )
        assert result is not None and result["ok"] is False
        # The misleading remedy MUST NOT fire for a network failure.
        assert result.get("code") != "git_clone_failed_no_credentials"
        assert "Private app repos must live inside the registry repo" not in result["error"]
        assert "credentials are withheld" not in result["error"]
        # The honest, redacted (constant) failure class is surfaced instead.
        assert result["code"] == "git_clone_failed"
        assert result["error"] == "Git clone failed: host could not be resolved."

    @pytest.mark.asyncio
    async def test_typoed_branch_failure_drops_the_posture_hint(self, tmp_path):
        result = await _run_index_clone_failure(
            tmp_path,
            b"fatal: Remote branch nonexistent-branch not found in upstream origin\n",
        )
        assert result is not None and result["ok"] is False
        assert result.get("code") != "git_clone_failed_no_credentials"
        assert "credentials are withheld" not in result["error"]
        assert result["code"] == "git_clone_failed"
        assert result["error"] == "Git clone failed: the requested branch does not exist."

    @pytest.mark.asyncio
    async def test_repository_not_found_is_posture_possible_softened(self, tmp_path):
        # Ambiguous on an auth-gated forge: keep the hint, softened.
        result = await _run_index_clone_failure(
            tmp_path,
            b"remote: Repository not found.\n"
            b"fatal: repository 'https://github.com/acme/private-sibling.git/' not found\n",
        )
        assert result is not None and result["ok"] is False
        assert result["code"] == "git_clone_failed_no_credentials"
        assert "a likely cause is that owner credentials are withheld" in result["error"]

    @pytest.mark.asyncio
    async def test_unrecognized_failure_returns_bare_honest_message(self, tmp_path):
        result = await _run_index_clone_failure(tmp_path, b"fatal: something weird happened\n")
        assert result is not None and result["ok"] is False
        assert result.get("code") not in ("git_clone_failed_no_credentials",)
        assert result["error"] == "git clone failed"
        assert "credentials are withheld" not in result["error"]
        # non-2xx-body-carries-code: the bare fallback is the only failure shape
        # on this path that once returned no `code`; a machine keys on `code`
        # while the frontend renders `error` verbatim, so the slug must be
        # present even when no failure class is recognized (the non-2xx invariant
        # in docs/system-specs/common/code-style.md). Regression pin: this
        # assertion fails at the pre-fix tree.
        assert result["code"] == "git_clone_failed"

    @pytest.mark.asyncio
    async def test_local_unwritable_destination_drops_the_posture_hint(self, tmp_path):
        # auth-marker-precision: a LOCAL filesystem failure — an unwritable
        # clone destination — emits git's own `Permission denied` errno text
        # with NO method parenthetical. It has nothing to do with withheld
        # remote credentials, so the credential-posture remedy MUST NOT fire.
        # Fails at the pre-fix tree (the bare `permission denied` marker matched
        # this local errno and mislabeled it credential-blocked); passes after.
        result = await _run_index_clone_failure(
            tmp_path,
            b"fatal: could not create work tree dir "
            b"'/read-only/clone-dest': Permission denied\n",
        )
        assert result is not None and result["ok"] is False
        assert result.get("code") != "git_clone_failed_no_credentials"
        assert "Private app repos must live inside the registry repo" not in result["error"]
        assert "credentials are withheld" not in result["error"]
        # An unrecognized (non-auth, non-classified) failure falls open to the
        # honest bare message — never a false posture hint.
        assert result["code"] == "git_clone_failed"
        assert result["error"] == "git clone failed"

    @pytest.mark.asyncio
    async def test_ssh_publickey_refusal_keeps_the_posture_hint(self, tmp_path):
        # A genuine REMOTE SSH credential refusal carries the method
        # parenthetical (`Permission denied (publickey).`) that a local errno
        # never has, so it is still classified auth-shaped and keeps the
        # posture hint. This is the true-positive control for the marker
        # tightening above.
        result = await _run_index_clone_failure(
            tmp_path,
            b"git@github.com: Permission denied (publickey).\n"
            b"fatal: Could not read from remote repository.\n",
        )
        assert result is not None and result["ok"] is False
        assert result["code"] == "git_clone_failed_no_credentials"
        assert "credentials are withheld" in result["error"]
        assert "Private app repos must live inside the registry repo" in result["error"]

    def test_permission_denied_marker_is_remote_ssh_specific(self):
        # Unit-level pin on the classifier verdict (never on git argv): the
        # `permission denied` auth marker fires ONLY on the SSH method-list
        # forms, never on a bare local-FS errno. This is the property the
        # end-to-end tests above exercise, asserted directly on the boolean.
        from kiro_crew.apps import registry as reg

        # Local FS errno forms — every one must fall open (not auth-shaped).
        assert (
            reg._git_output_is_auth_shaped(
                "fatal: could not create work tree dir '/x': Permission denied"
            )
            is False
        )
        assert reg._git_output_is_auth_shaped("error: open('/x'): Permission denied") is False
        # Remote SSH refusal forms — every method list must stay auth-shaped.
        assert reg._git_output_is_auth_shaped("git@host: Permission denied (publickey).") is True
        assert reg._git_output_is_auth_shaped("Permission denied (publickey,password).") is True
        assert (
            reg._git_output_is_auth_shaped("Permission denied (publickey,keyboard-interactive).")
            is True
        )
        # fail-open-direction-pinned: a phrasing the allowlist does not
        # recognize returns False (falls open to the honest bare message), so a
        # future git rewording drops the posture hint rather than fabricating
        # one — the safe direction for a version-sensitive substring allowlist.
        assert reg._git_output_is_auth_shaped("fatal: some future git wording") is False

    @pytest.mark.asyncio
    async def test_owner_designated_clone_failure_body_carries_code(self, tmp_path):
        # The owner-designated (index_originated=False) path never runs the
        # credential-posture classifier — its sole failure shape is the outer
        # bare body. It, too, must carry the machine-readable `code` (the second
        # bare return site). Regression pin: fails at the pre-fix tree, where the
        # body was `{"ok": False, "name": ...}` with no `code`.
        result = await _run_index_clone_failure(
            tmp_path,
            b"fatal: something weird happened\n",
            index_originated=False,
        )
        assert result is not None and result["ok"] is False
        assert result["error"] == "git clone failed"
        assert result["code"] == "git_clone_failed"
        # The posture hint never fires off the owner-designated path.
        assert "credentials are withheld" not in result["error"]

    @pytest.mark.asyncio
    async def test_no_credential_bearing_stderr_reaches_the_banner(self, tmp_path):
        # A credential-bearing URL and an on-disk path in git's output must not
        # be echoed into the user-facing banner. The banner is derived from a
        # constant allowlist, never a slice of git output.
        secret_token = "ghp_SUPERSECRETTOKEN1234567890"
        secret_path = "/home/user/.ssh/id_rsa"
        result = await _run_index_clone_failure(
            tmp_path,
            (
                f"fatal: unable to access "
                f"'https://x-access-token:{secret_token}@github.com/acme/private-sibling.git/': "
                f"Could not resolve host: github.com\n"
                f"could not read private key from {secret_path}\n"
            ).encode(),
        )
        assert result is not None and result["ok"] is False
        banner = result["error"]
        assert secret_token not in banner
        assert secret_path not in banner
        assert "x-access-token" not in banner
        # Exact honest banner (not a bare substring of git output).
        assert banner == "Git clone failed: host could not be resolved."


class TestCloneLocaleIsPinnedForDeterministicClassifier:
    """deterministic-classifier-input: every clone env whose stderr feeds
    ``_git_output_is_auth_shaped`` pins ``LC_ALL`` so git emits deterministic
    English regardless of the operator's ``LANG``/``LC_ALL``. Without the pin, a
    credential-blocked owner on a non-English host would see git localize
    ``fatal: Authentication failed`` — the English-only marker allowlist would
    miss and the posture hint would silently vanish. We assert the deterministic
    PROPERTY (the pin on the constructed env), not a real non-English git."""

    def test_anonymous_git_env_pins_locale_over_operator_lang(self, monkeypatch):
        from kiro_crew.apps import registry as reg

        # Simulate a non-English operator host: LANG/LC_ALL are on os.environ
        # and pass through _SAFE_ENV_KEYS.
        monkeypatch.setenv("LANG", "de_DE.UTF-8")
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
        env = reg.anonymous_git_env()
        # LC_ALL is pinned to the C locale (wins over LANG and any LC_*), so
        # git's client-side messages are the classifier's known English.
        assert env["LC_ALL"] == reg._GIT_CLONE_LOCALE
        assert env["LC_ALL"] != "de_DE.UTF-8"
        # The pin does not weaken any credential suppression.
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert "SSH_AUTH_SOCK" not in env

    def test_minimal_env_does_not_pin_locale(self, monkeypatch):
        # The owner-designated (minimal_env) path is NOT read by the classifier,
        # so it must NOT pin the locale — pinning here would only degrade the
        # many other minimal_env subprocesses (pip installs, app backends, …),
        # and C.UTF-8 is invalid on macOS BSD libc. The operator's LANG/LC_ALL
        # pass through unchanged.
        from kiro_crew.apps import registry as reg

        monkeypatch.setenv("LANG", "ja_JP.UTF-8")
        monkeypatch.setenv("LC_ALL", "ja_JP.UTF-8")
        env = reg.minimal_env()
        assert env["LC_ALL"] == "ja_JP.UTF-8"

    def test_explicit_extra_still_overrides_the_pin(self, monkeypatch):
        # The pin is applied before *extra*, so an explicit caller override wins
        # (documented contract) — the pin is a default, not a lock.
        from kiro_crew.apps import registry as reg

        env = reg.anonymous_git_env(LC_ALL="en_US.UTF-8")
        assert env["LC_ALL"] == "en_US.UTF-8"

    def test_pinned_locale_makes_the_english_classifier_input_match(self):
        # The property the pin buys: with the locale pinned to C, git emits the
        # English phrasing the STRICT allowlist recognizes, so a genuinely
        # credential-blocked clone is classified auth-shaped. A localized
        # (e.g. German) rendering of the same failure would NOT match — which is
        # exactly why the env must force English before the classifier runs.
        from kiro_crew.apps import registry as reg

        english = "fatal: Authentication failed for 'https://github.com/acme/x.git/'"
        localized = "fatal: Authentifizierung fehlgeschlagen für 'https://github.com/acme/x.git/'"
        assert reg._git_output_is_auth_shaped(english) is True
        assert reg._git_output_is_auth_shaped(localized) is False

    @pytest.mark.asyncio
    async def test_index_clone_localized_auth_failure_gets_hint_after_pin(self, tmp_path):
        # End-to-end: the locale pin forces git to emit English, so the clone
        # output the classifier sees is the English phrasing (modeled here),
        # and a credential-blocked index-originated clone keeps the posture hint
        # on a non-English host. Fails at 2db0acd4 (no pin: git would localize
        # and the hint would vanish); passes after.
        from kiro_crew.apps import registry as reg

        captured: dict = {}

        def _fake_wrap_argv(argv, mode="standard", **_kwargs):
            return argv, None

        async def _fake_create(*args, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FailingCloneProc(
                b"remote: Permission denied to acme/private-sibling.\n"
                b"fatal: Authentication failed for "
                b"'https://github.com/acme/private-sibling.git/'\n"
            )

        dest = tmp_path / "clone-dest"
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch("kiro_crew.apps.registry.wrap_argv", side_effect=_fake_wrap_argv),
            patch("kiro_crew.apps.registry.cgroup_scope_argv", side_effect=lambda c: c),
            patch("kiro_crew.apps.registry.create_subprocess_limited", new=_fake_create),
        ):
            result = await reg._git_clone_or_pull(
                "https://github.com/acme/private-sibling.git",
                "main",
                dest,
                [],
                index_originated=True,
            )
        # The clone env carried the pin (the deterministic property).
        assert captured["env"]["LC_ALL"] == reg._GIT_CLONE_LOCALE
        # And the posture hint survived.
        assert result is not None and result["ok"] is False
        assert result["code"] == "git_clone_failed_no_credentials"
        assert "credentials are withheld" in result["error"]


class TestCloneLocaleIsPlatformValid:
    """locale-value-valid-on-every-platform: the pinned clone locale
    (``_GIT_CLONE_LOCALE``, fed to ``anonymous_git_env``'s ``LC_ALL``) MUST be a
    locale that is valid on the current platform's libc. ``C.UTF-8`` is the
    always-present UTF-8 locale on glibc/musl (Linux) but is NOT a valid BSD-libc
    locale on macOS, where an explicitly-set invalid ``LC_ALL`` makes
    ``setlocale`` fall to C/ASCII and suppresses PEP 538 coercion, so a child
    reading non-ASCII git output raises ``UnicodeDecodeError`` — turning the
    diagnostic clone into a crash. The deterministic property is that the pinned
    value is platform-appropriate: ``en_US.UTF-8`` on Darwin, ``C.UTF-8``
    elsewhere (mirroring :mod:`kiro_crew.service.common`)."""

    def test_pinned_locale_is_valid_for_the_running_platform(self):
        # The module-level constant, as resolved for THIS host, must be the
        # platform-appropriate value. Fails at 0e3001598 on macOS (the value was
        # the hardcoded, BSD-libc-invalid "C.UTF-8"); passes after the split.
        import sys

        from kiro_crew.apps import registry as reg

        expected = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
        assert reg._GIT_CLONE_LOCALE == expected
        # macOS never gets the BSD-libc-invalid C.UTF-8.
        if sys.platform == "darwin":
            assert reg._GIT_CLONE_LOCALE != "C.UTF-8"

    def test_locale_selection_is_platform_split_not_a_flat_literal(self, monkeypatch):
        # Drive the selection logic on BOTH platforms without depending on the
        # host's real platform or a real non-English git: reload the module with
        # sys.platform monkeypatched and assert the resolved constant. Fails at
        # 0e3001598 (the value is a flat "C.UTF-8" literal, so it stays "C.UTF-8"
        # even under a darwin reload); passes after the platform split.
        import importlib
        import sys

        from kiro_crew.apps import registry as reg

        try:
            monkeypatch.setattr(sys, "platform", "darwin")
            # flake-ok: pre-existing reload of the registry facade under a patched sys.platform, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-in-process-reload -- pre-existing reload of the registry facade under a patched sys.platform, moved verbatim; tracked for a de-flake pass
            reloaded = importlib.reload(reg)
            assert reloaded._GIT_CLONE_LOCALE == "en_US.UTF-8"

            monkeypatch.setattr(sys, "platform", "linux")
            # flake-ok: pre-existing reload of the registry facade under a patched sys.platform, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-in-process-reload -- pre-existing reload of the registry facade under a patched sys.platform, moved verbatim; tracked for a de-flake pass
            reloaded = importlib.reload(reg)
            assert reloaded._GIT_CLONE_LOCALE == "C.UTF-8"
        finally:
            # Restore the module to the real-platform value so later tests (and
            # the shared module object) see the genuine constant.
            # flake-ok: pre-existing undo in the finally that restores the real platform before the last reload, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-bare-monkeypatch-undo -- pre-existing undo in the finally that restores the real platform before the last reload, moved verbatim; tracked for a de-flake pass
            monkeypatch.undo()
            # flake-ok: pre-existing reload in the finally, after the undo restored the real sys.platform, so the shared registry facade carries the genuine constant again, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-in-process-reload -- pre-existing reload in the finally, after the undo restored the real sys.platform, so the shared registry facade carries the genuine constant again, moved verbatim; tracked for a de-flake pass
            importlib.reload(reg)

    def test_anonymous_git_env_pins_the_platform_valid_value(self):
        # End of the wire: the env the index-originated clone actually runs with
        # carries the platform-appropriate locale, not a macOS-invalid one.
        import sys

        from kiro_crew.apps import registry as reg

        env = reg.anonymous_git_env()
        expected = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
        assert env["LC_ALL"] == expected


class TestSslMarkerIsAnchoredNotBareSubstring:
    """classifier-marker-not-substring: the TLS/SSL failure-class marker MUST
    anchor on git/curl/(open|gnu)tls TLS-error phrasing, never the bare token
    ``ssl`` — that substring also appears in a repo URL (cloning
    ``github.com/openssl/openssl`` echoes the URL in stderr), and matching it
    would mislabel an ordinary auth/not-found failure ``a TLS/SSL error
    occurred`` — the exact false-positive class this table guards against."""

    _TLS_LABEL = "a TLS/SSL error occurred"

    def test_ssl_in_repo_url_is_not_labeled_a_tls_error(self):
        from kiro_crew.apps import registry as reg

        # A not-found failure whose stderr echoes a repo URL containing "ssl"
        # (openssl) is NOT a TLS error and must NOT get the TLS label. Fails at
        # 0e3001598 (bare "ssl" substring matches the URL); passes after.
        label = reg._redacted_git_failure_class(
            "fatal: repository 'https://github.com/openssl/openssl.git/' not found"
        )
        assert label != self._TLS_LABEL

    def test_gnutls_in_repo_url_is_not_labeled_a_tls_error(self):
        from kiro_crew.apps import registry as reg

        # A not-found failure whose stderr echoes a repo URL containing the
        # library name "gnutls" (github.com/gnutls/gnutls) is NOT a TLS error.
        # The marker anchors on git's full symbol "gnutls_handshake", never the
        # bare library name, so the URL cannot trip the TLS label.
        label = reg._redacted_git_failure_class(
            "fatal: repository 'https://github.com/gnutls/gnutls.git/' not found"
        )
        assert label != self._TLS_LABEL

    def test_genuine_tls_errors_still_labeled(self):
        from kiro_crew.apps import registry as reg

        # git/curl/(open|gnu)tls real TLS-error phrasings still map to the TLS
        # label so the classifier keeps recognizing genuine transport failures.
        for stderr in (
            "fatal: unable to access 'https://example.com/x.git/': "
            "SSL certificate problem: unable to get local issuer certificate",
            "fatal: unable to access 'https://example.com/x.git/': "
            "error:0A000086:SSL routines::certificate verify failed",
            "fatal: unable to access 'https://example.com/x.git/': "
            "Unsupported SSL backend 'schannel'",
            "fatal: unable to access 'https://example.com/x.git/': "
            "gnutls_handshake() failed: Error in the pull function.",
            "fatal: unable to access 'https://example.com/x.git/': TLS handshake failed",
        ):
            assert reg._redacted_git_failure_class(stderr) == self._TLS_LABEL

    def test_no_bare_ssl_token_marker_remains(self):
        from kiro_crew.apps import registry as reg

        # Every TLS-labeled marker is a multi-token phrase, never the bare token
        # "ssl" that would match a repo URL path segment.
        tls_markers = [
            marker for marker, label in reg._GIT_FAILURE_CLASS_LABELS if label == self._TLS_LABEL
        ]
        assert tls_markers, "expected at least one TLS marker"
        assert "ssl" not in tls_markers
        assert "gnutls" not in tls_markers
        for marker in tls_markers:
            # No TLS marker may appear as a substring of a repo URL that merely
            # names a TLS library (openssl, gnutls), or it would false-positive.
            assert marker not in "github.com/openssl/openssl.git"
            assert marker not in "github.com/gnutls/gnutls.git"


class TestRefErrorMarkerPrecision:
    """auth-marker-precision: the ref-error failure-class marker is anchored on
    git's own ref-error phrasing (``remote ref``), so an unrelated failure that
    merely contains ``does not exist`` is not mislabeled a missing ref."""

    def test_non_ref_does_not_exist_is_not_labeled_a_ref_error(self):
        from kiro_crew.apps import registry as reg

        # A pathspec/path error that contains "does not exist" but is NOT a git
        # ref error must NOT be classified as the requested-ref failure.
        label = reg._redacted_git_failure_class(
            "fatal: pathspec 'src/missing' does not exist in the working tree"
        )
        assert label != "the requested ref does not exist"

    def test_git_ref_error_still_classified(self):
        from kiro_crew.apps import registry as reg

        # git's actual missing-ref phrasing still maps to the ref label.
        assert (
            reg._redacted_git_failure_class("fatal: couldn't find remote ref refs/heads/nope")
            == "the requested ref does not exist"
        )
        assert (
            reg._redacted_git_failure_class("error: remote ref does not exist")
            == "the requested ref does not exist"
        )


class TestSiblingFailureShapesUseCodeNotError:
    """Finding 2: the three sibling shapes carry the human sentence in `error`
    and the machine slug in `code`."""

    @pytest.mark.asyncio
    async def test_destination_not_a_checkout_slug_in_code(self, tmp_path):
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "app-source"
        dest.mkdir()
        (dest / "not-a-repo.txt").write_text("plain files", encoding="utf-8")
        log: list[str] = []
        result = await reg._git_fetch_commit(
            "https://example.com/a.git",
            "0" * 40,
            dest,
            log,
            clone_env={},
            sandbox_mode="standard",
        )
        assert result is not None and result["ok"] is False
        assert result["code"] == "destination_not_a_checkout"
        # The banner-rendered `error` is a human sentence, not the slug.
        assert result["error"] != "destination_not_a_checkout"
        assert "not a git checkout" in result["error"]
        assert "message" not in result

    @pytest.mark.asyncio
    async def test_untrusted_clone_host_slug_in_code(self, tmp_path):
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "clone-dest"
        with patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=False):
            result = await reg._git_clone_or_pull(
                "https://169.254.169.254/internal.git",
                "main",
                dest,
                [],
                index_originated=True,
            )
        assert result is not None and result["ok"] is False
        assert result["code"] == "untrusted_clone_host"
        assert result["error"] != "untrusted_clone_host"
        assert "untrusted host" in result["error"]
        assert "message" not in result

    @pytest.mark.asyncio
    async def test_existing_checkout_not_moved_aside_slug_in_code(self, tmp_path):
        from kiro_crew.apps import registry as reg

        dest = tmp_path / "source"
        (dest / ".git").mkdir(parents=True)

        async def _must_not_fetch(*a, **k):
            raise AssertionError("a refused move-aside must not reach the fetch")

        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch.object(reg, "_clone_origin_url", new=AsyncMock(return_value="https://x/y.git")),
            patch.object(reg, "_git_fetch_commit", new=_must_not_fetch),
            patch.object(reg, "_move_checkout_aside", new=AsyncMock(return_value=None)),
        ):
            result = await reg._git_clone_or_pull(
                "https://x/y.git", "main", dest, [], commit="0" * 40
            )
        assert result is not None and result["ok"] is False
        assert result["code"] == "existing_checkout_not_moved_aside"
        assert result["error"] != "existing_checkout_not_moved_aside"
        assert "could not be moved aside" in result["error"]
        assert "message" not in result

    @pytest.mark.asyncio
    async def test_originally_swapped_shapes_still_use_code(self, tmp_path):
        """Control: the two shapes swapped in the base PR are unchanged."""
        from kiro_crew.apps import registry as reg

        # unreadable_clone_origin: origin remote unreadable, refuse in place.
        dest = tmp_path / "unreadable"
        (dest / ".git").mkdir(parents=True)
        with (
            patch("kiro_crew.apps.registry.is_clone_host_trusted", return_value=True),
            patch.object(reg, "_clone_origin_url", new=AsyncMock(return_value=None)),
        ):
            result = await reg._git_clone_or_pull("https://x/y.git", "main", dest, [], commit="")
        assert result is not None and result["ok"] is False
        assert result["code"] == "unreadable_clone_origin"
        assert result["error"] != "unreadable_clone_origin"
        assert "message" not in result
