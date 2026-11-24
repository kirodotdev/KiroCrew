"""A server npm could not download because a registry refused its sign-in.

An ``npx``-launched MCP server whose registry token has expired fails before it starts,
and npm's stderr puts its error code first and public-registry advice last. The probe's
error leads with one sentence of Kiro Crew's own that names the refusal, the registry,
and the fix; npm's own text stays underneath. The registry is the one npm's stderr
names, else the one npm's settings give the package (``npm config get``).

The stderr texts are npm 11's, captured from ``npx -y @playwright/mcp@latest`` against
a loopback registry answering each way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_discovery_helpers import _passthrough_sandbox  # noqa: F401 -- autouse by import

from kiro_crew import mcp_discovery_probe, mcp_npm_sign_in
from kiro_crew.mcp_discovery import McpServerInfo, probe_server
from kiro_crew.mcp_npm_sign_in import npm_sign_in_hint

#: npm's last stderr line, naming the debug log it wrote for the run.
_LOG_LINE = "npm error A complete log of this run can be found in: /h/.npm/_logs/x-debug-0.log"


#: A 401 with a Basic challenge: npm's longest answer, whose code line the shown end loses.
_BASIC_401 = [
    "npm error code E401",
    "npm error Incorrect or missing password.",
    "npm error If you were trying to login, change your password, create an "
    "authentication token or enable two-factor authentication then that means you "
    "likely typed your password in incorrectly.",
    "npm error Please try again, or recover your password at:",
    "npm error   https://www.npmjs.com/forgot",
    "npm error",
    "npm error If you were doing some other operation then your saved credentials are "
    "probably out of date.",
    "npm error To correct this please try logging in again with:",
    "npm error   npm login",
]
#: A 401 with a Bearer challenge, the answer an expired registry token gets.
_BEARER_401 = [
    "npm error code E401",
    "npm error Unable to authenticate, your authentication token seems to be invalid.",
    "npm error To correct this please try logging in again with:",
    "npm error   npm login",
]
_PLAIN_401 = [
    "npm error code E401",
    "npm error 401 Unauthorized - GET http://127.0.0.1:33973/@playwright%2fmcp - refused",
]
_PLAIN_403 = [
    "npm error code E403",
    "npm error 403 403 Forbidden - GET https://user:pw@registry.example.com/@playwright%2fmcp"
    " - refused",
    "npm error 403 In most cases, you or one of your dependencies are requesting a package "
    "version that is forbidden by your security policy, or on a server you do not have "
    "access to.",
]
_OLD_NPM = ["npm ERR! code E401", 'npm ERR! Unable to authenticate, need: Basic realm="x"']

#: How the sentence names a registry whose address npm's stderr leaves out.
_UNNAMED = "the package registry in your npm settings"


#: The environment and arguments a failed ``npx`` server was launched with.
_ENV = {"PATH": "/opt/node/bin"}
_ARGS = ["-y", "@playwright/mcp@latest"]


def _stderr(lines: list[str]) -> str:
    return "\n".join([*lines, _LOG_LINE])


@pytest.fixture
def settings_registry() -> Iterator[AsyncMock]:
    """What npm's settings answer for the registry; none unless a test sets one."""
    with patch.object(mcp_npm_sign_in, "_configured_registry", AsyncMock(return_value="")) as asked:
        yield asked


@pytest.mark.parametrize(
    ("stderr", "says"),
    [
        pytest.param(_stderr(_BASIC_401), f"{_UNNAMED} did not accept", id="basic-401"),
        pytest.param(_stderr(_BEARER_401), f"{_UNNAMED} did not accept", id="bearer-401"),
        pytest.param(
            _stderr(_PLAIN_401),
            "the package registry 127.0.0.1:33973 in your npm settings did not accept",
            id="plain-401",
        ),
        pytest.param(
            _stderr(_PLAIN_403),
            "the package registry registry.example.com in your npm settings refused npm access",
            id="plain-403",
        ),
        pytest.param("\r\n".join(_OLD_NPM), f"{_UNNAMED} did not accept", id="old-npm-crlf"),
    ],
)
@pytest.mark.asyncio
async def test_each_refusal_npm_prints_is_named_with_its_code_and_fix(
    stderr: str, says: str, settings_registry: AsyncMock
) -> None:
    hint = await npm_sign_in_hint(stderr, _ENV, _ARGS)

    code = "E403" if "E403" in stderr else "E401"
    assert hint.startswith("npm could not download this server: ")
    assert says in hint and f"(npm error {code})" in hint
    assert hint.endswith(", then test the server again.")
    assert "user" not in hint and "pw" not in hint and "/.npm/" not in hint
    # npm is asked only when its stderr leaves the registry out.
    assert settings_registry.await_count == (1 if _UNNAMED in says else 0)


