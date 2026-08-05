"""Security tests for Ops Mission Control.

These lock in the properties that are expensive to retrofit: the provider tokens
must sit on the keystone floor so the agent cannot read or overwrite its own
credentials, and token shapes must be redacted before any provider payload reaches
a model prompt, a transcript, or Slack.

The keystone test deliberately asserts against the real ``security`` module rather
than a mock. A rename of ``SECRETS_FILENAME`` that forgot to update
``_CREW_SECRET_LEAVES`` would silently drop the protection with no other symptom,
so this is the test that catches it.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from kiro_crew import security
from kiro_crew.apps.builtins.ops_mission_control.backend import secrets


class TestKeystoneProtection(unittest.TestCase):
    def test_filename_is_registered_on_the_secret_floor(self):
        """The two lists must agree — see the module docstring."""
        self.assertIn(secrets.SECRETS_FILENAME, security._CREW_SECRET_LEAVES)

    def _secret_path(self) -> str:
        return os.path.expanduser(f"~/.kiro/crew/{secrets.SECRETS_FILENAME}")

    def test_agent_file_tools_cannot_touch_it(self):
        """``is_sensitive_path`` is the shared read+write gate for agent tools."""
        self.assertTrue(security.is_sensitive_path(self._secret_path()))

    def test_agent_shell_cannot_read_it(self):
        self.assertTrue(security.is_sensitive_bash_command(f"cat {self._secret_path()}"))

    def test_agent_shell_cannot_write_it(self):
        for command in (
            f"echo pwned > {self._secret_path()}",
            f"tee {self._secret_path()}",
            f"cp /tmp/x {self._secret_path()}",
        ):
            with self.subTest(command=command):
                self.assertTrue(security.is_sensitive_bash_command(command))

    def test_every_home_prefix_is_covered(self):
        """The floor is built per home prefix — including the legacy home.

        The prefixes are read from ``security._CREW_HOME_PREFIXES`` rather than
        written as a literal ``~/.kirocrew/...``: ``test_runtime_home_write_paths``
        forbids any Python outside ``test/`` from expanding a hardcoded legacy home
        (it is how the legacy dir kept getting re-created), and these tests live
        under ``src/``. Deriving the prefixes also means a future home move is
        covered here automatically.
        """
        for prefix in security._CREW_HOME_PREFIXES:
            with self.subTest(prefix=prefix):
                path = os.path.join(os.path.expanduser("~"), prefix, secrets.SECRETS_FILENAME)
                self.assertTrue(security.is_sensitive_path(path))


class TestRedaction(unittest.IsolatedAsyncioTestCase):
    """IsolatedAsyncioTestCase rather than bare ``asyncio.run``: the spawn audit
    (``test/test_spawn_audit.py``) scans for ``asyncio.<spawn attr>`` across the package
    and ``asyncio.run`` trips it — the same convention the other async tests here use."""

    def test_pagerduty_token_shape(self):
        out = secrets.redact_tokens("Authorization: Token token=u+AbCdEfGhIjKlMnOpQrStUv")
        self.assertNotIn("AbCdEfGhIjKlMnOpQrStUv", out)
        self.assertIn(secrets.REDACTED_PLACEHOLDER, out)

    def test_datadog_api_key_shape(self):
        key = "a" * 32
        out = secrets.redact_tokens(f"DD-API-KEY: {key}")
        self.assertNotIn(key, out)

    def test_datadog_app_key_shape(self):
        key = "b" * 40
        out = secrets.redact_tokens(f"app key {key} trailing")
        self.assertNotIn(key, out)

    def test_datadog_prefixed_keys_are_redacted(self):
        """Newer Datadog tenants issue ``ddapp_``/``ddapi_`` keys, not bare hex.

        Found with a live tenant. Every fixture above uses bare hex (``"a" * 32``),
        so the hex patterns matched and the suite looked complete — while the shape
        real users actually hold was covered by nothing. A prefixed key is not hex,
        so neither hex pattern applies to it.
        """
        # Synthetic values only. The shapes are what matter, and a fixture must never
        # be a real credential — the point of this test is that keys do not travel.
        for key in ("ddapp_" + "Aa1" * 11, "ddapi_" + "x" * 32):
            with self.subTest(key=key[:9]):
                self.assertNotIn(key, secrets.redact_tokens(f"key is {key} here"))

    def test_the_dd_application_key_header_is_a_carrier(self):
        """``app[_-]?key`` does NOT match ``DD-APPLICATION-KEY``.

        That is the header Datadog documents, so it is the one an adapter author is
        most likely to echo into an error string or a reproduced ``curl`` line. With
        a prefixed key the hex patterns miss the value and this carrier is the only
        thing left — verified leaking before ``application[_-]?key`` was added.
        """
        secret = "SomeOpaqueValueThatIsLongEnough"
        out = secrets.redact_tokens(f'-H "DD-APPLICATION-KEY: {secret}"')
        self.assertNotIn(secret, out)

    def test_application_keyword_in_prose_is_not_redacted(self):
        """The carrier must not fire on ordinary words containing "application"."""
        for text in ("the application keyboard shortcut", "apply key rotation quarterly"):
            with self.subTest(text=text):
                self.assertEqual(secrets.redact_tokens(text), text)

    def test_bearer_carrier(self):
        out = secrets.redact_tokens("Bearer: sk-abcdefghijklmnop")
        self.assertIn(secrets.REDACTED_PLACEHOLDER, out)

    def test_bearer_without_a_colon_is_redacted(self):
        """The single most common real form — `Authorization: Bearer sk-...`.

        The pattern required `[:=]` after the keyword, so a token following a SPACE (which
        is what RFC 6750 actually specifies) passed through. Found by handing a leaky
        evidence adapter four credential shapes: AKIA, PagerDuty and Datadog were redacted
        and this one reached the investigation brief — the model's prompt — in clear text.
        Core's pattern requires the literal `Authorization` by design, so it does not catch
        this shape either — but core is not the weak link: it matches real vendor keys
        (`sk-proj-`, `sk-ant-`, `xoxb-`, `sk_live_`, `ghp_`, JWT, AKIA) by their own
        patterns whatever precedes them. Only a prefix-less opaque token reaches this
        app-level carrier check.
        """
        for text in (
            "Bearer sk-abcdefghijklmnopqrst",
            "Authorization: Bearer sk-abcdefghijklmnopqrst",
            "authorization: bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        ):
            with self.subTest(text=text):
                out = secrets.redact_tokens(text)
                self.assertNotIn("sk-abcdefghijklmnopqrst", out)
                self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", out)
                self.assertIn(secrets.REDACTED_PLACEHOLDER, out)

    def test_the_separator_form_still_works(self):
        """Widening the separator must not lose the `token=value` case."""
        out = secrets.redact_tokens("api_key=abcdefghijklmnopqrstuvwx")
        self.assertNotIn("abcdefghijklmnopqrstuvwx", out)

    def test_a_keyword_in_prose_is_not_redacted(self):
        r"""`\s+` is alternated with `\s*[:=]\s*` deliberately.

        A bare `\s*` separator would let `token` followed by any 12+ non-space chars match
        ordinary prose and redact real diagnostic text — which would corrupt the diagnosis
        this app exists to produce.
        """
        for text in (
            "the tokenization step failed on row 42",
            "bearer of bad news: the pipeline stalled",
        ):
            with self.subTest(text=text):
                self.assertEqual(secrets.redact_tokens(text), text)

    async def test_a_leaky_adapter_cannot_reach_the_investigation_brief(self):
        """The property that matters, asserted end to end rather than on the regex.

        `gather_evidence` is the single funnel out of every adapter precisely so an adapter
        author cannot forget. This drives a deliberately careless adapter through it and
        into `investigation_brief`, which is what reaches the model.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import (
            dispatch,
            registry,
            store,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
            Evidence,
            EvidenceBudget,
        )

        # REAL vendor prefixes, not invented ones. The first version of this test used a
        # fabricated `sk-abcdef…` that matches no provider's actual format, which made core
        # look broken when it was not — the leak was genuine but the diagnosis overreached.
        leaked = {
            "akia": "AKIAIOSFODNN7EXAMPLE",
            "pagerduty": "u+AbCdEfGhIjKlMnOpQrStUv",
            "datadog": "d" * 32,
            "openai_bearer": "Bearer sk-proj-AbCdEfGhIjKlMnOpQrStUv",
            "anthropic_bearer": "Bearer sk-ant-AbCdEfGhIjKlMnOpQrStUv",
            "github_bearer": "Bearer ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123",
            "slack_bearer": "Bearer xoxb-123456789012-abcdefghijkl",
            "jwt_bearer": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijkl",
            # The residual case: opaque, no recognizable vendor prefix. Core cannot match
            # this on shape alone, so the app's carrier pattern is the only thing that does.
            "opaque_bearer": "Bearer Zm9vYmFyYmF6cXV1eGNvcmdlZ3JhdWx0",
        }

        class _Leaky:
            id = "leaky-test"
            display_name = "Leaky"

            def configured(self):
                return True

            async def gather(self, signal, budget):
                body = "\n".join(f"{k}={v}" for k, v in leaked.items())
                return [Evidence(source=self.id, kind="logs", title="raw dump", body=body)]

        import os
        import tempfile

        tmp = tempfile.mkdtemp()
        prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = tmp
        registry.reset_registry()
        reg = registry.get_registry()
        reg.register_evidence_source(_Leaky())
        try:
            signal = Signal.create(
                source="cloudwatch", native_id="alarm/leak-redaction", title="t", resource="q"
            )
            evidence = await reg.gather_evidence(signal, EvidenceBudget())
            incident = store.claim(signal, operating_mode="observe")
            assert incident is not None
            claimed = dispatch.ClaimedIncident(incident=incident, evidence=list(evidence))
            brief = dispatch.investigation_brief(claimed)
            for name, secret in leaked.items():
                with self.subTest(credential=name):
                    self.assertNotIn(secret, brief, f"{name} leaked into the model prompt")
        finally:
            registry.reset_registry()
            if prev is None:
                os.environ.pop("KIROCREW_HOME", None)
            else:
                os.environ["KIROCREW_HOME"] = prev
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ordinary_text_survives(self):
        """Redaction must not mangle a normal diagnosis."""
        text = "RDS connections hit 800 of 1000; the pool is not being released."
        self.assertEqual(secrets.redact_tokens(text), text)

    def test_empty_input(self):
        self.assertEqual(secrets.redact_tokens(""), "")


