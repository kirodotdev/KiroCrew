"""A running stub never loads the gateway's pool, manager or metrics graph.

The stub is the most replicated process Kiro Crew runs, one per session per MCP
server, so whatever it loads is paid for once per stub on the host.

The bound here is on the STEADY STATE -- the module set a stub holds once it has
built its Register payload and its log label, which is the state it bridges in
and sits at. Bounding the module set right after ``import`` is the wrong
test, and a change that saves nothing passes it: Python never unloads a module,
so one the stub imports two calls later costs exactly as much as one its body
imports. The import-time figure is not the figure that describes memory at rest.

The bound is a NAMED SET, not a byte figure. Resident set depends on the
interpreter build, the libc allocator and what else the host is doing, so a
number pinned here would either be loose enough to let a regression through or
tight enough to fail on somebody else's machine. A module name is exact.

Every check runs in a FRESH interpreter. Other tests in this suite import these
modules, so an in-process assertion would answer about the session rather than
about the stub.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.subprocess_utf8 import UTF8_TEXT

#: Modules a stub must not hold once it is bridging. Each has a large
#: transitive graph and is reached, if at all, on a path a stub takes rarely:
#: ``pool`` and ``manager`` are the daemon's side of the socket, ``metrics``
#: counts a reconnect, ``jsonl_util`` writes a fallback record, and
#: ``config.loader`` resolves a ceiling the rewriter already resolved.
#:
#: Deliberately specific rather than a parent package: a parent would also
#: catch a cheap leaf landing beside an expensive one, and report a failure
#: that does not say which module grew the footprint.
FORBIDDEN_WHEN_BRIDGING = (
    "kiro_crew.config.loader",
    "kiro_crew.mcp_gateway.pool",
    "kiro_crew.mcp_gateway.manager",
    "kiro_crew.mcp_gateway.backend",
    "kiro_crew.mcp_gateway.rewriter",
    "kiro_crew.metrics.events",
    "kiro_crew.jsonl_util",
    "kiro_crew.security",
    "kiro_crew.validation",
)

#: Additionally absent after a bare ``import``: building the Register payload
#: hashes the target binary, which reaches ``code_fingerprint`` for one of Kiro
#: Crew's own servers. ``mcp_caller`` and ``mcp_cleanup`` are NOT here: every
#: run builds a Register payload, so importing them in the module body changes
#: when rather than whether, and the eager spelling is the readable one.
FORBIDDEN_AT_IMPORT = FORBIDDEN_WHEN_BRIDGING + ("kiro_crew.code_fingerprint",)

#: What the rewriter stamps into every overlay it writes. 64 MiB is the shipped
#: default, so a probe using it asserts the plumbing without also asserting a
#: particular operator's config.
FLAG_CEILING = 64 * 1024 * 1024

#: Enough argv for ``build_register_payload`` to run, plus the ceiling flag the
#: rewriter writes. ``--work-dir`` and ``--target-command`` are the only
#: required ones beyond server and agent.
STUB_ARGV = [
    "--socket",
    "/nonexistent/gateway.sock",
    "--server",
    "kirocrew-core",
    "--agent",
    "a",
    "--target-command",
    "/bin/true",
    "--work-dir",
    "/tmp",
    "--read-limit",
    str(FLAG_CEILING),
]


def _run_probe(
    tmp_path: Path, code: str, *, session_key: bool = True
) -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter against this checkout's ``src``.

    ``session_key`` picks which rung of the caller identity the probe exercises.
    With it, ``CallerContext.from_env`` answers from the environment (rung 1),
    which is what a claimed session does. Without it, resolution falls through
    to the warm-pool pid mapping (rung 4) -- a real path, taken by every stub a
    pre-spawned kiro-cli starts before its session is claimed, and one that
    reads ``config_dir``. Both must satisfy the bound, and only running both
    says so.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["KIROCREW_HOME"] = str(tmp_path / "crew")
    if session_key:
        env["KIROCREW_SESSION_KEY"] = "chat-probe"
    else:
        env.pop("KIROCREW_SESSION_KEY", None)
    return subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=120,
        **UTF8_TEXT,
    )


@pytest.mark.parametrize(
    "session_key", [True, False], ids=["claimed-session", "warm-pool-no-session-key"]
)
def test_bridging_stub_holds_none_of_the_heavy_modules(tmp_path, session_key):
    """After the Register payload and the log label, none of them is resident.

    This is the regression bound. It drives the same calls ``_amain`` makes
    before it connects, so a heavy import moved out of the module body and into
    one of them does not pass.

    Run on BOTH identity rungs. The warm-pool case is not a variation for its
    own sake: a stub with no session key yet resolves its caller through the pid
    mapping, which reads ``config_dir``, and taking that from ``config.loader``
    rather than the ``config.paths`` leaf that defines it put the whole config
    package back into every pre-claim stub.
    """
    code = (
        "import json, sys\n"
        "import kiro_crew.mcp_gateway.stub as stub\n"
        f"args = stub._parse_args({STUB_ARGV!r})\n"
        "stub._adopt_read_ceiling(args)\n"
        "payload = stub.build_register_payload(args)\n"
        "label = stub.format_pool_label(payload)\n"
        "assert label, 'the stub must still produce a pool label'\n"
        f"forbidden = {list(FORBIDDEN_WHEN_BRIDGING)!r}\n"
        "print(json.dumps(sorted(m for m in forbidden if m in sys.modules)))\n"
    )
    result = _run_probe(tmp_path, code, session_key=session_key)
    assert result.returncode == 0, result.stderr
    resident = json.loads(result.stdout.strip().splitlines()[-1])
    assert resident == [], (
        "a stub that has adopted its ceiling and built its Register payload and "
        f"label is holding modules it must not: {resident}. Import the module "
        "inside the function that needs it, take the value from a leaf (see "
        "mcp_gateway.read_limits, hashing.format_pool_label), or have the "
        "rewriter hand it over on argv the way the read ceiling arrives."
    )


def test_stub_import_loads_even_less(tmp_path):
    """A bare import reaches neither the bridging set nor the startup modules."""
    code = (
        "import json, sys\n"
        "import kiro_crew.mcp_gateway.stub\n"
        f"forbidden = {list(FORBIDDEN_AT_IMPORT)!r}\n"
        "print(json.dumps(sorted(m for m in forbidden if m in sys.modules)))\n"
    )
    result = _run_probe(tmp_path, code)
    assert result.returncode == 0, result.stderr
    resident = json.loads(result.stdout.strip().splitlines()[-1])
    assert resident == [], f"importing the stub loaded: {resident}"


def test_stub_label_matches_the_poolkey_the_daemon_builds(tmp_path):
    """The stub's label and ``PoolKey.human_readable`` are the same string.

    An operator matches a stub's log line to the daemon's by eye, so the two
    sides naming one identity differently would be a real regression even
    though nothing would fail.
    """
    code = (
        "import kiro_crew.mcp_gateway.stub as stub\n"
        f"args = stub._parse_args({STUB_ARGV!r})\n"
        "payload = stub.build_register_payload(args)\n"
        "from kiro_crew.mcp_gateway.pool import PoolKey\n"
        "mine = stub.format_pool_label(payload)\n"
        "theirs = PoolKey.from_register(payload).human_readable()\n"
        "assert mine == theirs, (mine, theirs)\n"
    )
    result = _run_probe(tmp_path, code)
    assert result.returncode == 0, result.stderr


def test_label_names_every_field_it_is_missing():
    """A payload short of a label field raises, rather than printing ``None``."""
    from kiro_crew.mcp_gateway.hashing import format_pool_label

    with pytest.raises(ValueError) as caught:
        format_pool_label({"agent_name": "a", "server_name": "s"})
    message = str(caught.value)
    for field in ("os_uid", "effective_env_hash", "command_args_hash", "work_dir"):
        assert field in message, (field, message)


def test_adopted_ceiling_comes_off_argv_without_reading_config(tmp_path):
    """The flag the rewriter wrote is the ceiling, and config stays unloaded."""
    code = (
        "import sys\n"
        "import kiro_crew.mcp_gateway.stub as stub\n"
        f"args = stub._parse_args({STUB_ARGV!r})\n"
        f"assert stub._adopt_read_ceiling(args) == {FLAG_CEILING}\n"
        f"assert stub._HELD_FRAME_BYTES == {FLAG_CEILING}\n"
        "assert 'kiro_crew.config.loader' not in sys.modules\n"
    )
    result = _run_probe(tmp_path, code)
    assert result.returncode == 0, result.stderr


def test_env_var_still_beats_the_flag(tmp_path):
    """``KIROCREW_MCP_READ_LIMIT`` stays the per-process override it was."""
    code = (
        "import os\n"
        "os.environ['KIROCREW_MCP_READ_LIMIT'] = '2048'\n"
        "import kiro_crew.mcp_gateway.stub as stub\n"
        f"args = stub._parse_args({STUB_ARGV!r})\n"
        "assert stub._adopt_read_ceiling(args) == 2048\n"
    )
    result = _run_probe(tmp_path, code)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("bad", ["", "not-a-number", "16"])
def test_absent_or_unusable_flag_resolves_the_old_way(tmp_path, bad):
    """An overlay without a usable ceiling still honours the config key.

    This is the compatibility path, and it is about correctness rather than
    tidiness: answering a missing flag with the bare default would silently
    ignore a ``read_buffer_limit_bytes`` an operator had set, until something
    happened to rewrite the overlay. ``16`` stands for a value under the 1024
    floor, which the resolver rejects the same way it always has.
    """
    argv = [a for a in STUB_ARGV if a not in ("--read-limit", str(FLAG_CEILING))]
    if bad:
        argv += ["--read-limit", bad]
    code = (
        "import sys\n"
        "from unittest.mock import patch\n"
        "import kiro_crew.mcp_gateway.stub as stub\n"
        f"args = stub._parse_args({argv!r})\n"
        "with patch('kiro_crew.config.loader._raw_config',\n"
        "           return_value={'mcp_gateway': {'read_buffer_limit_bytes': 4194304}}):\n"
        "    assert stub._adopt_read_ceiling(args) == 4194304\n"
    )
    result = _run_probe(tmp_path, code)
    assert result.returncode == 0, result.stderr


def _fingerprint(limit: int) -> dict:
    """``_rewrite_inputs_fingerprint`` with everything but the ceiling pinned."""
    from kiro_crew.mcp_gateway import rewriter

    return rewriter._rewrite_inputs_fingerprint(
        source_dir=Path("/tmp"),
        settings_path=Path("/tmp/settings.json"),
        overlay_dir=Path("/tmp/overlay"),
        socket_path=Path("/tmp/gateway.sock"),
        work_dir=Path("/tmp"),
        sandbox_mode="standard",
        approval_mode="on-request",
        stub_set=frozenset(["kirocrew-core"]),
        pooling_enabled=True,
        forward_env=False,
        identity_keys=(),
        read_buffer_limit=limit,
    )


def test_raising_the_ceiling_alone_invalidates_the_overlay_cache():
    """The ceiling is baked into the overlay, so it is a fingerprint input.

    ``rewrite_agents`` skips the rewrite loop when the fingerprint matches, so a
    ceiling that was written onto every stub's argv but left out of it would
    leave the overlays untouched after the operator raised
    ``mcp_gateway.read_buffer_limit_bytes``: a restart would reuse the cached
    overlay and every stub would keep taking the previous ceiling, with nothing
    saying so until some unrelated input happened to change.
    """
    low, high = _fingerprint(64 * 1024 * 1024), _fingerprint(128 * 1024 * 1024)

    assert low["read_buffer_limit"] == 64 * 1024 * 1024
    assert high["read_buffer_limit"] == 128 * 1024 * 1024
    assert low != high, (
        "the ceiling is written into every overlay but does not move the "
        "fingerprint, so a raised limit will not regenerate them"
    )


def test_one_rewrite_pass_writes_one_ceiling(tmp_path):
    """Every overlay in a pass carries the SAME ceiling, resolved once.

    Both ends of a stub<->gatewayd socket size their reader from one answer, so
    a pass that read config per entry could hand two stubs different ceilings if
    the file changed under it. ``_build_stub_entry`` therefore takes the value
    rather than resolving it.
    """
    import inspect

    from kiro_crew.mcp_gateway import rewriter

    assert "read_buffer_limit" in inspect.signature(rewriter._build_stub_entry).parameters
    body = inspect.getsource(rewriter._build_stub_entry)
    assert '"--read-limit", str(read_buffer_limit)' in body
    assert "config_read_buffer_limit(" not in body, (
        "_build_stub_entry resolves the ceiling itself, so one pass can write "
        "two different ones and the fingerprint records neither"
    )


def test_the_module_body_defaults_the_ceiling_without_reading_config(tmp_path):
    """Before anything adopts a flag, the three bounds hold the shipped default.

    The module body must not resolve the ceiling -- that is the config read this
    change exists to remove -- so it starts from the stdlib leaf's constant and
    ``_adopt_read_ceiling`` replaces it. These are plain module globals, which
    is also the seam ``test_stub_reconnect_queued_calls.py`` overrides.
    """
    code = (
        "import sys\n"
        "import kiro_crew.mcp_gateway.stub as stub\n"
        "from kiro_crew.mcp_gateway.read_limits import _DEFAULT_READ_BUFFER_LIMIT\n"
        "assert stub.READ_BUFFER_LIMIT_BYTES == _DEFAULT_READ_BUFFER_LIMIT\n"
        "assert stub._HELD_FRAME_BYTES == _DEFAULT_READ_BUFFER_LIMIT\n"
        "assert stub._HELD_TOTAL_BYTES == _DEFAULT_READ_BUFFER_LIMIT\n"
        "assert 'kiro_crew.config.loader' not in sys.modules\n"
    )
    result = _run_probe(tmp_path, code)
    assert result.returncode == 0, result.stderr


def test_a_lowered_ceiling_does_not_tighten_the_retention_floor(tmp_path):
    """``_HELD_TOTAL_BYTES`` stays at the shipped default when the key is lowered.

    The key accepts 1 KiB, and a disconnected stub that could hold only 1 KiB
    would drop frames it was meant to replay on reconnect.
    """
    argv = [a for a in STUB_ARGV if a != str(FLAG_CEILING)]
    argv[argv.index("--read-limit") + 1 : argv.index("--read-limit") + 1] = ["4096"]
    code = (
        "import kiro_crew.mcp_gateway.stub as stub\n"
        "from kiro_crew.mcp_gateway.read_limits import _DEFAULT_READ_BUFFER_LIMIT\n"
        f"args = stub._parse_args({argv!r})\n"
        "assert stub._adopt_read_ceiling(args) == 4096\n"
        "assert stub._HELD_FRAME_BYTES == 4096\n"
        "assert stub._HELD_TOTAL_BYTES == _DEFAULT_READ_BUFFER_LIMIT\n"
    )
    result = _run_probe(tmp_path, code)
    assert result.returncode == 0, result.stderr


def test_execution_context_does_not_load_validation():
    """``execution_context`` takes one integer from a leaf, not a graph.

    It sits on the identity path every stub walks at startup
    (``CallerContext.from_env`` -> ``member_memory_auth``), and importing
    ``validation`` for ``MAX_SHORT_STRING`` reached ``artifact_store``,
    ``computer_use``, ``config.sections``, ``monitoring`` and ``project_scope``
    -- and ``security`` behind it. Checked in a fresh interpreter because the
    suite has all of those loaded.
    """
    import subprocess

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import sys, kiro_crew.execution_context\n"
            "assert 'kiro_crew.validation' not in sys.modules\n"
            "assert 'kiro_crew.security' not in sys.modules\n",
        ],
        env=env,
        capture_output=True,
        timeout=120,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr


def test_validation_still_binds_the_constant_that_moved():
    """``validation.MAX_SHORT_STRING`` is the leaf's object, for its 15 readers."""
    from kiro_crew import constants, validation

    assert validation.MAX_SHORT_STRING is constants.MAX_SHORT_STRING
    assert validation.MAX_SHORT_STRING == 500