@pytest.mark.asyncio
async def test_a_refusal_with_no_challenge_advises_a_new_sign_in_and_a_403_its_access(
    settings_registry: AsyncMock,
) -> None:
    assert "Sign in to that registry again or renew its token" in await npm_sign_in_hint(
        _stderr(_PLAIN_401), _ENV, _ARGS
    )
    assert "may download this package" in await npm_sign_in_hint(_stderr(_PLAIN_403), _ENV, _ARGS)


@pytest.mark.asyncio
@pytest.mark.parametrize("lines", [_BASIC_401, _BEARER_401], ids=["basic-401", "bearer-401"])
async def test_a_refusal_whose_stderr_names_no_registry_names_the_one_npm_settings_give(
    lines: list[str], settings_registry: AsyncMock
) -> None:
    """npm leaves the address out of a refusal with a sign-in challenge, so the host is
    the one npm says its settings use for the package; never the user part."""
    settings_registry.return_value = "https://user:pw@npm.example.com:8443/repo/"

    hint = await npm_sign_in_hint(_stderr(lines), _ENV, _ARGS)

    assert "the package registry npm.example.com:8443 in your npm settings did not accept" in hint
    assert "user" not in hint and "pw" not in hint and "/repo" not in hint
    settings_registry.assert_awaited_once_with(_ENV, _ARGS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(AsyncMock(return_value="not a url"), id="no-host"),
        pytest.param(AsyncMock(side_effect=OSError("npm failed")), id="npm-failed"),
        pytest.param(AsyncMock(side_effect=asyncio.TimeoutError()), id="npm-stuck"),
    ],
)
async def test_a_registry_npm_cannot_name_leaves_the_sentence_unnamed(answer: AsyncMock) -> None:
    with patch.object(mcp_npm_sign_in, "_configured_registry", answer):
        hint = await npm_sign_in_hint(_stderr(_BASIC_401), _ENV, _ARGS)

    assert f"{_UNNAMED} did not accept npm's sign-in (npm error E401)" in hint


@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param("", id="empty"),
        pytest.param(
            "npm error code E404\nnpm error 404 Not Found - GET https://registry.npmjs.org/nope",
            id="not-found",
        ),
        pytest.param("Error: upstream answered E401 for the forecast", id="server-text"),
        pytest.param("tool: npm error code E401 was reported", id="mid-line"),
    ],
)
@pytest.mark.asyncio
async def test_anything_but_npm_own_sign_in_code_line_gets_no_sentence(
    stderr: str, settings_registry: AsyncMock
) -> None:
    assert await npm_sign_in_hint(stderr, _ENV, _ARGS) == ""
    settings_registry.assert_not_awaited()


