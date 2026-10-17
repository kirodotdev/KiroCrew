"""Review-workflow tests for what a review lane's model, and the runner it runs on, can
reach: credential values, the model shell and sandbox, the environment, and the
network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from ai_review_workflows_helpers import (
    FORK_REVIEW_LANES,
    WORKFLOWS,
    _bash,
    _flat,
    _step,
    _step_env,
    _step_script,
    _workflow,
)


def _lane_jobs(lane: str) -> dict:
    """The `jobs:` mapping of one workflow file under `.github/workflows`."""
    return yaml.safe_load((WORKFLOWS / lane).read_text(encoding="utf-8"))["jobs"]


def _blocking_endpoints(job: dict) -> "list[str] | None":
    """The job's blocking-egress endpoint list, or None when it does not block."""
    for step in job.get("steps") or ():
        if "step-security/harden-runner" not in str(step.get("uses") or ""):
            continue
        settings = step.get("with") or {}
        if settings.get("egress-policy") != "block":
            return None
        return str(settings.get("allowed-endpoints") or "").split()
    return None


class TestForkLaneBunEgress:
    """The fork reviewers install bun from a GitHub *release asset*.

    `anthropics/claude-code-action` runs `oven-sh/setup-bun`, which downloads
    `https://github.com/oven-sh/bun/releases/download/...`. GitHub answers that
    with a 302 to `release-assets.githubusercontent.com` -- a different host
    from the `objects.githubusercontent.com` these allowlists already carry. A
    lane that blocks egress without it does not fail at the model call: bun
    never lands, the action's own script dies `bun: command not found`
    (exit 127), the lane posts `review incomplete`, and because `PR Readiness`
    aggregates these lanes, every fork PR goes red at once.

    `workflow_run` lanes always execute the DEFAULT branch's copy of the yaml,
    so a PR editing these files cannot exercise its own change. This test is
    the only pre-merge guard the coupling has.
    """

    ENDPOINT = "release-assets.githubusercontent.com:443"
    ACTION = "anthropics/claude-code-action"

    @classmethod
    def _runs_a_model(cls, job: dict) -> bool:
        return any(cls.ACTION in str(step.get("uses") or "") for step in job.get("steps") or ())

    @pytest.mark.parametrize("lane", FORK_REVIEW_LANES)
    def test_every_model_job_allows_the_bun_release_asset_host(self, lane: str) -> None:
        checked = 0
        for name, job in _lane_jobs(lane).items():
            if not self._runs_a_model(job):
                continue
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            checked += 1
            assert self.ENDPOINT in endpoints, (
                f"{lane} job {name!r} runs {self.ACTION} behind a blocking egress "
                f"policy but does not allow {self.ENDPOINT}, so setup-bun's download "
                "is refused and the step exits 127 instead of reviewing anything"
            )
        assert checked, f"{lane} has no blocking-egress {self.ACTION} job to check"

    @pytest.mark.parametrize("lane", FORK_REVIEW_LANES)
    def test_jobs_that_run_no_model_keep_the_narrower_allowlist(self, lane: str) -> None:
        # Least privilege: only the job that actually downloads bun gets the
        # host. `fork-security-scope-review.yml` blocks egress in four jobs and
        # runs the model in exactly one, so a blanket per-file edit would widen
        # three allowlists that fetch nothing but Actions artifacts.
        for name, job in _lane_jobs(lane).items():
            if self._runs_a_model(job):
                continue
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            assert self.ENDPOINT not in endpoints, (
                f"{lane} job {name!r} runs no model and downloads no bun, so "
                f"allowing {self.ENDPOINT} widens its egress for nothing"
            )