class TestSecretBackend(unittest.TestCase):
    """The store is write-only over the API: values go in, only names come out."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = Path(tempfile.mkdtemp())
        self.backend = secrets.KeystoneFileBackend(self.tmp / "secrets.json")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_put_then_get(self):
        self.backend.put("pagerduty", "api_token", "u+secretvalue")
        self.assertEqual(self.backend.get("pagerduty", "api_token"), "u+secretvalue")

    def test_missing_returns_empty_not_raise(self):
        self.assertEqual(self.backend.get("nope", "nope"), "")

    def test_configured_fields_reports_names_only(self):
        self.backend.put("datadog", "api_key", "x" * 32)
        fields = self.backend.configured_fields("datadog")
        self.assertEqual(fields, frozenset({"api_key"}))

    def test_blank_value_is_not_configured(self):
        self.backend.put("datadog", "api_key", "")
        self.assertNotIn("api_key", self.backend.configured_fields("datadog"))

    def test_delete_removes_all_fields(self):
        self.backend.put("datadog", "api_key", "x" * 32)
        self.backend.put("datadog", "app_key", "y" * 40)
        self.assertTrue(self.backend.delete("datadog"))
        self.assertEqual(self.backend.configured_fields("datadog"), frozenset())

    def test_delete_unknown_is_false(self):
        self.assertFalse(self.backend.delete("never-configured"))

    def test_file_is_owner_only(self):
        """A world-readable token file would defeat the whole design."""
        from kiro_crew import platform_compat

        self.backend.put("pagerduty", "api_token", "u+secretvalue")
        path = self.tmp / "secrets.json"
        self.assertTrue(path.exists())
        if platform_compat.IS_POSIX:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_file_degrades_to_empty(self):
        (self.tmp / "secrets.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.backend.get("pagerduty", "api_token"), "")


class TestDescribeSecrets(unittest.TestCase):
    def test_never_returns_a_value(self):
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mkdtemp())
        backend = secrets.KeystoneFileBackend(tmp / "s.json")
        backend.put("pagerduty", "api_token", "u+thisisthesecret")
        secrets.register_secret_backend(backend)
        try:
            described = secrets.describe_secrets("pagerduty", ("api_token",))
            self.assertEqual(described["api_token"], secrets.REDACTED_PLACEHOLDER)
            self.assertNotIn("thisisthesecret", str(described))
        finally:
            secrets.register_secret_backend(secrets.KeystoneFileBackend())
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class TestCrossPlatform(unittest.TestCase):
    """AGENTS.md requires macOS + Linux + Windows for every change.

    This app spawns two external binaries (`git` for ledger sync, `gh` for the rotation
    login) and does timezone math, which is where the Windows differences actually bite.
    Asserted from source rather than by running on Windows, because CI here is POSIX —
    the point is to catch a raw POSIX call at review time, not to simulate the platform.
    """

    APP_FILES = ("backend/ledger_sync.py", "backend/providers/schedule_file.py")

    def _sources(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger_sync

        root = Path(ledger_sync.__file__).resolve().parent.parent
        return {name: (root / name).read_text(encoding="utf-8") for name in self.APP_FILES}

    def test_preexec_fn_comes_from_the_shim_not_a_raw_callable(self):
        """``preexec_fn`` is unsupported on Windows — passing ANY callable raises.

        ``resource_limit_preexec()`` returns ``None`` off POSIX, so routing through it is
        what makes these spawns portable. A hand-rolled ``preexec_fn=lambda: ...`` would
        work locally and raise ValueError on every Windows spawn.
        """
        for name, src in self._sources().items():
            if "preexec_fn" not in src:
                continue
            with self.subTest(file=name):
                self.assertIn("resource_limit_preexec", src)
                for line in src.splitlines():
                    if "preexec_fn=" in line:
                        self.assertIn(
                            "resource_limit_preexec()",
                            line,
                            f"{name}: preexec_fn must come from the shim (Windows-safe)",
                        )

    def test_no_raw_posix_process_calls(self):
        """Per the platform_compat shim table. `os.kill(pid, 0)` TERMINATES on Windows."""
        banned = ("os.killpg", "os.getpgid", "os.getuid", "fcntl.", "signal.SIGKILL")
        for name, src in self._sources().items():
            with self.subTest(file=name):
                for token in banned:
                    self.assertNotIn(token, src, f"{name} uses POSIX-only {token}")

    def test_no_posix_only_paths_or_shell(self):
        """A hardcoded `/bin/sh` or `/tmp` is a Windows failure and a sandbox bypass."""
        for name, src in self._sources().items():
            with self.subTest(file=name):
                self.assertNotIn("/bin/sh", src)
                self.assertNotIn("shell=True", src)
                self.assertNotIn('"/tmp/', src)

    def test_timezone_lookup_degrades_instead_of_raising(self):
        """Windows ships no system IANA database, so `ZoneInfo(...)` can raise.

        `tzdata` is a declared Windows dependency, but an install that somehow lacks it
        must still resolve a rotation rather than crash the 5-minute cron. Verified by
        making the import itself fail, which is the shape of the real failure.
        """
        import builtins
        from datetime import datetime, timezone
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import (
            schedule_file,
        )

        tmp = Path(tempfile.mkdtemp())
        prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(tmp)
        try:
            path = schedule_file.schedule_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "timezone: America/Los_Angeles\n"
                "shifts:\n  - from: 2026-08-01\n    to: 2026-08-08\n    who: octocat\n",
                encoding="utf-8",
            )
            real_import = builtins.__import__

            def _no_zoneinfo(name, *args, **kwargs):
                if name == "zoneinfo":
                    raise ImportError("No module named 'zoneinfo'")
                return real_import(name, *args, **kwargs)

            with mock.patch.object(schedule_file, "_resolve_login_sync", return_value="octocat"):
                with mock.patch.object(builtins, "__import__", _no_zoneinfo):
                    status = schedule_file.resolve_now(
                        datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
                    )
            # A definitive answer, not the fail-open "unknown" — the window still resolves,
            # just in UTC.
            self.assertTrue(status.on_shift)
            self.assertFalse(status.unknown)
            self.assertEqual(status.who, "octocat")
        finally:
            if prev is None:
                os.environ.pop("KIROCREW_HOME", None)
            else:
                os.environ["KIROCREW_HOME"] = prev
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class TestTheScheduleIsWriteProtectedButReadable(unittest.TestCase):
    """`rotation.yaml` decides who is on call, so the agent must not be able to rewrite it.

    It is an INPUT TO AN AUTHORIZATION DECISION: an agent that names its own login in the
    schedule makes `rotation.authorize_action` -> `_definitely_off_shift` accept a forged shift
    and execute a production write against a teammate's incident tooling. Unlike `config.json`,
    whose inflated values the loader clamps, nothing downstream neutralizes a forged entry.
    Found in review — the fifth instance of one class on this refusal (the others: the GitHub
    login, the strict-gating flag, the provider `config_fields` list, and
    `providers.<id>.enabled`).

    The review proposed excluding `schedule-file` from authorization votes instead. That would
    have deleted the app's single-owner model: for a team without a rotation service the
    committed schedule IS the rotation, so ignoring it means every instance claims every alarm.
    The defect is PLACEMENT, not logic — so the file moves onto the write-protected floor and
    the voting logic is untouched.

    WRITE-protected, not read+write sensitive. That asymmetry is the point and is asserted in
    both directions below: every teammate's instance must READ the file to answer "am I on
    call?", and it holds no secret, so classifying it as sensitive would break the feature it
    exists to serve.
    """

    def _path(self) -> str:
        from kiro_crew.apps.builtins.ops_mission_control.backend.providers import schedule_file

        return str(schedule_file.schedule_path())

    def test_agent_file_tools_cannot_write_it(self):
        self.assertTrue(security.is_sensitive_write_path(self._path()))

    def test_agent_file_tools_can_still_read_it(self):
        """The whole rotation feature depends on this staying True."""
        self.assertFalse(
            security.is_sensitive_path(self._path()),
            "the schedule must stay readable — the app reads it on every rotation check",
        )

    def test_the_shell_path_is_closed_too(self):
        """The tool gate is primary, but a shell write bypasses it entirely.

        Asserted across write FORMS rather than one verb: the matcher is deliberately
        verb-independent, because a narrow allowlist is bypassable by a quoted redirect, `cp`,
        or any novel write verb.

        Spelled with POSIX separators (`as_posix`), which is what the gate matches and what a
        bash command carries. A native `WindowsPath` renders all-backslash and matches nothing —
        a whole-gate limitation on `security`'s home-anchored patterns, not specific to this
        leaf, so pinning it here would assert a fix this file does not own.
        """
        path = Path(self._path()).as_posix()
        for cmd in (
            f"echo 'who: attacker' > {path}",
            f"cp /tmp/evil.yaml {path}",
            f"tee {path}",
            f"""python -c "open('{path}','w').write('x')" """,
            f"sed -i s/alice/attacker/ {path}",
            f"mv /tmp/evil.yaml {path}",
        ):
            with self.subTest(cmd=cmd[:40]):
                self.assertTrue(
                    security.is_sensitive_bash_command(cmd),
                    f"shell write not blocked: {cmd!r}",
                )

    def test_the_registered_path_is_not_a_bare_filename(self):
        """A bare `rotation.yaml` entry matches NOTHING, which is the trap here.

        The bash matcher builds `<home>/<crew-prefix>/<entry>`, so an entry has to carry its
        `apps/.../data/` subpath. Spelling it as a bare leaf enforced nothing while reading
        exactly like a completed fix — so this pins the shape, not just the behaviour.
        """
        entries = [e for e in security._WRITE_PROTECTED_BASH_LEAVES if "rotation.yaml" in e]
        self.assertEqual(len(entries), 1, "the schedule must be registered exactly once")
        self.assertTrue(
            entries[0].endswith("apps/ops-mission-control/data/rotation.yaml"),
            f"entry must be the home-relative PATH, not a bare filename: {entries[0]!r}",
        )


class TestEveryRedactionSinkUsesTheSameSeam(unittest.TestCase):
    """All five egress paths must redact through ``platform.redact_via_context``.

    Two of them (`registry.gather_evidence`, `dispatch.investigation_brief`) already did, and
    their docstrings give the reason: the shim makes a loaded companion's declared credential
    patterns apply, and an enterprise host that FAILS to compose its companion fails CLOSED
    rather than silently falling back to public patterns. The other three — the Slack board,
    desktop notifications, and the per-incident postmortem — called `security.redact` directly,
    so a companion-only credential shape was masked on two paths and not the other three.
    Found in review.

    Structural rather than behavioural: the divergence is invisible in the public edition,
    where the shim just calls the core function. Only the SHAPE of the call distinguishes them,
    so only the shape can be asserted — a behavioural test would pass either way here and fail
    only on a companion host nobody runs in CI.
    """

    #: Every module that redacts before sending text off-box.
    SINK_MODULES = ("slack_out", "notify_out", "store", "registry", "dispatch")

    def _source(self, name: str) -> str:
        import importlib
        import inspect

        mod = importlib.import_module(
            f"kiro_crew.apps.builtins.ops_mission_control.backend.{name}"
        )
        return inspect.getsource(mod)

    def test_no_sink_imports_the_core_redactor_directly(self):
        for name in self.SINK_MODULES:
            with self.subTest(module=name):
                source = self._source(name)
                self.assertNotIn(
                    "from kiro_crew.security import redact",
                    source,
                    f"{name} bypasses the CPP redaction seam — a companion's patterns would "
                    "not apply and an enterprise host would fail OPEN",
                )

    def test_every_sink_reaches_redact_via_context(self):
        """The converse: not importing the core is not the same as using the shim."""
        for name in self.SINK_MODULES:
            with self.subTest(module=name):
                self.assertIn("redact_via_context", self._source(name))

    def test_both_pattern_families_are_still_masked(self):
        """The seam must not have cost coverage: core AND app shapes both still go.

        `redact` knows AWS keys; `redact_tokens` knows the provider shapes (a prefixed Datadog
        app key, a PagerDuty token). Neither is a superset, which is why every sink runs both.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import slack_out

        core_shape = slack_out._safe("alarm AKIAIOSFODNN7EXAMPLE fired")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", core_shape)

        app_shape = slack_out._safe("key ddapp_0123456789abcdef0123456789abcdef01234567")
        self.assertNotIn("ddapp_0123456789abcdef", app_shape)


