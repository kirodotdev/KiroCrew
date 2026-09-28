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

import contextlib
import json
import os
import stat
import string
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import pytest

from kiro_crew.apps.builtins.issue_radar.backend import azure_client, azure_transport
from kiro_crew.apps.builtins.issue_radar.backend.errors import (
    ProviderCliError,
    ProviderInvalidInputError,
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
    # Windows resolves a bare name against PATHEXT, so the fixture carries the
    # suffix the Azure CLI's own launcher uses; POSIX resolves the bare name and
    # takes the execute bit below.
    suffix = ".cmd" if sys.platform == "win32" else ""
    path = os.path.join(directory, f"{name}{suffix}")
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


class TestAzBinResolution(unittest.TestCase):
    """``_az_bin`` decides WHICH binary runs with the user's Azure session.

    Runs on every platform, because the resolution does: both helpers it calls
    are platform-aware (``shutil.which`` applies ``PATHEXT``, the validator reads
    an ACL instead of ``st_uid``), so there is no platform short-circuit above
    them to except this suite from.

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

            def _candidates() -> set[str]:
                # Normalized case: Windows appends the extension as PATHEXT
                # spells it (commonly upper), so the resolved path differs from
                # the fixture's on-disk spelling without being a different file.
                return {
                    os.path.normcase(path)
                    for path in source_providers.provider_executable_candidates("az")
                }

            with mock.patch.dict(os.environ, {"PATH": tmp}, clear=False):
                os.environ.pop(STRICT_PROVIDER_BIN_ENV, None)
                self.assertIn(os.path.normcase(on_path), _candidates())
                os.environ[STRICT_PROVIDER_BIN_ENV] = "1"
                self.assertNotIn(os.path.normcase(on_path), _candidates())

    def test_the_resolver_is_consulted_on_every_platform(self):
        """No platform refuses ahead of resolution.

        The two helpers ``_az_bin`` calls are the SAME ones the Sidebar PR panel
        and ``_glab_bin`` use, and both are implemented for Windows as well as
        POSIX, so a platform test here would refuse a host whose ``az`` those
        helpers accept -- and would tell a Windows user their trusted install is
        unusable while ``gh`` and ``glab`` run from the same policy.
        """
        for platform in ("win32", "darwin", "linux"):
            with self.subTest(platform=platform):
                azure_client._az_bin_cache = None
                candidates = mock.Mock(return_value=("C:\\tools\\az\\az.cmd",))
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("KIROCREW_ISSUE_RADAR_AZ", None)
                    with mock.patch.object(os.path, "isfile", return_value=True):
                        with mock.patch.multiple(
                            source_providers,
                            provider_executable_candidates=candidates,
                            _validate_provider_executable=mock.Mock(side_effect=lambda p: p),
                        ):
                            self.assertEqual(azure_client._az_bin(), "C:\\tools\\az\\az.cmd")
                candidates.assert_called_once_with("az")


class TestReparsedLauncherArguments(unittest.TestCase):
    """A ``.cmd``/``.bat`` az is executed BY the Windows command processor.

    ``CreateProcess`` hands such a launcher to ``%COMSPEC%``, which re-parses the
    command line, so the argv list by itself stops guaranteeing that one argument
    stays one argument. Every value this module puts in an argv is shape-checked
    upstream and every request body travels in a file, so this suite covers the
    backstop that keeps the guarantee true for a value that reaches argv without
    passing one of those checks.
    """

    def setUp(self):
        azure_client._az_bin_cache = None

    def tearDown(self):
        azure_client._az_bin_cache = None

    def test_a_metacharacter_argument_is_refused_before_any_spawn(self):
        launcher = os.path.join("C:", os.sep, "tools", "azure-cli", "az.cmd")
        for payload in ('a"b', "a&b", "a|b", "a>b", "a<b", "a^b", "a\rb", "a\nb"):
            with self.subTest(payload=payload):
                run = mock.Mock()
                with mock.patch.object(azure_client, "_az_bin", return_value=launcher):
                    with mock.patch.object(azure_client.subprocess, "run", run):
                        with self.assertRaises(ProviderInvalidInputError):
                            azure_client._az_run(
                                ["az", "devops", "invoke", payload], host=HOST, timeout=1.0
                            )
                # Refused BEFORE the spawn, not detected after it: the point is
                # that the command processor never sees the argument.
                run.assert_not_called()

    def test_a_percent_encoded_argument_still_reaches_a_cmd_launcher(self):
        # _SEGMENT_RE admits a SPACE, so an organization whose name carries one is
        # legitimate, and _org_url percent-encodes it to %20 on every call. Refusing
        # every percent would break a supported name rather than an attack, so the rule
        # is the encoding contract: every percent must open two hex digits.
        launcher = os.path.join("C:", os.sep, "tools", "azure-cli", "az.cmd")
        run = mock.Mock(return_value=_proc())
        with mock.patch.object(azure_client, "_az_bin", return_value=launcher):
            with mock.patch.object(azure_client, "_audit"):
                with mock.patch.object(azure_client.subprocess, "run", run):
                    azure_client._az_run(
                        ["az", "devops", "invoke", "--org", "https://dev.azure.com/My%20Org"],
                        host=HOST,
                        timeout=1.0,
                    )
        run.assert_called_once()

    def test_a_variable_reference_is_refused_under_a_cmd_launcher(self):
        # The other half of the same rule, and the reason presence of a percent cannot
        # decide it: %PATH% is EXPANDED by the command processor -- measured putting the
        # value of PATH into a child's argv -- and `PA` is not two hex digits, so the
        # encoding contract separates it from %20 without a list of variable names.
        launcher = os.path.join("C:", os.sep, "tools", "azure-cli", "az.cmd")
        run = mock.Mock(return_value=_proc())
        with mock.patch.object(azure_client, "_az_bin", return_value=launcher):
            with mock.patch.object(azure_client, "_audit"):
                with mock.patch.object(azure_client.subprocess, "run", run):
                    with self.assertRaises(azure_client.ProviderInvalidInputError):
                        azure_client._az_run(
                            ["az", "devops", "invoke", "--org", "https://dev.azure.com/%PATH%"],
                            host=HOST,
                            timeout=1.0,
                        )
        run.assert_not_called()

    def test_the_percent_rule_admits_encoding_and_refuses_everything_else(self):
        """Table over the rule itself, so the boundary is pinned rather than implied."""
        for value in ("My%20Org", "a%2Fb%2Fc", "plain", "50%25done"):
            with self.subTest(accepted=value):
                self.assertIsNone(azure_client._reject_percent_that_is_not_encoding(value))
        for value in ("%PATH%", "a%PATH%b", "trailing%", "%2", "%GG", "%%PATH%%"):
            with self.subTest(refused=value):
                self.assertIsNotNone(azure_client._reject_percent_that_is_not_encoding(value))

    def test_a_percent_encoded_argument_still_reaches_a_real_exe(self):
        # An .exe is executed by the kernel, so nothing re-parses its arguments and
        # neither half of the percent rule applies.
        direct = os.path.join(os.sep, "opt", "azure-cli", "bin", "az")
        run = mock.Mock(return_value=_proc())
        with mock.patch.object(azure_client, "_az_bin", return_value=direct):
            with mock.patch.object(azure_client, "_audit"):
                with mock.patch.object(azure_client.subprocess, "run", run):
                    azure_client._az_run(
                        ["az", "devops", "invoke", "--org", "https://dev.azure.com/%PATH%"],
                        host=HOST,
                        timeout=1.0,
                    )
        run.assert_called_once()

    def test_a_binary_the_kernel_runs_directly_is_not_subject_to_the_check(self):
        # No launcher in the chain means no second parser: the argv list reaches
        # the kernel as written, which is why this module has no shell surface.
        direct = os.path.join(os.sep, "opt", "azure-cli", "bin", "az")
        run = mock.Mock(return_value=_proc())
        with mock.patch.object(azure_client, "_az_bin", return_value=direct):
            with mock.patch.object(azure_client, "_audit"):
                with mock.patch.object(azure_client.subprocess, "run", run):
                    azure_client._az_run(["az", "devops", "invoke", "a&b"], host=HOST, timeout=1.0)
        run.assert_called_once()
        self.assertIn("a&b", run.call_args.args[0])


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


class ReparsedLauncherGuard(unittest.TestCase):
    """The guard's own behaviour, independent of any platform.

    What a real command processor DOES to each of these characters is measured in
    ``test/test_azure_launcher_reparse.py``, which lives outside this tree because it
    spawns a launcher and every spawn under ``src/kiro_crew`` must be routed or
    allowlisted. These assertions need no spawn: the guard keys off the resolved
    binary's suffix, so it decides identically on every host.
    """

    def test_every_listed_metacharacter_is_refused_under_a_cmd_launcher(self):
        """The refusal covers each listed character, on every platform.

        Complements the measurement above: that one proves WHY the characters are on
        the list, this one proves the guard actually rejects each of them, and it runs
        everywhere because the guard keys off the launcher's suffix, not the host.
        """
        for char in azure_client._MEASURED_HOSTILE_CHARACTERS:
            with self.subTest(char=char):
                with self.assertRaises(azure_client.ProviderInvalidInputError):
                    azure_client._reject_reparsed_launcher_args(
                        r"C:\Program Files\Azure CLI\az.cmd", [f"contoso{char}x"]
                    )

    def test_a_real_exe_launcher_does_not_refuse_the_same_argument(self):
        """An ``.exe`` is executed by the kernel, so nothing re-parses its arguments."""
        azure_client._reject_reparsed_launcher_args("/usr/bin/az", ["contoso&whoami"])
        azure_client._reject_reparsed_launcher_args(r"C:\tools\az.exe", ["contoso%PATH%"])

    def test_bang_stays_refused_even_though_a_stock_host_measures_it_inert(self):
        """``!`` must not be dropped by a measurement on a default host.

        A stock launcher receives ``a!PATH!b`` intact, which reads as evidence that
        ``!`` is harmless -- and that reading is the trap. Delayed expansion is a HOST
        setting (``Software\\Microsoft\\Command Processor\\DelayedExpansion``), not a
        property of the launcher: re-measured with it enabled, the same argument arrives
        as the expanded ``PATH`` split across dozens of argv elements.

        Pins the DECISION rather than the observation, because the observation flips
        with a registry value no test can rely on.
        """
        with self.assertRaises(azure_client.ProviderInvalidInputError):
            azure_client._reject_reparsed_launcher_args(
                r"C:\Program Files\Azure CLI\az.cmd", ["contoso!PATH!x"]
            )

    def test_the_allowlist_still_covers_every_shape_check(self):
        """The allowlist is DERIVED from the upstream shapes, so re-derive it here.

        Widening a shape check upstream must fail here rather than silently opening a
        hole in the launcher backstop. For every printable character, if any value
        containing it can satisfy a shape check, that character has to be allowed.
        """
        shapes = {
            name: getattr(azure_client, name)
            for name in ("_SEGMENT_RE", "_LOGIN_RE", "_SHA_RE", "_GUID_RE")
        }
        printable = [c for c in string.printable if c not in "\r\n\t\x0b\x0c"]
        for name, shape in shapes.items():
            for char in printable:
                reachable = any(
                    shape.match(candidate)
                    for candidate in (f"a{char}b", f"{char}ab", f"ab{char}", char * 8)
                )
                if reachable:
                    with self.subTest(shape=name, char=char):
                        self.assertIn(char, azure_client._ARGUMENT_ALLOWED_CHARACTERS)

        # A GUID is 8-4-4-4-12 hex, so no candidate above can match it; assert its
        # charset directly rather than letting the loop silently prove nothing.
        self.assertTrue(shapes["_GUID_RE"].match("0123abcd-4567-89ef-0123-456789abcdef"))
        for char in "0123456789abcdefABCDEF-":
            with self.subTest(char=char):
                self.assertIn(char, azure_client._ARGUMENT_ALLOWED_CHARACTERS)

    def test_the_allowlist_covers_what_the_url_builder_emits(self):
        """Derive from the real argv BUILDER, not only from the shape checks.

        Deriving the allowlist from the four shapes alone was wrong once: an ``--org``
        argument is a full URL, so ``:`` and ``/`` reach argv and a shapes-only allowlist
        refused every real call. This asserts against what :func:`_org_url` actually
        emits for the widest legitimate organization name.
        """
        widest = "My Org.Name_1-v2"
        self.assertIsNotNone(azure_client._SEGMENT_RE.match(widest))
        url = azure_transport._org_url(widest)
        for char in url:
            with self.subTest(char=char, url=url):
                self.assertIn(char, azure_client._ARGUMENT_ALLOWED_CHARACTERS)
        azure_client._reject_reparsed_launcher_args(r"C:\Program Files\Azure CLI\az.cmd", [url])

    def test_user_data_cannot_introduce_a_raw_slash_or_colon(self):
        """Why admitting ``:`` and ``/`` does not widen what an attacker controls.

        Both come from the CONSTANT ``https://dev.azure.com/`` prefix. Anything the user
        supplies goes through ``quote(..., safe="")``, which encodes a slash and a colon
        rather than passing them through -- so the two characters the allowlist admits
        for the URL's sake are not reachable from the part of it a caller influences.
        """
        for hostile in ("a/b", "a:b", "../../etc", "a/b:c"):
            with self.subTest(hostile=hostile):
                encoded = azure_transport._org_url(hostile)
                tail = encoded.split("dev.azure.com/", 1)[1]
                self.assertNotIn("/", tail)
                self.assertNotIn(":", tail)

    def test_every_measured_hostile_character_is_outside_the_allowlist(self):
        """The measurements act as a regression check on the allowlist.

        Each character in ``_MEASURED_HOSTILE_CHARACTERS`` was observed doing damage to
        a real ``.cmd`` launcher's command line. None of them may be reachable through
        the allowlist, and none of them needs to be named by the enforcement path.
        """
        for char in azure_client._MEASURED_HOSTILE_CHARACTERS:
            with self.subTest(char=char):
                self.assertNotIn(char, azure_client._ARGUMENT_ALLOWED_CHARACTERS)

    def test_the_allowlist_covers_a_real_composed_argv(self):
        """Derive from the PRODUCER, not from a proxy for it.

        The allowlist governs whole argv elements, so it has to be checked against argv
        elements the transport really composes -- ``f"{key}={value}"`` route/query pairs,
        OData keys like ``$top``, and the Windows temp path for ``--in-file``. Deriving
        it from the value shape checks alone refused every real call, because those
        shapes describe what goes INTO an element, not the element.
        """
        captured: list[list[str]] = []

        def _capture(argv, *, host, timeout):
            captured.append(list(argv))
            return _proc()

        with mock.patch.object(azure_client, "_az_run", _capture):
            with contextlib.suppress(Exception):
                azure_client._az_invoke(
                    org="My Org",
                    area="git",
                    resource="repositories",
                    method="GET",
                    api_version="7.1",
                    route={"project": "My Project"},
                    query={"$top": "30", "searchCriteria.status": "active"},
                    body={"hello": "world"},
                    media_type="application/json",
                    host=HOST,
                )

        self.assertTrue(captured, "the transport never reached the spawn seam")
        for element in captured[0][1:]:
            if element in azure_transport._GENERATED_BODY_PATHS:
                continue  # host-owned spelling; covered by the body-path test below
            for char in element:
                with self.subTest(element=element, char=char):
                    self.assertIn(char, azure_client._ARGUMENT_ALLOWED_CHARACTERS)

    def test_a_generated_body_path_is_not_held_to_the_allowlist(self):
        """A temp path's spelling is the HOST's, so the allowlist cannot describe it.

        Measured the hard way: this branch passed on a developer host whose temp path is
        ``C:\\Users\\bolic\\...`` and failed on the CI Windows runner, whose path carries
        an 8.3 short name -- ``C:\\Users\\RUNNER~1\\...`` -- contributing a ``~`` no shape
        check or argv builder could have predicted. A ``TMPDIR`` under
        ``Program Files (x86)`` would contribute parentheses next. So a path this module
        generated is checked for characters that genuinely break a command line, and only
        an UNREGISTERED value is held to the allowlist.
        """
        registry = azure_transport._GENERATED_BODY_PATHS
        launcher = r"C:\Program Files\Azure CLI\az.cmd"
        short_name_path = r"C:\Users\RUNNER~1\AppData\Local\Temp\kirocrew-az-x.json"
        parens_path = r"C:\Program Files (x86)\tmp\kirocrew-az-y.json"

        for path in (short_name_path, parens_path):
            with self.subTest(path=path):
                # Unregistered, it is refused -- so forgetting to register is strict.
                registry.discard(path)
                with self.assertRaises(azure_client.ProviderInvalidInputError):
                    azure_client._reject_reparsed_launcher_args(launcher, [path])
                # Registered, it passes.
                registry.add(path)
                try:
                    azure_client._reject_reparsed_launcher_args(launcher, [path])
                finally:
                    registry.discard(path)

        # A registered path is still refused when it carries a real metacharacter.
        hostile = r"C:\tmp\a&whoami\kirocrew-az-z.json"
        registry.add(hostile)
        try:
            with self.assertRaises(azure_client.ProviderInvalidInputError):
                azure_client._reject_reparsed_launcher_args(launcher, [hostile])
        finally:
            registry.discard(hostile)

    def test_a_real_composed_argv_reaches_the_spawn_under_a_cmd_launcher(self):
        """End to end: a normal invoke must SPAWN on a Windows ``az.cmd`` install.

        The guard sits at the spawn chokepoint, so a refusal here is the provider not
        working at all on the platform this PR enables -- per request, with a message
        blaming the caller's input. This drives route parameters, a query parameter and a
        body file through ``_az_run`` with ``_az_bin`` resolving a ``.cmd`` launcher, and
        asserts ``subprocess.run`` is actually called.
        """
        launcher = os.path.join("C:", os.sep, "Program Files", "Azure CLI", "az.cmd")
        run = mock.Mock(return_value=_proc())
        with mock.patch.object(azure_client, "_az_bin", return_value=launcher):
            with mock.patch.object(azure_client, "_audit"):
                with mock.patch.object(azure_client.subprocess, "run", run):
                    with contextlib.suppress(Exception):
                        azure_client._az_invoke(
                            org="My Org",
                            area="git",
                            resource="repositories",
                            method="GET",
                            api_version="7.1",
                            route={"project": "My Project"},
                            query={"$top": "30"},
                            body={"hello": "world"},
                            media_type="application/json",
                            host=HOST,
                        )
        run.assert_called_once()
        spawned = run.call_args[0][0]
        self.assertIn("--route-parameters", spawned)
        self.assertIn("project=My Project", spawned)
        self.assertIn("$top=30", spawned)

    def test_no_shape_check_admits_a_character_the_composition_contributes(self):
        """Why admitting ``: / = $ \\ %`` does not widen what an attacker controls.

        Each of the six enters only from a literal this module writes -- the constant
        URL prefix, the ``key=value`` join, an OData key, a temp path, or ``quote``'s
        octets. If a shape check ever started admitting one, caller data could carry it
        into an argv element, and this assertion is what fails first.
        """
        for name in ("_SEGMENT_RE", "_LOGIN_RE", "_SHA_RE", "_GUID_RE"):
            shape = getattr(azure_client, name)
            for char in ":/=$\\%":
                for candidate in (f"a{char}b", f"{char}ab", f"ab{char}", char * 8):
                    with self.subTest(shape=name, candidate=candidate):
                        self.assertIsNone(shape.match(candidate))

    def test_no_az_child_env_key_can_form_an_octet_spanning_reference(self):
        """The invariant that makes a two-space organization name safe to pass.

        ``quote`` emits one ``%20`` per space, so an organization named ``A B C`` reaches
        argv as ``A%20B%20C``. Both octets satisfy
        :func:`_reject_percent_that_is_not_encoding` individually, and a command processor
        reading the line left to right can still see ``%20B%`` SPANNING them as a
        reference to a variable named ``20B``. Measured against a real ``.cmd`` launcher:
        with ``20B`` undefined the argument arrives intact, and with ``20B`` defined it
        arrives as ``A<value>20C``. The mechanism is real.

        What closes it is the child environment, not the argument: ``_az_env`` builds the
        env from a fixed literal allowlist rather than inheriting the parent's, so a
        variable of that shape is never defined in the process that re-parses the line.
        That was an incidental property of an unrelated function until this test, which is
        the problem -- widening the env allowlist would have opened a command-injection
        path here with nothing failing.

        Refusing the argument instead is NOT the fix: rejecting more than one ``%`` would
        refuse ``A%20B%20C``, and a two-space organization name is legitimate under
        ``_SEGMENT_RE``.
        """
        env = azure_client._az_env("dev.azure.com")
        for key in env:
            with self.subTest(key=key):
                self.assertRegex(
                    key,
                    r"^[A-Za-z_]",
                    "an env key starting with a digit could be the target of a "
                    "`%<digits><letters>%` reference spanning two encoded octets",
                )

        # The parent's environment must not be able to inject one either.
        with mock.patch.dict(os.environ, {"20B": "INJECTED", "20": "X"}, clear=False):
            leaked = azure_client._az_env("dev.azure.com")
            self.assertNotIn("20B", leaked)
            self.assertNotIn("20", leaked)

    def test_a_two_space_organization_name_still_reaches_the_launcher(self):
        """The legitimate value the tempting fix would have broken."""
        url = azure_transport._org_url("A B C")
        self.assertEqual(url, "https://dev.azure.com/A%20B%20C")
        azure_client._reject_reparsed_launcher_args(r"C:\Program Files\Azure CLI\az.cmd", [url])

    def test_cmd_delimiters_no_measurement_named_are_refused_anyway(self):
        """What deriving the check from the producer buys.

        ``;``, ``,``, tab, ``(`` and ``)`` are all significant to cmd.exe and appear in
        no argv element this provider composes, so the allowlist refuses them without
        anyone having measured them -- the property an enumeration of hostile characters
        cannot have, since it admits every spelling not yet enumerated.

        ``=`` and ``$`` are deliberately NOT in this list: the transport writes both
        itself (``key=value`` pairs, OData keys like ``$top``), so refusing them refuses
        the provider's own traffic.
        """
        for char in (";", ",", "\t", "(", ")", "`", "~", "*", "?", "{", "}", "["):
            with self.subTest(char=char):
                with self.assertRaises(azure_client.ProviderInvalidInputError):
                    azure_client._reject_reparsed_launcher_args(
                        r"C:\Program Files\Azure CLI\az.cmd", [f"contoso{char}x"]
                    )

    def test_a_legitimate_value_still_reaches_a_cmd_launcher(self):
        """The allowlist must not refuse what the provider legitimately builds."""
        for value in (
            "contoso corp",  # _SEGMENT_RE admits a space
            "My.Project_1-v2",
            "user.name+tag@example.com",
            "o'brien",  # _LOGIN_RE admits an apostrophe
            "0123abcd-4567-89ef-0123-456789abcdef",
            "a1b2c3d4e5f6",
            "contoso%20corp",  # the encoded form of the space
        ):
            with self.subTest(value=value):
                azure_client._reject_reparsed_launcher_args(
                    r"C:\Program Files\Azure CLI\az.cmd", [value]
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