class TestForkLaneBubblewrapBootstrapEgress:
    """The fork reviewers fetch the whole toolchain their own settings turn on.

    Setting `allowed_non_write_users` auto-enables `claude-code-action`'s
    subprocess secret-scrub plus bubblewrap isolation, and the action bootstraps
    in two sequential network phases. First `apt-get install bubblewrap socat`:
    on the ubuntu-latest image `/etc/apt/apt-mirrors.txt` names the azure mirror
    first over plaintext http and falls back to the two canonical hosts over
    https, so all three are on the path of that one install. Then the CLI itself,
    via `curl https://claude.ai/install.sh`, whose script reads its version
    manifest and binary from `downloads.claude.ai/claude-code-releases`.

    Both phases are asserted together because they are SEQUENTIAL: an allowlist
    carrying only the apt half lets apt succeed and then dies on curl, with the
    same `review incomplete` and no model call, so a green apt phase is not
    evidence that the bootstrap resolves.

    Blocked, the install exits 7 before the model is ever reached, and the lane
    reports `review incomplete` rather than a verdict. Failing closed is
    correct -- the isolation is a security control, so running unsandboxed must
    never be a silent fallback -- but the lane then cannot review at all, and the
    advisory lanes publish that as a NEUTRAL check, so three reviewers stopped
    reviewing every fork PR without turning anything red.

    This is the whole-file guard: `workflow_run` lanes always execute the DEFAULT
    branch's yaml, so a PR editing these files cannot exercise its own change.
    The endpoints are asserted on the model job only, for the same least-privilege
    reason as the bun release-asset host above.
    """

    ENDPOINTS = (
        "azure.archive.ubuntu.com:80",
        "archive.ubuntu.com:443",
        "security.ubuntu.com:443",
        "claude.ai:443",
        "downloads.claude.ai:443",
    )
    ACTION = "anthropics/claude-code-action"

    @classmethod
    def _runs_a_model(cls, job: dict) -> bool:
        return any(cls.ACTION in str(step.get("uses") or "") for step in job.get("steps") or ())

    @pytest.mark.parametrize("lane", FORK_REVIEW_LANES)
    def test_every_model_job_allows_the_bubblewrap_bootstrap_hosts(self, lane: str) -> None:
        checked = 0
        for name, job in _lane_jobs(lane).items():
            if not self._runs_a_model(job):
                continue
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            checked += 1
            missing = [host for host in self.ENDPOINTS if host not in endpoints]
            assert not missing, (
                f"{lane} job {name!r} runs {self.ACTION} behind a blocking egress "
                f"policy but does not allow {missing}, so a phase of its bootstrap "
                "is refused (apt for the bubblewrap sandbox, curl for the CLI "
                "itself), the action exits 7 before any model call, and the lane "
                "publishes `review incomplete` instead of a verdict"
            )
        assert checked, f"{lane} has no blocking-egress {self.ACTION} job to check"

    @pytest.mark.parametrize("lane", FORK_REVIEW_LANES)
    def test_jobs_that_run_no_model_keep_the_narrower_allowlist(self, lane: str) -> None:
        # Least privilege, exactly as for the bun host: only a job that actually
        # bootstraps the sandbox gets the package mirrors.
        # `fork-security-scope-review.yml` blocks egress in four jobs and runs the
        # model in one, so a blanket per-file edit would widen three allowlists
        # that install nothing.
        for name, job in _lane_jobs(lane).items():
            if self._runs_a_model(job):
                continue
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            present = [host for host in self.ENDPOINTS if host in endpoints]
            assert not present, (
                f"{lane} job {name!r} runs no model and installs no sandbox, so "
                f"allowing {present} widens its egress for nothing"
            )

    @pytest.mark.parametrize("lane", FORK_REVIEW_LANES)
    def test_no_lane_allows_a_host_nothing_here_fetches(self, lane: str) -> None:
        # Two ways a reader widens this allowlist from something that merely
        # APPEARED in the output. The runner image preinstalls google-chrome and
        # microsoft apt sources, so a blocked `apt-get update` names them in the
        # same wall of text as the ubuntu archive -- but their failures are apt
        # WARNINGS (`W:`) and nothing here installs from them. And the CLI
        # installer script prints `code.claude.com` and `www.anthropic.com` inside
        # its own error messages without ever requesting them, so grepping that
        # script for hostnames yields two more that belong nowhere near a
        # blast-radius control.
        forbidden = (
            "dl.google.com",
            "packages.microsoft.com",
            "code.claude.com",
            "www.anthropic.com",
        )
        for name, job in _lane_jobs(lane).items():
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            for host in forbidden:
                assert not any(entry.startswith(host) for entry in endpoints), (
                    f"{lane} job {name!r} allows {host}, which this lane never "
                    "requests; it only ever appeared in a warning or an error string"
                )


class TestForkGptLaneMantleEgress:
    """The GPT passes reach Bedrock on the mantle host, not the runtime host.

    The two review passes run a CLI configured to talk to a loopback signing
    proxy; that proxy SigV4-signs each request and forwards it to
    `https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses` -- the URL a
    real job log shows the lane calling. The classic
    `bedrock-runtime.*.amazonaws.com` hosts in the allowlist do not cover it, so
    under blocking egress the proxy cannot connect, each pass retries five times,
    ends `Connection failed: error sending request`, and the lane fails closed
    with `review incomplete`.

    Only the job that runs that proxy gets the host: the Opus, Design, UX,
    First-Principles and Security-Scope lanes talk to Bedrock through the runtime
    host and must not carry it.
    """

    ENDPOINT = "bedrock-mantle.us-east-1.api.aws:443"
    # The lane that reaches the mantle host does so either by configuring the
    # built-in amazon-bedrock provider or by running the loopback signing proxy
    # that forwards to mantle (the UID-boundary design).
    PROVIDER = 'model_provider = "amazon-bedrock"'
    PROXY_MARKER = "bedrock-signing-proxy.py"

    @classmethod
    def _configures_the_mantle_provider(cls, job: dict) -> bool:
        for step in job.get("steps") or ():
            run = str(step.get("run") or "")
            if cls.PROVIDER in run or cls.PROXY_MARKER in run:
                return True
        return False

    def test_the_gpt_lane_model_job_allows_the_mantle_endpoint(self) -> None:
        checked = 0
        for name, job in _lane_jobs("fork-gpt-review.yml").items():
            if not self._configures_the_mantle_provider(job):
                continue
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            checked += 1
            assert self.ENDPOINT in endpoints, (
                f"fork-gpt-review.yml job {name!r} reaches "
                f"Bedrock's mantle endpoint behind a blocking egress policy but "
                f"does not allow {self.ENDPOINT}, so the signing proxy fails to "
                "connect and the lane posts `review incomplete`"
            )
        assert checked, "fork-gpt-review.yml has no blocking-egress mantle job to check"

    @pytest.mark.parametrize("lane", FORK_REVIEW_LANES)
    def test_lanes_that_use_no_mantle_provider_keep_the_narrower_allowlist(self, lane: str) -> None:
        for name, job in _lane_jobs(lane).items():
            if self._configures_the_mantle_provider(job):
                continue
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            assert self.ENDPOINT not in endpoints, (
                f"{lane} job {name!r} runs no mantle-backed model, so allowing "
                f"{self.ENDPOINT} widens its egress for nothing"
            )