class TestTheIncidentIndexIsWriteProtectedButReadable(unittest.TestCase):
    """The index decides WHICH signal the autonomy gate authorizes, so the agent must not
    be able to rewrite it.

    `/incident/action` looks the incident up by id and hands `incident.signal` to
    `rotation.authorize_action`, whose `AutonomyRule.matches` keys on
    `signal.source`/`resource`/`labels`. With the index writable, an agent can pair a
    resource an operator's rule authorizes (`resource="prod-db-1"` matching
    `resource_glob="prod-*"`) with a DIFFERENT provider target in `labels`: the gate
    approves one signal while the sink mutates another, so the authorization describes a
    signal that does not exist.

    Same defect already fixed on `/incident/claim` by resolving the signal server-side —
    but that cannot help here, because the store IS the server's copy. The fix is
    placement, like the schedule before it. Found in review (GPT 5.6).

    The read/write asymmetry is asserted in BOTH directions and is the point: the index is
    the board every instance renders and holds no secret, so classifying it sensitive
    would break the app.
    """

    def _path(self) -> str:
        from kiro_crew.apps.builtins.ops_mission_control.backend import store

        return str(store.index_path())

    def test_agent_file_tools_cannot_write_it(self):
        self.assertTrue(security.is_sensitive_write_path(self._path()))

    def test_agent_file_tools_can_still_read_it(self):
        """The whole board depends on this staying True."""
        self.assertFalse(
            security.is_sensitive_path(self._path()),
            "the index must stay readable — every dispatch cycle and board render reads it",
        )

    def test_the_shell_path_is_closed_too(self):
        """The tool gate is primary, but a shell write bypasses it entirely.

        Spelled with POSIX separators and the home forms the matcher anchors on (`~`,
        `$HOME`, `/home/<user>`, `/Users/<user>`) — see the schedule's equivalent test for
        why a native `WindowsPath` is not used here.
        """
        for home in ("~", "$HOME", "/home/alice", "/Users/alice"):
            path = f"{home}/.kiro/crew/apps/ops-mission-control/data/incidents/index.json"
            for cmd in (
                f"echo '{{}}' > {path}",
                f"cp /tmp/evil.json {path}",
                f"tee {path}",
                f"""python -c "open('{path}','w').write('x')" """,
                f"sed -i s/a/b/ {path}",
                f"mv /tmp/evil.json {path}",
            ):
                with self.subTest(cmd=cmd[:44]):
                    self.assertTrue(
                        security.is_sensitive_bash_command(cmd),
                        f"shell write not blocked: {cmd!r}",
                    )

    def test_the_shell_matcher_is_verb_independent_including_reads(self):
        """A shell READ is blocked too, and that is the documented trade, not an oversight.

        `_WRITE_PROTECTED_BASH_LEAVES` is matched verb-independently on purpose — a narrow
        write-verb allowlist is bypassable by a quoted redirect or any novel verb — so
        `cat` on this path is refused as well. Harmless here for the same reason it is
        harmless for the schedule: the file holds no secret, and the legitimate readers
        (the app itself, the board, the dispatch cycle) go through Python, not a shell.

        The TOOL gate is where the read/write asymmetry actually lives, and
        `test_agent_file_tools_can_still_read_it` above pins it. Asserted rather than left
        implicit so the next reader does not "fix" this into a write-only matcher.
        """
        path = "~/.kiro/crew/apps/ops-mission-control/data/incidents/index.json"
        self.assertIsNotNone(security.is_sensitive_bash_command(f"cat {path}"))
        # And identical to the leaf registered before it, so the two cannot drift.
        schedule = "~/.kiro/crew/apps/ops-mission-control/data/rotation.yaml"
        self.assertIsNotNone(security.is_sensitive_bash_command(f"cat {schedule}"))

    def test_the_registered_path_is_not_a_bare_filename(self):
        """A bare `index.json` entry would match nothing — and `index.json` is a name common
        enough that a bare entry would ALSO be wrong in the other direction."""
        entries = [
            e for e in security._WRITE_PROTECTED_BASH_LEAVES if e.endswith("incidents/index.json")
        ]
        self.assertEqual(len(entries), 1, "the index must be registered exactly once")
        self.assertEqual(entries[0], "apps/ops-mission-control/data/incidents/index.json")