def _npm_config(stdout: str) -> AsyncMock:
    """An ``npm config get`` run that prints *stdout* and exits."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout.encode(), b""))
    return AsyncMock(return_value=proc)


@pytest.fixture
def unwrapped() -> Iterator[AsyncMock]:
    """The sandbox wrap, passing the argv and env through unchanged."""

    async def _wrap(argv, *_a, env=None, **_k):
        return list(argv), dict(env or {}), None

    with patch.object(mcp_npm_sign_in, "sandboxed_spawn_argv_async", side_effect=_wrap) as wrap:
        yield wrap


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "keys", "stdout", "registry"),
    [
        pytest.param(
            ["-y", "@corp/mcp@latest"],
            ["@corp:registry", "registry"],
            "@corp:registry=https://corp.example/npm/\nregistry=https://registry.npmjs.org/\n",
            "https://corp.example/npm/",
            id="scope-has-its-own",
        ),
        pytest.param(
            ["--package=@corp/mcp", "corp-mcp"],
            ["@corp:registry", "registry"],
            "@corp:registry=undefined\nregistry=https://registry.npmjs.org/\n",
            "https://registry.npmjs.org/",
            id="scope-has-none",
        ),
        pytest.param(
            ["--yes", "-p", "@corp/mcp", "corp-mcp"],
            ["@corp:registry", "registry"],
            "@corp:registry=https://corp.example/npm/\nregistry=https://registry.npmjs.org/\n",
            "https://corp.example/npm/",
            id="package-option",
        ),
        pytest.param(
            ["-y", "weather-mcp", "--city", "@home/x"],
            ["registry"],
            "https://registry.npmjs.org/\n",
            "https://registry.npmjs.org/",
            id="unscoped",
        ),
    ],
)
async def test_npm_is_asked_for_the_package_scope_registry_before_the_default(
    args: list[str], keys: list[str], stdout: str, registry: str, unwrapped: AsyncMock
) -> None:
    spawn = _npm_config(stdout)
    with (
        patch.object(mcp_npm_sign_in.shutil, "which", return_value="/opt/node/bin/npm"),
        patch.object(mcp_npm_sign_in, "create_subprocess_limited", spawn),
    ):
        assert await mcp_npm_sign_in._configured_registry(_ENV, args) == registry

    argv = unwrapped.await_args.args[0]
    assert argv == ["/opt/node/bin/npm", "config", "get", *keys]
    assert unwrapped.await_args.kwargs["env"] == _ENV
    assert spawn.await_args.kwargs["env"] == _ENV


@pytest.mark.asyncio
async def test_no_npm_on_the_server_path_asks_nothing(unwrapped: AsyncMock) -> None:
    with patch.object(mcp_npm_sign_in.shutil, "which", return_value=None) as which:
        assert await mcp_npm_sign_in._configured_registry(_ENV, _ARGS) == ""

    which.assert_called_once_with("npm", path="/opt/node/bin")
    unwrapped.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_npm_that_does_not_answer_is_stopped(unwrapped: AsyncMock) -> None:
    proc = MagicMock()
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    with (
        patch.object(mcp_npm_sign_in.shutil, "which", return_value="/opt/node/bin/npm"),
        patch.object(mcp_npm_sign_in, "create_subprocess_limited", AsyncMock(return_value=proc)),
        patch.object(mcp_npm_sign_in.platform_compat, "kill_and_reap", AsyncMock()) as reap,
        pytest.raises(asyncio.TimeoutError),
    ):
        await mcp_npm_sign_in._configured_registry(_ENV, _ARGS)

    reap.assert_awaited_once_with(proc)


def _npx_process(stderr: str) -> AsyncMock:
    """A launcher that exits without answering, having written *stderr*."""
    proc = AsyncMock()
    proc.returncode = 1
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdout = AsyncMock()
    proc.stdout.readline = AsyncMock(return_value=b"")
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(side_effect=[stderr.encode(), b""])
    return proc


@pytest.mark.asyncio
async def test_the_probe_error_leads_with_the_sentence_npm_cut_off_the_shown_end() -> None:
    """The code line is past the shown end of a Basic-challenge refusal, so the sentence
    comes from the whole text; npm's own end is still shown under it."""
    stderr = _stderr(_BASIC_401)
    shown = mcp_discovery_probe._last_lines(stderr, mcp_discovery_probe._STDERR_SHOWN_CHARS)
    assert "E401" not in shown
    server = McpServerInfo(name="playwright-mcp", command="npx", args=["-y", "@playwright/mcp"])

    with (
        patch(
            "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
            return_value=_npx_process(stderr),
        ),
        patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/npx"),
        patch.object(
            mcp_npm_sign_in,
            "_configured_registry",
            AsyncMock(return_value="http://127.0.0.1:33973/"),
        ) as settings_registry,
    ):
        result = await probe_server(server)

    assert result.status == "error"
    first, _, rest = result.error.partition("\n")
    assert "the package registry 127.0.0.1:33973 in your npm settings did not accept" in first
    assert rest == f"no response\nstderr: {shown}"
    # npm is asked with the environment and arguments the server was launched with.
    env, args = settings_registry.await_args.args
    assert env["PATH"] and args == ["-y", "@playwright/mcp"]


@pytest.mark.asyncio
async def test_a_failure_with_no_npm_sign_in_code_keeps_its_error_as_it_was() -> None:
    server = McpServerInfo(name="weather", command="npx", args=["-y", "weather-mcp"])
    why = "Error: Cannot find module 'weather-core'"

    with (
        patch(
            "kiro_crew.mcp_discovery.asyncio.create_subprocess_exec",
            return_value=_npx_process(why),
        ),
        patch("kiro_crew.mcp_discovery.shutil.which", return_value="/usr/bin/npx"),
        patch.object(mcp_npm_sign_in, "_configured_registry", AsyncMock()) as settings_registry,
    ):
        result = await probe_server(server)

    assert result.error == f"no response\nstderr: {why}"
    settings_registry.assert_not_awaited()