class TestForkLaneRedactsCredentialValues:
    """A BARE credential value must not survive into the posted body.

    Every shape rule in these lanes misses a bare AWS secret access key: it is 40
    characters of ``[A-Za-z0-9/+=]`` with no distinctive prefix, so it is not
    ``AKIA``/``ASIA`` (that is the key *ID*), it carries no ``name=`` for the
    named-pair rule to anchor on, and it is far short of the 200+ char base64 run
    the withhold filter looks for. The reviewer runs agentically with those values
    in its environment and a malicious fork can prompt it to print them, so the
    lanes redact the VALUES too -- which cannot be evaded by how the model chooses
    to format them.
    """

    # Shape-accurate stand-in: 40 chars from the AWS secret alphabet, no prefix.
    FAKE_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

    LANES = (
        ("fork-gpt-review.yml", "Redact credential shapes", "codex-review-output.md"),
        ("fork-opus-review.yml", "Capture and redact review output", "claude-review-output.md"),
    )

    def _run_redaction(
        self, tmp_path: Path, workflow: str, step: str, out: str, body: str, cred: str
    ):
        bash = _bash()
        if bash is None or shutil.which("perl") is None or shutil.which("jq") is None:
            pytest.skip("redaction test requires Bash, perl and jq")
        if os.name == "nt":
            pytest.skip("the lanes' perl -i redaction is exercised on POSIX runners")

        cwd = tmp_path / "ws"
        cwd.mkdir()
        (cwd / out).write_text(body, encoding="utf-8")
        # The Opus lane folds redaction into its capture step, which reads the
        # agent transcript; give it one so the real step runs unmodified.
        exec_file = tmp_path / "exec.json"
        exec_file.write_text(json.dumps({"result": body}), encoding="utf-8")

        script_file = tmp_path / "step.sh"
        script_file.write_text(
            _step_script(_workflow(workflow), step), encoding="utf-8", newline="\n"
        )
        env = {**os.environ, "EXEC_FILE": str(exec_file)}
        # Absent means "not configured", which must be a no-op rather than an
        # empty pattern -- so the caller can ask for the unset case explicitly.
        for name in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID"):
            env.pop(name, None)
        if cred is not None:
            env["AWS_SECRET_ACCESS_KEY"] = cred
        result = subprocess.run(
            [bash, "-e", str(script_file)], check=False, capture_output=True, cwd=cwd, env=env
        )
        return (cwd / out).read_text(encoding="utf-8"), result

    @pytest.mark.parametrize(("workflow", "step", "out"), LANES)
    def test_a_bare_secret_value_is_redacted(
        self, tmp_path: Path, workflow: str, step: str, out: str
    ) -> None:
        body = f"the agent printed its environment: {self.FAKE_SECRET} and kept going\n"
        text, result = self._run_redaction(tmp_path, workflow, step, out, body, self.FAKE_SECRET)
        assert result.returncode == 0, result.stderr.decode()
        assert self.FAKE_SECRET not in text, f"{workflow}: bare secret survived redaction"
        assert "[REDACTED-CREDENTIAL]" in text
        # Surrounding prose is untouched -- this redacts a value, not the body.
        assert "and kept going" in text

    @pytest.mark.parametrize(("workflow", "step", "out"), LANES)
    def test_an_unset_credential_does_not_carpet_the_body(
        self, tmp_path: Path, workflow: str, step: str, out: str
    ) -> None:
        """The guard that makes the loop safe.

        ``\\Q\\E`` on an empty string is an empty pattern, which matches at every
        position -- so an unconfigured credential would otherwise replace the gaps
        between every character with the marker and destroy the review body.
        """
        body = "a perfectly ordinary review body with no secrets in it\n"
        text, result = self._run_redaction(tmp_path, workflow, step, out, body, None)
        assert result.returncode == 0, result.stderr.decode()
        assert "[REDACTED-CREDENTIAL]" not in text
        assert text.strip() == body.strip()


