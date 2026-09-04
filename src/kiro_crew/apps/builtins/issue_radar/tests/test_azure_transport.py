"""Azure DevOps TRANSPORT layer: argv assembly, spawn hardening, body files, paging.

The companion to ``test_azure.py``, which covers URL parsing, WIQL safety and the
normalization ABOVE this layer. Everything here sits BELOW ``_az_invoke`` -- the
seam every other test in this app mocks -- so no test above it can observe these
behaviours, and a defect in one of them is silent rather than loud:

  * A value folded into the wrong argv element. ``route`` and ``query`` become
    ``key=value`` elements after their own flag; a value carrying a space or an
    ``&`` must stay one element, because that is the entire reason this module
    has no shell-injection surface.
  * A credential forwarded to a host it was not issued for.
    ``AZURE_DEVOPS_EXT_PAT`` is a single ambient token with no host binding, so
    ``_az_env`` forwards it only for the pinned cloud host.
  * A request body left on disk, or left world-readable, or shared between two
    concurrent calls. Azure's REST passthrough reads a body from a FILE, so every
    body is materialized -- privately, uniquely, and removed either way.
  * A page walk with no ceiling, which turns one pathological project into an
    unbounded request.
  * A failure classified as the wrong KIND. ``not_installed`` /
    ``not_authenticated`` / forbidden / generic all render differently in the
    connect dialog, and only the exit code and the stderr tail distinguish them.

No test here reaches the network or needs the ``az`` CLI. ``_az_bin`` imports its
two resolver helpers at CALL time, so they are patched on
``kiro_crew.dashboard.handlers.source_providers``; the spawn tests patch
``azure_client.subprocess.run``. A host that happens to have -- or lack -- a real
``az`` therefore changes no result here.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pytest

from kiro_crew.apps.builtins.issue_radar.backend import azure_client
from kiro_crew.apps.builtins.issue_radar.backend.errors import (
    ProviderCliError,
    ProviderPermissionError,
    ProviderSetupError,
)
from kiro_crew.dashboard.handlers import source_providers
from kiro_crew.github_runner import STRICT_PROVIDER_BIN_ENV

HOST = "dev.azure.com"


def _proc(
    *, returncode: int = 0, stdout: str = "{}", stderr: str = ""
) -> subprocess.CompletedProcess:
    """A finished ``az`` process, as ``subprocess.run(text=True)`` returns one."""
    return subprocess.CompletedProcess(
        args=["az", "devops", "invoke"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _executable(directory: str, name: str = "az") -> str:
    """An executable file that is NOT az -- only its path and mode are ever read."""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\nexit 0\n")
    # Add ONLY the owner-execute bit to whatever mode the file was created with,
    # rather than naming a mode: the resolver just needs `os.access(path, X_OK)` to
    # hold for this user, and this widens nothing else. It also matches how the
    # other suites make a fixture executable (see test_source_launcher.py).
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


class TestOrgUrl(unittest.TestCase):
    """``--org`` is built here, so the host cannot come from the organization."""

    def test_the_org_url_is_always_on_the_pinned_host(self):
        self.assertEqual(azure_client._org_url("contoso"), "https://dev.azure.com/contoso")

    def test_facade_quote_patch_seam_remains_authoritative(self):
        with mock.patch.object(azure_client, "quote", return_value="encoded") as quote:
            self.assertEqual(azure_client._org_url("contoso"), "https://dev.azure.com/encoded")
        quote.assert_called_once_with("contoso", safe="")

    def test_the_org_becomes_exactly_one_path_segment(self):
        # Encoded with safe='', so a separator inside the name cannot add a
        # segment and retarget the call at a different organization. The segment
        # charset already refuses a slash, which makes this the second line of
        # defense rather than the only one -- and the one that still holds if a
        # future caller reaches _org_url without going through _split_owner.
        self.assertEqual(azure_client._org_url("a/b"), "https://dev.azure.com/a%2Fb")
        self.assertEqual(azure_client._org_url("My Org"), "https://dev.azure.com/My%20Org")


class TestSplitOwner(unittest.TestCase):
    """``owner`` carries TWO independent Azure names and must yield both."""

    def test_an_owner_without_a_slash_is_refused_rather_than_guessed_at(self):
        # Defaulting the project to the organization would read a DIFFERENT
        # project's work items and look entirely successful doing it.
        for bad in ("contoso", "", "   ", "/", "//"):
            with self.subTest(bad=bad), self.assertRaises(ProviderCliError):
                azure_client._split_owner(bad)

    def test_a_well_formed_owner_yields_the_organization_and_the_project(self):
        self.assertEqual(azure_client._split_owner("contoso/Widgets"), ("contoso", "Widgets"))

    def test_surrounding_whitespace_and_slashes_are_stripped(self):
        # The owner arrives from a stored config entry, so a stray delimiter must
        # not become part of a name that is then compared case-sensitively
        # against the connected-repo record.
        for raw in (
            "  contoso/Widgets  ",
            "/contoso/Widgets",
            "contoso/Widgets/",
            "contoso / Widgets",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(azure_client._split_owner(raw), ("contoso", "Widgets"))

    def test_a_third_path_segment_is_refused(self):
        # The split takes the FIRST slash only, so a three-part value would put a
        # slash inside the project name -- which the charset then refuses. The
        # alternative (silently keeping "Widgets/extra" as a project) would reach
        # a route parameter and address something else.
        with self.assertRaises(ProviderCliError):
            azure_client._split_owner("contoso/Widgets/extra")

    def test_a_space_is_accepted_because_a_name_never_becomes_a_shell_string(self):
        # Azure genuinely allows spaces in project names, and every value reaches
        # az as its own argv element, so refusing one would lock real projects out.
        self.assertEqual(
            azure_client._split_owner("con toso/My Project"), ("con toso", "My Project")
        )

    def test_the_segment_charset_refuses_anything_that_could_act_as_a_separator(self):
        # Each of these would otherwise reach a REST route parameter, a query
        # string, or a cache path segment.
        for bad in (
            "contoso/",
            "contoso/wid?gets",
            "contoso/wid#gets",
            "contoso/wid&gets",
            "contoso/wid'gets",
            "contoso/wid\\gets",
            "contoso/wid:gets",
            "contoso/wid%20gets",
            "contoso/..",
            "contoso/.",
        ):
            with self.subTest(bad=bad), self.assertRaises(ProviderCliError):
                azure_client._split_owner(bad)

    def test_a_reserved_routing_segment_is_not_a_name(self):
        # "_apis" as a project would build a URL addressing the REST API root.
        for bad in ("_git/Widgets", "contoso/_apis", "contoso/_workitems"):
            with self.subTest(bad=bad), self.assertRaises(ProviderCliError):
                azure_client._split_owner(bad)

    def test_a_name_must_not_open_with_a_punctuation_character(self):
        for bad in ("contoso/.hidden", "contoso/-dash", "contoso/_under", ".org/Widgets"):
            with self.subTest(bad=bad), self.assertRaises(ProviderCliError):
                azure_client._split_owner(bad)

    def test_the_length_ceiling_is_inclusive_at_sixty_four_characters(self):
        # Asserted at the boundary in both directions: an off-by-one here either
        # rejects a legal Azure project or lets an unbounded name into a path.
        ok = "a" * 64
        self.assertEqual(azure_client._split_owner(f"contoso/{ok}")[1], ok)
        with self.assertRaises(ProviderCliError):
            azure_client._split_owner(f"contoso/{'a' * 65}")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the POSIX binary-resolution path -- _az_bin refuses Windows outright",
)
class TestAzBinResolution(unittest.TestCase):
    """``_az_bin`` decides WHICH binary runs with the user's Azure session.

    The cache is a module global, so it is cleared around every test: a leaked
    value would make a later test assert against a path this one chose.
    """

    def setUp(self):
        azure_client._az_bin_cache = None

    def tearDown(self):
        azure_client._az_bin_cache = None

    @staticmethod
    def _resolver(*, candidates=(), validate=None):
        """Patch both resolver helpers ``_az_bin`` imports at call time."""
        return mock.patch.multiple(
            source_providers,
            provider_executable_candidates=mock.Mock(return_value=tuple(candidates)),
            _validate_provider_executable=validate or mock.Mock(side_effect=lambda path: path),
        )

    def test_the_override_is_validated_and_its_canonical_path_is_returned(self):
        # The validator returns the RESOLVED path, and that -- not the raw env
        # value -- is what must be spawned, or a symlink swap after validation
        # would run a different file.
        validate = mock.Mock(return_value="/opt/az/real/az")
        with mock.patch.dict(os.environ, {"KIROCREW_ISSUE_RADAR_AZ": "/opt/az/az"}, clear=False):
            with self._resolver(validate=validate):
                self.assertEqual(azure_client._az_bin(), "/opt/az/real/az")
        validate.assert_called_once_with("/opt/az/az")

    def test_a_resolved_binary_is_cached_so_the_validation_walk_runs_once(self):
        # Validation is a stat-heavy walk of every parent directory, and this runs
        # on every list refresh.
        validate = mock.Mock(side_effect=lambda path: path)
        with mock.patch.dict(os.environ, {"KIROCREW_ISSUE_RADAR_AZ": "/opt/az/az"}, clear=False):
            with self._resolver(validate=validate):
                first = azure_client._az_bin()
                second = azure_client._az_bin()
        self.assertEqual(first, second)
        self.assertEqual(validate.call_count, 1)

    def test_an_unusable_override_is_a_setup_error_and_never_falls_back(self):
        # Falling back to some other az would run a binary the operator did not
        # name, which defeats the point of naming one. Both failure types the
        # validator raises must land on the same answer.
        for error in (ValueError("not canonical"), OSError("unreadable")):
            with self.subTest(error=type(error).__name__):
                azure_client._az_bin_cache = None
                candidates = mock.Mock(return_value=("/usr/bin/az",))
                with mock.patch.dict(
                    os.environ, {"KIROCREW_ISSUE_RADAR_AZ": "/tmp/az"}, clear=False
                ):
                    with mock.patch.multiple(
                        source_providers,
                        provider_executable_candidates=candidates,
                        _validate_provider_executable=mock.Mock(side_effect=error),
                    ):
                        with self.assertRaises(ProviderSetupError) as caught:
                            azure_client._az_bin()
                self.assertEqual(caught.exception.reason, "not_installed")
                # The message has to name the override, since the user set it and
                # is the only one who can fix it.
                self.assertIn("KIROCREW_ISSUE_RADAR_AZ", str(caught.exception))
                candidates.assert_not_called()

    def test_a_failed_resolution_is_not_cached(self):
        # The fix for "az is not installed" is to install az. Caching the failure
        # would make that require a gateway restart.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KIROCREW_ISSUE_RADAR_AZ", None)
            with self._resolver(candidates=()):
                with self.assertRaises(ProviderSetupError):
                    azure_client._az_bin()
            self.assertIsNone(azure_client._az_bin_cache)
            with tempfile.TemporaryDirectory() as tmp:
                installed = _executable(tmp)
                with self._resolver(candidates=(installed,)):
                    self.assertEqual(azure_client._az_bin(), installed)

    def test_a_candidate_that_does_not_exist_is_skipped_without_validation(self):
        # Validation raises "path does not exist" for a missing file, which would
        # be reported as the LAST CHECK and bury the real reason.
        with tempfile.TemporaryDirectory() as tmp:
            present = _executable(tmp)
            missing = os.path.join(tmp, "nowhere", "az")
            validate = mock.Mock(side_effect=lambda path: path)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("KIROCREW_ISSUE_RADAR_AZ", None)
                with self._resolver(candidates=(missing, present), validate=validate):
                    self.assertEqual(azure_client._az_bin(), present)
            validate.assert_called_once_with(present)

    def test_a_candidate_of_untrusted_provenance_is_skipped_for_the_next_one(self):
        # A world-writable or foreign-owned az must not stop resolution: the
        # user's own trustworthy install may be further down the list.
        with tempfile.TemporaryDirectory() as tmp:
            planted = _executable(tmp, "az")
            trusted = _executable(os.path.join(tmp), "az-trusted")

            def validate(path: str) -> str:
                if path == planted:
                    raise ValueError("executable is inside the agent-writable tree")
                return path

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("KIROCREW_ISSUE_RADAR_AZ", None)
                with self._resolver(
                    candidates=(planted, trusted), validate=mock.Mock(side_effect=validate)
                ):
                    self.assertEqual(azure_client._az_bin(), trusted)

    def test_when_every_candidate_is_refused_the_last_reason_is_reported(self):
        # "az was not found" and "the az you have is not trustworthy" need
        # different user actions, so the reason has to survive into the message.
        with tempfile.TemporaryDirectory() as tmp:
            planted = _executable(tmp)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("KIROCREW_ISSUE_RADAR_AZ", None)
                with self._resolver(
                    candidates=(planted,),
                    validate=mock.Mock(side_effect=ValueError("world-writable parent")),
                ):
                    with self.assertRaises(ProviderSetupError) as caught:
                        azure_client._az_bin()
        self.assertEqual(caught.exception.reason, "not_installed")
        self.assertIn("world-writable parent", str(caught.exception))

    def test_with_no_candidates_at_all_the_error_names_the_remedies(self):
        # Nothing was inspected, so there is no "last check" to report -- the
        # message must be the install/login/override instruction instead.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KIROCREW_ISSUE_RADAR_AZ", None)
            with self._resolver(candidates=()):
                with self.assertRaises(ProviderSetupError) as caught:
                    azure_client._az_bin()
        message = str(caught.exception)
        self.assertEqual(caught.exception.reason, "not_installed")
        self.assertNotIn("last check", message)
        self.assertIn("azure-devops", message)
        self.assertIn("KIROCREW_ISSUE_RADAR_AZ", message)

    def test_strict_mode_hides_an_az_that_only_exists_on_path(self):
        """Why the strict-mode error tells the user to set the override.

        Strict mode trusts system directories only and does not consult ``PATH``
        at all, so a user's Homebrew / pipx / mise install becomes invisible --
        which is the reason the override exists rather than a bug to work around.
        Asserted on the candidate list because that is where the difference is;
        ``_az_bin`` itself has no strict-mode branch.
        """
        with tempfile.TemporaryDirectory() as tmp:
            on_path = _executable(tmp)
            with mock.patch.dict(os.environ, {"PATH": tmp}, clear=False):
                os.environ.pop(STRICT_PROVIDER_BIN_ENV, None)
                self.assertIn(on_path, source_providers.provider_executable_candidates("az"))
                os.environ[STRICT_PROVIDER_BIN_ENV] = "1"
                self.assertNotIn(on_path, source_providers.provider_executable_candidates("az"))

    def test_windows_is_refused_before_any_resolution_is_attempted(self):
        # Not a ProviderSetupError: no install or login fixes it, so the connect
        # dialog must not offer one. The resolver is never consulted at all.
        candidates = mock.Mock()
        with mock.patch.object(azure_client.sys, "platform", "win32"):
            with mock.patch.object(source_providers, "provider_executable_candidates", candidates):
                with self.assertRaises(ProviderCliError) as caught:
                    azure_client._az_bin()
        self.assertNotIsInstance(caught.exception, ProviderSetupError)
        self.assertIn("WSL", str(caught.exception))
        candidates.assert_not_called()


class TestAzEnv(unittest.TestCase):
    """The child environment is an allowlist, so what is ABSENT is the assertion."""

    def test_azs_own_auth_config_and_network_vars_pass_through(self):
        # az cannot authenticate without its config root, and cannot reach a
        # corporate Azure through a proxy without these -- dropping them would
        # turn a working terminal setup into an unexplainable failure.
        ambient = {
            "AZURE_CONFIG_DIR": "/home/u/.azure",
            "AZURE_EXTENSION_DIR": "/home/u/.azure/cliextensions",
            "HTTPS_PROXY": "http://proxy.internal:3128",
            "no_proxy": "localhost",
            "REQUESTS_CA_BUNDLE": "/etc/ssl/corp.pem",
        }
        with mock.patch.dict(os.environ, ambient, clear=True):
            env = azure_client._az_env(HOST)
        for key, value in ambient.items():
            with self.subTest(key=key):
                self.assertEqual(env.get(key), value)

    def test_facade_minimal_env_patch_seam_remains_authoritative(self):
        shaped = {"SENTINEL": "facade"}
        with mock.patch.object(azure_client, "minimal_env", return_value=shaped) as build:
            self.assertIs(azure_client._az_env(HOST), shaped)
        build.assert_called_once()

    def test_the_personal_access_token_is_forwarded_only_for_the_pinned_host(self):
        # It is one ambient credential with no host binding, so forwarding it to
        # any other host would hand that server a dev.azure.com credential.
        with mock.patch.dict(os.environ, {"AZURE_DEVOPS_EXT_PAT": "secret"}, clear=True):
            self.assertEqual(azure_client._az_env(HOST).get("AZURE_DEVOPS_EXT_PAT"), "secret")
            for other in ("contoso.visualstudio.com", "evil.test", "DEV.AZURE.COM", ""):
                with self.subTest(host=other):
                    self.assertNotIn("AZURE_DEVOPS_EXT_PAT", azure_client._az_env(other))

    def test_unrelated_ambient_secrets_never_reach_az(self):
        # The whole point of the minimal env: a substituted or compromised az must
        # not be handed credentials for systems it has no business touching.
        ambient = {
            "AWS_SECRET_ACCESS_KEY": "aws",
            "SLACK_SIGNING_SECRET": "slack",
            "OPENAI_API_KEY": "openai",
            "GH_ENTERPRISE_TOKEN": "gh",
        }
        with mock.patch.dict(os.environ, ambient, clear=True):
            env = azure_client._az_env(HOST)
        for key in ambient:
            with self.subTest(key=key):
                self.assertNotIn(key, env)

    def test_dynamic_extension_install_is_disabled(self):
        """A spawn must never download and execute an extension wheel.

        With dynamic install enabled, naming an unknown command group makes az
        fetch and run code. A missing extension has to surface as an error the
        user resolves, so this is pinned as a security setting -- and pinned
        against the AMBIENT value, since an operator env that enables it must not
        win.
        """
        with mock.patch.dict(
            os.environ, {"AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "yes_without_prompt"}, clear=True
        ):
            env = azure_client._az_env(HOST)
        self.assertEqual(env["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"], "no")

    def test_telemetry_and_colour_are_off_so_output_stays_parseable(self):
        # stdout is parsed as JSON and stderr is matched against auth markers;
        # ANSI escapes break both.
        with mock.patch.dict(os.environ, {}, clear=True):
            env = azure_client._az_env(HOST)
        self.assertEqual(env["AZURE_CORE_COLLECT_TELEMETRY"], "0")
        self.assertEqual(env["AZURE_CORE_NO_COLOR"], "1")
        self.assertEqual(env["NO_COLOR"], "1")


class TestAzRunSpawnBoundary(unittest.TestCase):
    """``_az_run`` is the single spawn chokepoint: host, binary, env, audit."""

    ARGV = ["az", "devops", "invoke", "--org", "https://dev.azure.com/contoso"]

    def _spawn(self, *, run, host=HOST, argv=None, env=None):
        """Run ``_az_run`` with the binary, env and audit stubbed out.

        Returns the audit Mock so a caller can assert on the recorded outcome.
        """
        audit = mock.Mock()
        with mock.patch.object(azure_client, "_az_bin", return_value="/opt/az/az"):
            with mock.patch.object(azure_client, "_az_env", env or mock.Mock(return_value={})):
                with mock.patch.object(azure_client, "_audit", audit):
                    with mock.patch.object(azure_client.subprocess, "run", run):
                        proc = azure_client._az_run(list(argv or self.ARGV), host=host, timeout=3.0)
        return proc, audit

    def test_argv0_is_replaced_by_the_validated_binary_and_the_rest_is_untouched(self):
        # The caller writes a literal "az" it never resolved; only the validated
        # path may actually execute, and every other element must survive verbatim
        # or a query parameter would be silently dropped.
        run = mock.Mock(return_value=_proc())
        self._spawn(run=run)
        self.assertEqual(run.call_args.args[0], ["/opt/az/az", *self.ARGV[1:]])
        # Never a shell: shell=True is not passed, and the argv stays a list.
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertIs(run.call_args.kwargs["check"], False)
        self.assertEqual(run.call_args.kwargs["timeout"], 3.0)

    def test_the_child_env_is_built_for_the_RESOLVED_host(self):
        """A legally-spelled variant must not cost the user their credential.

        ``_az_env`` compares the host literally, so handing it the raw value would
        drop ``AZURE_DEVOPS_EXT_PAT`` for "DEV.AZURE.COM." -- the same
        organization, spelled differently -- and produce an authentication
        failure with nothing in the message to explain it.
        """
        env = mock.Mock(return_value={"AZURE_CONFIG_DIR": "/home/u/.azure"})
        run = mock.Mock(return_value=_proc())
        self._spawn(run=run, host="DEV.AZURE.COM.", env=env)
        env.assert_called_once_with(HOST)
        self.assertEqual(run.call_args.kwargs["env"], {"AZURE_CONFIG_DIR": "/home/u/.azure"})

    def test_an_unsupported_host_neither_spawns_nor_audits(self):
        # The host is re-checked BEFORE anything else, so a corrupted config entry
        # cannot reach another server with the user's credential -- and leaves no
        # misleading "invoked" record claiming it did.
        run = mock.Mock()
        audit = mock.Mock()
        with mock.patch.object(azure_client, "_az_bin", return_value="/opt/az/az"):
            with mock.patch.object(azure_client, "_audit", audit):
                with mock.patch.object(azure_client.subprocess, "run", run):
                    for bad in ("", "evil.test", "contoso.visualstudio.com"):
                        with self.subTest(host=bad), self.assertRaises(ProviderCliError):
                            azure_client._az_run(list(self.ARGV), host=bad, timeout=3.0)
        run.assert_not_called()
        audit.assert_not_called()

    def test_a_missing_binary_is_reported_as_not_installed(self):
        # _az_bin normally catches this first; the handler exists because the
        # binary can be removed between validation and exec, and the answer must
        # still be the actionable install instruction rather than an OSError.
        run = mock.Mock(side_effect=FileNotFoundError("no such file"))
        with self.assertRaises(ProviderSetupError) as caught:
            self._spawn(run=run)
        self.assertEqual(caught.exception.reason, "not_installed")

    def test_a_timeout_is_a_generic_cli_error_naming_the_budget(self):
        # Deliberately NOT a setup error: there is nothing for the user to install
        # or log into, so the connect dialog must not offer either.
        run = mock.Mock(side_effect=subprocess.TimeoutExpired(cmd="az", timeout=3.0))
        with self.assertRaises(ProviderCliError) as caught:
            self._spawn(run=run)
        self.assertNotIsInstance(caught.exception, ProviderSetupError)
        self.assertIn("3.0", str(caught.exception))

    def test_a_timeout_is_recorded_as_a_failure(self):
        # The "invoked" record already exists at this point, so without this the
        # audit trail would show a command that started and never ended.
        run = mock.Mock(side_effect=subprocess.TimeoutExpired(cmd="az", timeout=3.0))
        audit = mock.Mock()
        with mock.patch.object(azure_client, "_az_bin", return_value="/opt/az/az"):
            with mock.patch.object(azure_client, "_az_env", return_value={}):
                with mock.patch.object(azure_client, "_audit", audit):
                    with mock.patch.object(azure_client.subprocess, "run", run):
                        with self.assertRaises(ProviderCliError):
                            azure_client._az_run(list(self.ARGV), host=HOST, timeout=3.0)
        outcomes = [call.args[2] for call in audit.call_args_list]
        self.assertEqual(outcomes, ["invoked", "failure"])
        self.assertIn("timeout", audit.call_args.kwargs["error"])

    def test_a_nonzero_exit_is_audited_as_a_failure_and_handed_back_unraised(self):
        # Classification belongs to _az_invoke, which knows the endpoint name and
        # can read the stderr tail; raising here would lose both.
        run = mock.Mock(return_value=_proc(returncode=2, stderr="boom"))
        proc, audit = self._spawn(run=run)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual([call.args[2] for call in audit.call_args_list], ["invoked", "failure"])
        self.assertIn("exit 2", audit.call_args.kwargs["error"])

    def test_a_successful_exit_is_audited_as_ok(self):
        _, audit = self._spawn(run=mock.Mock(return_value=_proc()))
        self.assertEqual([call.args[2] for call in audit.call_args_list], ["invoked", "ok"])

    def test_the_audited_operation_names_the_command_group_only(self):
        """Route and query VALUES must not land in the security event log.

        The audited target is built from argv[1:3] alone. Those values are project
        and repository names, work item titles' ids and branch names -- data the
        log has no reason to retain, and which would make the record unbounded.
        """
        argv = ["az", "devops", "invoke", "--route-parameters", "project=Confidential Project"]
        _, audit = self._spawn(run=mock.Mock(return_value=_proc()), argv=argv)
        targets = {call.args[1] for call in audit.call_args_list}
        self.assertEqual(targets, {"az devops invoke"})


class TestFailureClassification(unittest.TestCase):
    """The stderr tail is the only signal separating three different user actions."""

    def test_auth_markers_classify_as_not_authenticated(self):
        # Matched case-insensitively, because az's own casing varies by message
        # and by Azure DevOps error code.
        for tail in (
            "ERROR: Please run 'az login' to setup account.",
            "TF400813: The user 'x' is not authorized to access this resource.",
            "az devops login --organization ...",
            "The requested operation returned 401 Unauthorized",
            "Before you can run this command you need to log in",
        ):
            with self.subTest(tail=tail):
                with self.assertRaises(ProviderSetupError) as caught:
                    azure_client._raise_if_setup_failure(tail)
                self.assertEqual(caught.exception.reason, "not_authenticated")
                # The message must name the command that fixes it.
                self.assertIn("az login", str(caught.exception))

    def test_a_missing_extension_wins_over_a_login_hint_in_the_same_message(self):
        """Order matters, and this is the case that proves it.

        az's message for an absent extension often ALSO suggests logging in.
        Telling a user to authenticate a CLI that cannot serve the command at all
        sends them down the wrong path and they never get out of it.
        """
        tail = "'devops' is not in the \"az\" command group. Also try az login."
        with self.assertRaises(ProviderSetupError) as caught:
            azure_client._raise_if_setup_failure(tail)
        self.assertEqual(caught.exception.reason, "not_installed")
        self.assertIn("az extension add", str(caught.exception))

    def test_every_missing_marker_reports_not_installed(self):
        for tail in (
            "ERROR: 'devops' is not in the \"az\" command group.",
            "run az extension add --name azure-devops",
            "extension is not installed",
            "az: command not found",
        ):
            with self.subTest(tail=tail):
                with self.assertRaises(ProviderSetupError) as caught:
                    azure_client._raise_if_setup_failure(tail)
                self.assertEqual(caught.exception.reason, "not_installed")

    def test_an_ordinary_failure_is_not_reclassified(self):
        # A 500 or a bad request is a retry, not a user action, so it must fall
        # through to the generic error rather than telling the user to log in.
        for tail in ("", "TF400898: An internal error occurred.", "Bad Request", "409 Conflict"):
            with self.subTest(tail=tail):
                try:
                    azure_client._raise_if_setup_failure(tail)
                except ProviderSetupError as exc:
                    self.fail(f"{tail!r} was misclassified as a setup problem: {exc}")

    def test_only_the_last_three_stderr_lines_are_read(self):
        # az prints a usage block ahead of the real error, so the tail is what
        # carries the signal -- and bounds how much CLI output reaches the browser.
        proc = _proc(stderr="usage: az\nline2\nfirst\nsecond\nthird\n")
        self.assertEqual(azure_client._stderr_tail(proc), "first second third")
        self.assertEqual(azure_client._stderr_tail(_proc(stderr="")), "")

    def test_facade_sanitizer_patch_seam_remains_authoritative(self):
        proc = _proc(stderr="secret")
        with mock.patch.object(
            azure_client, "sanitize_cli_stderr", return_value="clean"
        ) as sanitize:
            self.assertEqual(azure_client._stderr_tail(proc), "clean")
        sanitize.assert_called_once_with("secret")

    def test_forbidden_is_detected_by_status_and_by_prose(self):
        # Azure answers a permission problem three different ways depending on the
        # endpoint, and only this distinguishes 403 (a real answer the user can
        # act on) from a generic upstream failure.
        for tail in ("HTTP 403", "Access Denied: Forbidden", "does not have permission"):
            with self.subTest(tail=tail):
                self.assertTrue(azure_client._is_forbidden(tail))
        for tail in ("", "404 not found", "internal error"):
            with self.subTest(tail=tail):
                self.assertFalse(azure_client._is_forbidden(tail))


class TestAzInvokeArgv(unittest.TestCase):
    """Argv assembly: what az is actually asked, and how values stay values."""

    def _invoke(
        self,
        *,
        proc=None,
        area="git",
        resource="repositories",
        method="GET",
        route=None,
        query=None,
        media_type="",
        api_version="7.1",
    ):
        """Call ``_az_invoke`` with the spawn stubbed; returns (result, argv)."""
        seen: list[list[str]] = []

        def fake_run(argv, *, host, timeout):
            seen.append(list(argv))
            self.assertEqual(host, HOST)
            return proc if proc is not None else _proc()

        with mock.patch.object(azure_client, "_az_run", side_effect=fake_run):
            out = azure_client._az_invoke(
                org="contoso",
                area=area,
                resource=resource,
                host=HOST,
                api_version=api_version,
                method=method,
                route=route,
                query=query,
                media_type=media_type,
            )
        self.assertEqual(len(seen), 1)
        return out, seen[0]

    def test_the_api_version_is_always_sent_because_azs_own_default_is_too_old(self):
        # The CLI defaults to 5.0, which predates most of what this module reads
        # and answers 400 for the rest, so an omitted version is not a shape
        # difference -- it is a failed call.
        _, argv = self._invoke(api_version="7.1-preview.4")
        self.assertEqual(argv[argv.index("--api-version") + 1], "7.1-preview.4")

    def test_detection_is_always_disabled(self):
        # Detection infers the organization from the cwd's git remote, and the
        # gateway's cwd has nothing to do with the project being read.
        _, argv = self._invoke()
        self.assertEqual(argv[argv.index("--detect") + 1], "false")

    def test_the_call_is_addressed_by_org_area_resource_and_method(self):
        _, argv = self._invoke(area="wit", resource="wiql", method="POST")
        self.assertEqual(argv[:3], ["az", "devops", "invoke"])
        self.assertEqual(argv[argv.index("--org") + 1], "https://dev.azure.com/contoso")
        self.assertEqual(argv[argv.index("--area") + 1], "wit")
        self.assertEqual(argv[argv.index("--resource") + 1], "wiql")
        self.assertEqual(argv[argv.index("--http-method") + 1], "POST")
        self.assertEqual(argv[argv.index("--output") + 1], "json")

    def test_route_and_query_become_one_argv_element_per_pair(self):
        """The reason this module has no injection surface.

        Each pair is its OWN argv element, so a value containing a space, an ``&``
        or a quote is data. If any of them were joined into a single string, that
        string would be re-split -- by az on whitespace, or by a shell if one were
        ever introduced -- and the extra parameter would be silently honored.
        """
        _, argv = self._invoke(
            route={"project": "My Project", "repositoryId": "widget-service"},
            query={"searchCriteria.status": "active", "$top": 100},
        )
        route_at = argv.index("--route-parameters")
        self.assertEqual(
            argv[route_at + 1 : route_at + 3], ["project=My Project", "repositoryId=widget-service"]
        )
        query_at = argv.index("--query-parameters")
        self.assertEqual(
            argv[query_at + 1 : query_at + 3], ["searchCriteria.status=active", "$top=100"]
        )

    def test_a_hostile_value_cannot_add_a_parameter(self):
        # A project name that tries to close its own pair stays inside one
        # element, so az reads it as a (nonsense) project name rather than as a
        # second route parameter.
        _, argv = self._invoke(route={"project": "Widgets --query-parameters $top=1"})
        self.assertIn("project=Widgets --query-parameters $top=1", argv)
        self.assertEqual(argv.count("--query-parameters"), 0)

    def test_the_parameter_flags_are_omitted_when_there_is_nothing_to_pass(self):
        # An empty --route-parameters flag with no pairs after it is a CLI usage
        # error, so an empty dict must not produce the flag.
        _, argv = self._invoke(route={}, query={})
        self.assertNotIn("--route-parameters", argv)
        self.assertNotIn("--query-parameters", argv)
        self.assertNotIn("--media-type", argv)
        self.assertNotIn("--in-file", argv)

    def test_a_media_type_is_passed_only_when_the_endpoint_needs_one(self):
        # The JSON-Patch work-item endpoints refuse a plain application/json body.
        _, argv = self._invoke(media_type="application/json-patch+json")
        self.assertEqual(argv[argv.index("--media-type") + 1], "application/json-patch+json")

    def test_the_response_is_parsed_as_json(self):
        out, _ = self._invoke(proc=_proc(stdout='{"count": 1, "value": [{"id": 5}]}'))
        self.assertEqual(out, {"count": 1, "value": [{"id": 5}]})

    def test_an_empty_response_body_is_an_empty_object_not_a_parse_error(self):
        # Several Azure mutations answer 204 with no body, and az prints nothing.
        # Raising here would turn a successful write into a reported failure.
        for stdout in ("", "   \n"):
            with self.subTest(stdout=stdout):
                self.assertEqual(self._invoke(proc=_proc(stdout=stdout))[0], {})

    def test_unparseable_output_names_the_endpoint_without_echoing_the_output(self):
        # az prints an HTML error page or a progress line on some failures. That
        # text reaches the browser through the route's error body, so the message
        # carries the endpoint and not the payload.
        with self.assertRaises(ProviderCliError) as caught:
            self._invoke(proc=_proc(stdout="<html>Sign in to your account</html>"))
        message = str(caught.exception)
        self.assertIn("git/repositories", message)
        self.assertNotIn("<html>", message)

    def test_a_forbidden_failure_is_a_permission_error(self):
        # Routes map this to 403 rather than 502: the call worked, the answer was
        # "no", and a retry will not change it. The sample carries no auth marker,
        # because the unauthenticated check runs first and would otherwise claim a
        # permission failure as a login problem.
        with self.assertRaises(ProviderPermissionError) as caught:
            self._invoke(proc=_proc(returncode=1, stderr="ERROR: 403 Forbidden"))
        self.assertIn("git/repositories", str(caught.exception))

    def test_an_unauthenticated_failure_is_a_setup_error_end_to_end(self):
        # Proves the classification actually runs on this path -- the connect
        # dialog reads `reason` to decide whether to offer a login instruction.
        with self.assertRaises(ProviderSetupError) as caught:
            self._invoke(proc=_proc(returncode=1, stderr="ERROR: Please run 'az login'"))
        self.assertEqual(caught.exception.reason, "not_authenticated")

    def test_a_generic_failure_reports_the_endpoint_and_the_exit_code(self):
        with self.assertRaises(ProviderCliError) as caught:
            self._invoke(proc=_proc(returncode=3, stderr="TF400898: An internal error occurred."))
        self.assertNotIsInstance(caught.exception, ProviderSetupError)
        self.assertNotIsInstance(caught.exception, ProviderPermissionError)
        self.assertIn("exit 3", str(caught.exception))
        self.assertIn("internal error", str(caught.exception))


class TestAzInvokeBodyFile(unittest.TestCase):
    """A body is materialized on disk, so its lifetime and mode are the contract."""

    def _invoke_with_body(self, body, *, tmpdir, run):
        """Invoke with a body, forcing every temp file into ``tmpdir``.

        The temp directory is redirected so the assertions see ONLY this call's
        files -- a shared /tmp is also written by other tests running in parallel.
        """
        with mock.patch.object(tempfile, "tempdir", tmpdir):
            with mock.patch.object(azure_client, "_az_run", side_effect=run):
                return azure_client._az_invoke(
                    org="contoso",
                    area="wit",
                    resource="workitems",
                    host=HOST,
                    api_version="7.1",
                    method="PATCH",
                    body=body,
                )

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="asserts POSIX file mode bits, which Windows does not carry",
    )
    def test_the_body_reaches_az_as_a_private_file_named_by_in_file(self):
        body = [{"op": "test", "path": "/rev", "value": 7}]
        observed: dict[str, object] = {}

        def run(argv, *, host, timeout):
            path = argv[argv.index("--in-file") + 1]
            observed["path"] = path
            observed["mode"] = os.stat(path).st_mode & 0o777
            with open(path, encoding="utf-8") as handle:
                observed["body"] = json.load(handle)
            return _proc()

        with tempfile.TemporaryDirectory() as tmp:
            self._invoke_with_body(body, tmpdir=tmp, run=run)
        # 0600 from creation, never briefly world-readable: bodies here carry
        # comment prose and the commit a merge is pinned to.
        self.assertEqual(observed["mode"], 0o600)
        self.assertEqual(observed["body"], body)

    def test_the_body_file_is_removed_after_a_successful_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._invoke_with_body(
                {"text": "a note"}, tmpdir=tmp, run=lambda argv, *, host, timeout: _proc()
            )
            self.assertEqual(os.listdir(tmp), [])

    def test_the_body_file_is_removed_even_when_the_spawn_fails(self):
        # The unlink is in a `finally`, and this is the case that matters: a
        # failing call is exactly when a body would otherwise be left behind.
        def run(argv, *, host, timeout):
            raise ProviderCliError("spawn refused")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ProviderCliError):
                self._invoke_with_body({"text": "a note"}, tmpdir=tmp, run=run)
            self.assertEqual(os.listdir(tmp), [])

    def test_the_body_file_is_removed_when_the_call_is_classified_as_an_error(self):
        # The unlink happens before the returncode is even looked at, so a
        # non-zero exit must not leak the body either.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ProviderCliError):
                self._invoke_with_body(
                    {"text": "a note"},
                    tmpdir=tmp,
                    run=lambda argv, *, host, timeout: _proc(returncode=1, stderr="nope"),
                )
            self.assertEqual(os.listdir(tmp), [])

    def test_each_call_gets_its_own_path(self):
        """A fixed name would let two concurrent calls overwrite each other.

        Two requests in flight would then be able to swap bodies -- a merge
        completing with another call's payload -- and a predictable name in a
        shared temp directory is a symlink-attack target.
        """
        paths: list[str] = []

        def run(argv, *, host, timeout):
            paths.append(argv[argv.index("--in-file") + 1])
            return _proc()

        with tempfile.TemporaryDirectory() as tmp:
            for index in range(2):
                self._invoke_with_body({"n": index}, tmpdir=tmp, run=run)
        self.assertEqual(len(paths), 2)
        self.assertNotEqual(paths[0], paths[1])

    def test_a_body_that_cannot_be_serialized_leaves_nothing_behind(self):
        # The file is created before it is written, so a serialization failure has
        # to clean it up itself or every such call leaks an empty private file.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tempfile, "tempdir", tmp):
                with self.assertRaises(TypeError):
                    azure_client._body_file({"handle": object()})
                self.assertEqual(os.listdir(tmp), [])


class TestAzInvokePaged(unittest.TestCase):
    """Azure pages by offset with no next-page link, so a short page is the end."""

    @staticmethod
    def _pager(pages):
        """A fake ``_az_invoke`` answering ``pages`` in order, recording queries."""
        queries: list[dict] = []
        remaining = list(pages)

        def invoke(**kwargs):
            queries.append(dict(kwargs["query"]))
            rows = remaining.pop(0) if remaining else []
            return {"count": len(rows), "value": rows}

        return invoke, queries

    def _walk(self, invoke, *, route=None, query=None, timeout=30.0, api_version="7.1", limit=0):
        with mock.patch.object(azure_client, "_az_invoke", side_effect=invoke):
            return azure_client._az_invoke_paged(
                org="contoso",
                area="git",
                resource="pullRequests",
                host=HOST,
                timeout=timeout,
                route=route,
                query=query,
                api_version=api_version,
                limit=limit,
            )

    def test_the_walk_continues_until_a_page_comes_back_short(self):
        full = [{"id": n} for n in range(azure_client._PAGE_SIZE)]
        invoke, queries = self._pager([full, [{"id": 999}]])
        rows = self._walk(invoke)
        self.assertEqual(len(rows), azure_client._PAGE_SIZE + 1)
        self.assertEqual(rows[-1], {"id": 999})
        # Offset paging: $skip advances by the page size actually requested.
        self.assertEqual([q["$skip"] for q in queries], [0, azure_client._PAGE_SIZE])
        self.assertEqual({q["$top"] for q in queries}, {azure_client._PAGE_SIZE})

    def test_an_empty_first_page_ends_the_walk_immediately(self):
        invoke, queries = self._pager([[]])
        self.assertEqual(self._walk(invoke), [])
        self.assertEqual(len(queries), 1)

    def test_a_limit_shrinks_the_last_page_and_stops_the_walk(self):
        # Without the shrink the caller's cap would be exceeded by up to a full
        # page, and every extra row is a hydrate call the caller did not ask for.
        full = [{"id": n} for n in range(azure_client._PAGE_SIZE)]
        invoke, queries = self._pager([full, [{"id": n} for n in range(50)]])
        rows = self._walk(invoke, limit=azure_client._PAGE_SIZE + 50)
        self.assertEqual(len(rows), azure_client._PAGE_SIZE + 50)
        self.assertEqual([q["$top"] for q in queries], [azure_client._PAGE_SIZE, 50])

    def test_a_limit_met_exactly_does_not_cost_an_extra_call(self):
        invoke, queries = self._pager([[{"id": 1}, {"id": 2}]])
        rows = self._walk(invoke, limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(queries), 1, "a full page that meets the limit must end the walk")

    def test_the_page_ceiling_bounds_a_list_that_never_reports_an_end(self):
        """One pathological project must not produce an unbounded request.

        Every page here is full, so the short-page signal never arrives; only the
        ceiling stops it. Asserted as an exact call count, because an off-by-one
        that walks forever looks identical at any smaller scale.
        """
        full = [{"id": n} for n in range(azure_client._PAGE_SIZE)]
        invoke, queries = self._pager([full] * (azure_client._MAX_PAGES + 5))
        rows = self._walk(invoke)
        self.assertEqual(len(queries), azure_client._MAX_PAGES)
        self.assertEqual(len(rows), azure_client._MAX_PAGES * azure_client._PAGE_SIZE)

    def test_the_callers_query_is_copied_not_mutated(self):
        # The caller's filter dict is often built once and reused across repos; if
        # the walk wrote $top/$skip into it, the second read would start at the
        # first one's final offset and silently skip rows.
        caller_query = {"searchCriteria.status": "active"}
        invoke, queries = self._pager([[]])
        self._walk(invoke, query=caller_query)
        self.assertEqual(caller_query, {"searchCriteria.status": "active"})
        self.assertEqual(queries[0]["searchCriteria.status"], "active")

    def test_the_route_timeout_and_api_version_are_passed_to_every_page(self):
        # The paginating reads get a much larger budget than the single-shot ones;
        # a page that fell back to the default would time out mid-walk.
        seen: list[dict] = []

        def invoke(**kwargs):
            seen.append(kwargs)
            return {"value": []}

        self._walk(invoke, route={"project": "Widgets"}, timeout=150.0, api_version="7.1-preview.1")
        self.assertEqual(seen[0]["timeout"], 150.0)
        self.assertEqual(seen[0]["api_version"], "7.1-preview.1")
        self.assertEqual(seen[0]["route"], {"project": "Widgets"})

    def test_a_bare_array_page_is_accepted_alongside_the_wrapped_shape(self):
        # Most routes answer {"count": n, "value": [...]}, a few answer a bare
        # array, and a walk that understood only one shape would report an empty
        # list for the other -- indistinguishable from "nothing to show".
        def invoke(**kwargs):
            return [{"id": 1}, {"id": 2}]

        self.assertEqual(self._walk(invoke, limit=2), [{"id": 1}, {"id": 2}])


class TestTransportFacadeBindings(unittest.TestCase):
    """The extracted helpers must still obey azure_client's patch boundary."""

    def test_invoke_injects_the_facades_current_spawn_and_body_bindings(self):
        captured: dict = {}
        spawn = mock.Mock(name="patched_az_run")
        body_file = mock.Mock(name="patched_body_file")

        def invoke(**kwargs):
            captured.update(kwargs)
            return {"ok": True}

        with mock.patch.object(azure_client._transport, "invoke", side_effect=invoke):
            with mock.patch.object(azure_client, "_az_run", spawn):
                with mock.patch.object(azure_client, "_body_file", body_file):
                    out = azure_client._az_invoke(
                        org="contoso",
                        area="git",
                        resource="repositories",
                        host=HOST,
                        api_version="7.1",
                    )

        self.assertEqual(out, {"ok": True})
        self.assertIs(captured["run"], spawn)
        self.assertIs(captured["body_file"], body_file)

    def test_pager_injects_the_facades_current_invoke_binding(self):
        captured: dict = {}
        invoke_one = mock.Mock(name="patched_az_invoke")

        def invoke_paged(**kwargs):
            captured.update(kwargs)
            return []

        with mock.patch.object(azure_client._transport, "invoke_paged", side_effect=invoke_paged):
            with mock.patch.object(azure_client, "_az_invoke", invoke_one):
                out = azure_client._az_invoke_paged(
                    org="contoso",
                    area="git",
                    resource="repositories",
                    host=HOST,
                    timeout=3.0,
                    api_version="7.1",
                )

        self.assertEqual(out, [])
        self.assertIs(captured["invoke_one"], invoke_one)
        self.assertIs(captured["values"], azure_client._values)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