def _fork_gpt_cli_config(tmp_path: Path) -> dict:
    """Run the fork GPT lane's config step and parse the file it writes.

    PARSE, never grep: a heredoc emitting invalid TOML would still satisfy a
    substring assertion while codex discards the whole file -- taking the shell
    environment policy and the sandbox mode with it.
    """
    import tomllib

    bash = _bash()
    if bash is None:
        pytest.skip("writing the review CLI config requires Bash")
    home = tmp_path / "home"
    home.mkdir()
    script_file = tmp_path / "step.sh"
    script_file.write_text(
        _step_script(
            _workflow("fork-gpt-review.yml"), "Configure the review CLI for Amazon Bedrock"
        ),
        encoding="utf-8",
        newline="\n",
    )
    proc = subprocess.run(
        [bash, "-e", str(script_file)],
        check=False,
        capture_output=True,
        encoding="utf-8",
        cwd=tmp_path,
        env={**os.environ, "HOME": str(home)},
    )
    assert proc.returncode == 0, proc.stderr
    written = home / ".codex" / "config.toml"
    assert written.is_file(), "the step wrote no config.toml"
    return tomllib.loads(written.read_text(encoding="utf-8"))


class TestForkGptLaneKeepsCredentialsOutOfTheModelShell:
    """`codex exec` hands the model a shell; the Opus lane deliberately does not.

    The Opus fork lane runs with ``--allowedTools "Read,Grep,Glob"``, so its model
    has no way to read the environment. This lane's model does, and it runs on a
    fork's UNTRUSTED diff, so a prompt-injected reviewer can be told to print its
    environment. Redaction cannot close that by itself: it matches the
    credential's shapes and its verbatim value, and a value printed in chunks,
    with delimiters, or re-encoded defeats both. The credentials therefore must
    not be in the shell's environment at all.
    """

    STEP = "Configure the review CLI for Amazon Bedrock"

    def _config(self, tmp_path: Path) -> dict:
        return _fork_gpt_cli_config(tmp_path)

    def test_the_bedrock_provider_still_resolves(self, tmp_path: Path) -> None:
        # The credentials live outside the codex PROCESS environment. codex is
        # pointed at a loopback signing proxy that runs as a DIFFERENT UID, holds
        # the credentials, and SigV4-signs each request. So the config names a
        # CUSTOM provider on 127.0.0.1 with the responses wire API, and carries no
        # AWS credential of any kind -- there is nothing in codex's environment or
        # any file its UID can read for a prompt-injected reviewer to exfiltrate.
        config = self._config(tmp_path)
        assert config["model"] == "openai.gpt-6.1-sol"
        assert config["model_provider"] == "bedrock-signer", (
            "the fork GPT lane must route the model through the loopback signing "
            "proxy, not the amazon-bedrock provider that reads credentials from "
            "the codex process environment"
        )
        provider = config["model_providers"]["bedrock-signer"]
        assert provider["base_url"].startswith(
            "http://127.0.0.1:"
        ), f"the signer endpoint must be loopback, got {provider['base_url']!r}"
        assert provider["wire_api"] == "responses"
        # The provider carries no `env_key`: codex is given only the endpoint,
        # never a credential variable to read. The signing proxy supplies auth.
        assert "env_key" not in provider, (
            "the loopback signer provider must not name an env_key; codex holds "
            "no credential, the proxy does (#8789)"
        )

    def test_every_aws_credential_variable_is_excluded(self, tmp_path: Path) -> None:
        filters = self._config(tmp_path)["shell_environment_policy"]["filters"]
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            assert filters.get(name) == "exclude", f"{name} still reaches the model's shell"
        # The wildcard covers an AWS variable a later step introduces without
        # anyone remembering to name it here.
        assert filters.get("AWS_*") == "exclude"

    def test_secret_named_variables_are_not_kept(self, tmp_path: Path) -> None:
        # `ignore_default_excludes` reads backwards: it DEFAULTS TO TRUE, and true
        # KEEPS variables whose names contain KEY, SECRET or TOKEN. It must be
        # false so GH_TOKEN and future secret-shaped variables drop out too.
        policy = self._config(tmp_path)["shell_environment_policy"]
        assert policy["ignore_default_excludes"] is False


class TestForkGptLaneSandboxModeLivesInTheConfigFile:
    """A `--sandbox` flag on the command line makes this lane's config unreachable.

    The flag wins over `sandbox_mode` in the staged config AND suppresses a
    `default_permissions` profile outright, so a filesystem rule written into that
    file is inert for as long as the flag is passed. The mode therefore belongs in
    the file, which is the only place a read restriction can be attached to it.

    Both halves are asserted because either one alone is unsafe. Dropping the flag
    without pinning the key leaves the effective mode to a config default this
    repository does not control, and `workspace-write` and `danger-full-access`
    are legal values for it -- on a lane that reviews a fork's UNTRUSTED diff
    holding Bedrock credentials.
    """

    WORKFLOW = "fork-gpt-review.yml"

    def _codex_commands(self) -> list[str]:
        """Every `codex exec` invocation, backslash continuations joined up."""
        joined = re.sub(r"\\\n\s*", " ", _workflow(self.WORKFLOW))
        commands = [
            _flat(line).strip()
            for line in joined.splitlines()
            if ".bin/codex" in line and " exec " in _flat(line)
        ]
        assert commands, f"{self.WORKFLOW} runs no `codex exec` command to check"
        return commands

    def test_neither_pass_passes_the_sandbox_flag(self) -> None:
        for command in self._codex_commands():
            assert "--sandbox" not in command, (
                f"{self.WORKFLOW} passes --sandbox on a `codex exec` command line: "
                f"{command!r}. The flag overrides the staged config.toml and "
                "suppresses its `default_permissions` profile, so every filesystem "
                "rule in that file stops being enforced while this argument is "
                "present -- silently, because the lane still reports a read-only "
                "sandbox and still produces a verdict"
            )

    def test_the_staged_config_pins_the_mode_explicitly(self, tmp_path: Path) -> None:
        config = _fork_gpt_cli_config(tmp_path)
        assert config.get("sandbox_mode") == "read-only", (
            'the staged config.toml does not pin sandbox_mode = "read-only". With '
            "no --sandbox argument on the command line, `codex exec` resolves the "
            "mode from this file, so an absent or widened key hands the model's "
            "shell more of the runner than reading the diff needs"
        )


class TestForkGptLaneVerifiesTheReadRestrictionIsEnforced:
    """#13414: the lane must prove the enforcing sandbox actually ENFORCES a
    filesystem read restriction before the model runs, and fail closed when it
    cannot -- not merely configure read-only mode and trust it.

    read-only mode permits every read, so the sudo and credential-file probes in
    "Verify the review sandbox fails closed" never exercise a denied path. A
    separate probe applies a permission profile that denies a sentinel file and
    confirms, through the same confinement the passes use, that the sentinel read
    is refused while an ordinary read still works. A permission profile is
    honoured only on the enforcing backend; a backend that cannot apply it leaves
    no read restriction in force, so the step fails closed rather than review a
    fork diff behind a fence that is not enforced.
    """

    WORKFLOW = "fork-gpt-review.yml"
    STEP = "Verify the review sandbox fails closed (no sudo, no credential read)"

    def _step_body(self) -> str:
        doc = yaml.safe_load(_workflow(self.WORKFLOW))
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if step.get("name") == self.STEP:
                    return step.get("run") or ""
        raise AssertionError(f"{self.WORKFLOW} has no step named {self.STEP!r}")

    def test_it_applies_a_deny_permission_profile(self) -> None:
        # Without a permission profile that denies a path, the verify step never
        # exercises a denied read, so it never verifies the read restriction.
        body = self._step_body()
        assert "default_permissions" in body and re.search(r'=\s*"deny"', body), (
            "the verify step applies no permission profile that denies a path, so "
            "it never exercises -- and so never verifies -- a filesystem read "
            "restriction (#13414); read-only mode permits every read"
        )

    def test_the_profile_probe_does_not_pass_sandbox_mode(self) -> None:
        # `-c sandbox_mode=...` wins over the config file AND suppresses a
        # default_permissions profile outright, so a probe carrying it would
        # verify nothing while still reporting success.
        body = self._step_body()
        for line in body.splitlines():
            if "CODEX_HOME=" in line and "sandbox" in line:
                assert "sandbox_mode" not in line, (
                    "the permission-profile probe passes -c sandbox_mode, which "
                    "suppresses the profile and makes the read-restriction check a "
                    f"silent no-op: {line.strip()!r}"
                )

    def test_it_fails_closed_when_the_restriction_is_not_enforced(self) -> None:
        body = self._step_body()
        # A readable denied path, and a backend that cannot apply the profile at
        # all, must each end the job with a clear, actionable message.
        assert re.search(r"::error::[^\n]*read restriction[^\n]*", body), (
            "the verify step has no fail-closed error about the read restriction "
            "not being enforced"
        )
        assert re.search(
            r'\$PROFILE_SANDBOX cat "\$SENTINEL".*\n\s*echo "::error::.*\n\s*exit 1',
            body,
        ), (
            "a readable denied sentinel does not fail the job closed, so the lane "
            "could review a fork diff behind an unenforced read restriction"
        )
        assert re.search(
            r'if ! \$PROFILE_SANDBOX true;.*\n\s*echo "::error::.*\n\s*exit 1', body
        ), (
            "the step does not fail closed when the enforcing backend cannot be "
            "established with the read restriction applied"
        )


class TestForkModelStepsDenyReadingTheEnvironment:
    """Having no Bash is not the same as having no environment access.

    These lanes run their models with ``--allowedTools "Read,Grep,Glob"`` and no
    shell, which is why they need no `shell_environment_policy`. But `Read` is
    path-unscoped and ``/proc/self/environ`` is an ordinary file, so an injected
    prompt in the fork's untrusted diff could read the job's Bedrock credentials
    through `Read` alone and print them into a body this PR now publishes.
    Redaction cannot close that -- an encoded or chunked value evades both the
    shape rules and the verbatim-value match -- so the read path is denied.

    BOTH workflows this change edits are covered, not just the obvious one. The
    GPT lane's own Opus ADJUDICATION step is a `claude-code-action` step too, and
    the body it produces is posted by the SAME comment step that surfaces an
    unstamped review -- so a fence that skipped it would leave the exposure
    reachable in a file this diff already touches.
    """

    WORKFLOWS = ("fork-gpt-review.yml", "fork-opus-review.yml")
    ACTION = "anthropics/claude-code-action"

    def _model_steps(self) -> list[tuple[str, dict]]:
        import yaml

        found: list[tuple[str, dict]] = []
        for workflow in self.WORKFLOWS:
            doc = yaml.safe_load(_workflow(workflow))
            steps = [
                step
                for job in (doc.get("jobs") or {}).values()
                for step in (job.get("steps") or [])
                if self.ACTION in str(step.get("uses") or "")
            ]
            # Enumerate rather than grep the file: a NEW model step added without
            # the fence is exactly the regression this test exists to catch, and a
            # whole-file substring assertion would still pass with one unfenced
            # step sitting beside a fenced sibling.
            assert steps, f"{workflow}: found no {self.ACTION} step to check"
            found.extend((workflow, step) for step in steps)
        return found

    @staticmethod
    def _args(workflow: str, step: dict) -> tuple[str, str]:
        name = step.get("name") or step.get("id") or "<unnamed>"
        return f"{workflow}:{name}", step.get("with", {}).get("claude_args", "")

    def test_every_model_step_denies_reading_proc(self) -> None:
        for workflow, step in self._model_steps():
            where, args = self._args(workflow, step)
            assert "--disallowedTools" in args, f"{where}: no --disallowedTools fence"
            assert "Read(//proc/**)" in args, f"{where}: /proc is still readable"

    def test_the_fence_is_a_deny_not_a_narrowed_allow(self) -> None:
        # A deny rule outranks every allow rule and CLI flag, so the fence must
        # not be expressed by narrowing --allowedTools: an allow rule that fails
        # to match falls back to prompting, which in a non-interactive run is not
        # a guarantee about what was read.
        for workflow, step in self._model_steps():
            where, args = self._args(workflow, step)
            assert (
                "Read(//proc" not in args.split("--disallowedTools")[0]
            ), f"{where}: /proc appears before --disallowedTools, so it may be an allow rule"

    def test_the_lanes_still_have_the_tools_they_review_with(self) -> None:
        # The fence must not cost a lane its work: each reads the checked-out
        # tree and its pre-fetched data through exactly these three tools.
        for workflow, step in self._model_steps():
            where, args = self._args(workflow, step)
            assert '--allowedTools "Read,Grep,Glob"' in args, f"{where}: review tools changed"
            # And Bash stays absent -- the no-shell property these lanes rest on.
            assert "Bash" not in args, f"{where}: Bash reappeared in a fork model step"


class TestModelStepsRunAfterTheCredentialFilesAreScrubbed:
    """Every model step is directly preceded by a step that removes the runner's
    on-disk credential copy.

    ``configure-aws-credentials`` exports through ``core.exportVariable``, which
    writes each value into ``$RUNNER_TEMP/_runner_file_commands/set_env_*``. A
    model told to open that file can emit the value in chunks that match no shape
    rule, so the value is REMOVED rather than filtered.

    Two step shapes satisfy this, by design:

    * The Opus adjudication steps (both lanes) run a SHELL-LESS model
      (``--allowedTools "Read,Grep,Glob"``) that still holds Bedrock credentials
      in its own environment, so the "Scrub persisted credential files" step runs
      directly before each of them and truncates the ``set_env_*`` files.
    * The fork GPT lane's two ``codex exec`` passes hand the model a real SHELL,
      so they go further: a credential-boundary step (start/refresh the loopback
      signer) runs directly before each pass. It moves the credential across a
      UID boundary into the signer process, BLANKS it in ``$GITHUB_ENV`` so codex
      inherits nothing, AND truncates the same ``set_env_*`` files -- a superset
      of the scrub. codex itself holds no credential at all.
    """

    WORKFLOWS = ("fork-gpt-review.yml", "fork-opus-review.yml")
    SCRUB = "Scrub persisted credential files before the model runs"
    # Steps that establish/refresh the UID boundary before a shell-bearing codex
    # pass. Each truncates set_env_* AND removes the credential from the job env.
    BOUNDARY = (
        "Start the loopback signing proxy (holds the credentials)",
        "Refresh the signer's credentials (keep them behind the UID boundary)",
    )
    # A read-only probe that runs between the boundary step and the first codex
    # pass: it reads no credential into the env and reintroduces nothing, so it
    # is an allowed direct predecessor of a codex pass PROVIDED a boundary step
    # still ran earlier in the same job.
    SANDBOX_VERIFY = "Verify the review sandbox fails closed (no sudo, no credential read)"

    def _jobs(self, workflow: str) -> list[list[dict]]:
        import yaml

        doc = yaml.safe_load(_workflow(workflow))
        return [job.get("steps") or [] for job in (doc.get("jobs") or {}).values()]

    @staticmethod
    def _is_codex_pass(step: dict) -> bool:
        return "/.bin/codex" in str(step.get("run") or "")

    @classmethod
    def _is_model_step(cls, step: dict) -> bool:
        if "anthropics/claude-code-action" in str(step.get("uses") or ""):
            return True
        # The codex lane invokes the model from a run block, so the `uses` test
        # alone would miss both of its passes.
        return cls._is_codex_pass(step)

    def _scrub_bodies(self) -> list[str]:
        bodies = []
        for workflow in self.WORKFLOWS:
            for steps in self._jobs(workflow):
                for step in steps:
                    if step.get("name") == self.SCRUB:
                        bodies.append(step.get("run") or "")
        return bodies

    def _boundary_bodies(self) -> list[str]:
        bodies = []
        for steps in self._jobs("fork-gpt-review.yml"):
            for step in steps:
                if step.get("name") in self.BOUNDARY:
                    bodies.append(step.get("run") or "")
        return bodies

    def test_every_model_step_is_directly_preceded_by_the_scrub(self) -> None:
        seen = 0
        for workflow in self.WORKFLOWS:
            for steps in self._jobs(workflow):
                for index, step in enumerate(steps):
                    if not self._is_model_step(step):
                        continue
                    seen += 1
                    where = f"{workflow}:{step.get('name') or step.get('id') or index}"
                    assert index > 0, f"{where}: model step is first, so nothing scrubbed"
                    prev = steps[index - 1].get("name")
                    # A shell-bearing codex pass must be preceded by a UID-boundary
                    # step (which truncates set_env_* AND removes the credential
                    # from the job env); every other model step by the scrub. The
                    # read-only sandbox fail-closed probe may sit directly before a
                    # codex pass, but only when a boundary step still ran earlier
                    # in the same job -- it removes nothing itself.
                    if self._is_codex_pass(step):
                        if prev == self.SANDBOX_VERIFY:
                            # The read-only probe sits between the boundary step
                            # and the pass, so the boundary step must be exactly
                            # TWO places back. "A boundary step anywhere earlier"
                            # would let a later edit slot `configure-aws-credentials`
                            # + the verify step in front of a pass while an
                            # earlier pass's boundary step still satisfied the
                            # check, leaking fresh credentials into the job env.
                            assert index >= 2, (
                                f"{where}: sandbox-verify precedes the pass but there "
                                f"is no boundary step two places back"
                            )
                            two_back = steps[index - 2].get("name")
                            assert two_back in self.BOUNDARY, (
                                f"{where}: the step two before the codex pass is "
                                f"{two_back!r}, not a credential-boundary step "
                                f"{self.BOUNDARY!r}"
                            )
                        else:
                            assert prev in self.BOUNDARY, (
                                f"{where}: the step before a shell-bearing codex pass is "
                                f"{prev!r}, not a credential-boundary step {self.BOUNDARY!r} "
                                f"or the sandbox-verify step"
                            )
                    else:
                        assert (
                            prev == self.SCRUB
                        ), f"{where}: the step before it is {prev!r}, not the scrub"
        # Guard the enumeration itself: a rename that makes `_is_model_step` match
        # nothing would otherwise turn this into a vacuous pass.
        assert seen == 5, f"expected 5 model steps across both lanes, found {seen}"

    def test_the_scrub_is_byte_identical_at_every_site(self) -> None:
        # Three scrub sites remain: the fork GPT lane's Opus adjudication pass and
        # both fork Opus lane model passes. The two GPT codex passes are now
        # fronted by the stronger UID-boundary steps instead.
        bodies = self._scrub_bodies()
        assert len(bodies) == 3, f"expected 3 scrub steps, found {len(bodies)}"
        assert len(set(bodies)) == 1, "the scrub bodies have drifted apart"

    def test_every_boundary_step_also_truncates_the_set_env_files(self) -> None:
        # The UID-boundary steps REPLACE the scrub before the codex passes, so
        # each must still truncate the on-disk set_env_* copies the scrub did.
        bodies = self._boundary_bodies()
        assert len(bodies) == 2, f"expected 2 credential-boundary steps, found {len(bodies)}"
        for body in bodies:
            assert (
                "_runner_file_commands" in body and "set_env_*" in body
            ), "a credential-boundary step does not truncate the set_env_* files"
            # It must also remove the credential from the job env for later steps.
            assert (
                "AWS_ACCESS_KEY_ID=" in body and "$GITHUB_ENV" in body
            ), "a credential-boundary step does not blank the AWS vars in GITHUB_ENV"

    def test_the_scrub_truncates_set_env_files_and_nothing_else(self, tmp_path: Path) -> None:
        bash = _bash()
        if bash is None:
            pytest.skip("the scrub step is exercised on POSIX runners")
        runner_temp = tmp_path / "runner"
        commands = runner_temp / "_runner_file_commands"
        commands.mkdir(parents=True)
        (commands / "set_env_one").write_text(
            "AWS_SECRET_ACCESS_KEY<<X\nsecret\nX\n", encoding="utf-8"
        )
        (commands / "set_env_two").write_text("AWS_SESSION_TOKEN<<Y\ntoken\nY\n", encoding="utf-8")
        # A sibling command file the runner still needs: truncating it too would
        # break state passing, so the pattern has to stay scoped to set_env_*.
        (commands / "save_state_keep").write_text("keep\n", encoding="utf-8")

        script = tmp_path / "scrub.sh"
        script.write_text(self._scrub_bodies()[0], encoding="utf-8", newline="\n")
        proc = subprocess.run(
            [bash, "-e", str(script)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            cwd=tmp_path,
            env={**os.environ, "RUNNER_TEMP": str(runner_temp)},
        )
        assert proc.returncode == 0, proc.stderr
        assert (commands / "set_env_one").read_text(encoding="utf-8") == ""
        assert (commands / "set_env_two").read_text(encoding="utf-8") == ""
        assert (commands / "save_state_keep").read_text(encoding="utf-8") == "keep\n"

    def test_a_missing_runner_directory_is_not_an_error(self, tmp_path: Path) -> None:
        # The scrub runs unconditionally before every model step, so a job whose
        # runner exposes no file-command directory must pass through it rather
        # than failing the lane before the review starts.
        bash = _bash()
        if bash is None:
            pytest.skip("the scrub step is exercised on POSIX runners")
        script = tmp_path / "scrub.sh"
        script.write_text(self._scrub_bodies()[0], encoding="utf-8", newline="\n")
        proc = subprocess.run(
            [bash, "-e", str(script)],
            check=False,
            capture_output=True,
            encoding="utf-8",
            cwd=tmp_path,
            env={**os.environ, "RUNNER_TEMP": str(tmp_path / "absent")},
        )
        assert proc.returncode == 0, proc.stderr


class TestReviewSandboxFailsClosed:
    """The fork GPT lane proves its UID credential boundary before the model runs.

    The boundary's defence against ``sudo cat /tmp/bedrock-signer.env`` is that
    codex's read-only sandbox (``no_new_privs``) denies the review subprocess
    any escalation. The job user has passwordless ``sudo`` and the sysctl that
    enables the sandbox substrate ends in ``|| true``, so if that sandbox fails
    to come up -- or a codex upgrade changes what ``read-only`` enforces --
    nothing else would go red. This step runs the escalation attempts through
    ``codex sandbox`` (the same Landlock+seccomp confinement the passes use) and
    fails the job closed when the boundary does not hold.
    """

    STEP = "Verify the review sandbox fails closed (no sudo, no credential read)"

    def _step(self) -> dict:
        return _step("fork-gpt-review.yml", self.STEP)

    def _script(self) -> str:
        return _step_script(_workflow("fork-gpt-review.yml"), self.STEP)

    def test_the_step_exists_and_runs_before_both_codex_passes(self) -> None:
        doc = yaml.safe_load(_workflow("fork-gpt-review.yml"))
        for job in (doc.get("jobs") or {}).values():
            steps = job.get("steps") or []
            names = [s.get("name") for s in steps]
            if self.STEP not in names:
                continue
            verify_at = names.index(self.STEP)
            codex_passes = [
                i
                for i, s in enumerate(steps)
                if "/.bin/codex" in str(s.get("run") or "") and " exec " in str(s.get("run") or "")
            ]
            assert codex_passes, "no codex exec pass found in the job"
            assert all(
                verify_at < i for i in codex_passes
            ), "the sandbox-verify step must run before every codex pass"
            return
        raise AssertionError(f"no job contains the step {self.STEP!r}")

    def test_it_probes_sudo_and_credential_read_through_the_codex_sandbox(self) -> None:
        script = self._script()
        # It must drive the probes through `codex sandbox` (the real confinement),
        # not a bare shell, or it would test the job user instead of the sandbox.
        # The pinned @openai/codex selects the mode via `-c sandbox_mode=...`.
        assert "sandbox -c sandbox_mode=read-only --" in script
        assert "sudo -n true" in script, "the step does not probe privilege escalation"
        # It reads the signer credential file the boundary is meant to protect.
        assert 'cat "$CRED_FILE"' in script
        env = _step_env("fork-gpt-review.yml", self.STEP)
        assert env.get("CRED_FILE") == "/tmp/bedrock-signer.env"

    def test_each_probe_failure_exits_nonzero(self) -> None:
        # The three guard clauses each `exit 1`: a broken sandbox (control
        # command fails), a successful sudo, and a readable credential file.
        script = self._script()
        assert script.count("exit 1") >= 3, (
            "the step must fail closed on a broken sandbox, a successful sudo, "
            "and a readable credential file"
        )
        # The control command (`$SANDBOX true`) must be REQUIRED to succeed, so a
        # probe that fails only because codex sandbox is broken is not a false pass.
        assert "if ! $SANDBOX true;" in script

    def test_it_runs_as_the_same_binary_the_passes_use(self) -> None:
        # The probe must exercise the SAME codex binary the review passes run,
        # or it proves nothing about the sandbox those passes get.
        env = _step_env("fork-gpt-review.yml", self.STEP)
        assert env.get("CODEX_BIN") == "${{ runner.temp }}/review-cli/node_modules/.bin/codex"
