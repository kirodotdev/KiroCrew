"""A late opaque pipe handle closes when a real MXC refusal stub terminates."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

PROGRAM = r"""
import asyncio, json, os
from pathlib import Path
from kiro_crew.mcp_gateway.required import serve_refusal
from kiro_crew.mcp_gateway.stub import _hard_exit

try:
    Path(os.environ["KIROCREW_HOME"], "trust", "sel_hmac.key").read_bytes()
    raise AssertionError("key readable")
except PermissionError:
    pass
print(json.dumps({"pid": os.getpid()}), flush=True)
print("handoff_already_timed_out", flush=True)
_hard_exit(asyncio.run(serve_refusal("connection_failed")))
"""


async def exercise(root, request, sandbox_runner):
    home = root / "crew"
    user = root / "home"
    scratch = home / "scratch/runtime-lifetime"
    for path in (home / "trust", user, scratch):
        path.mkdir(parents=True, exist_ok=True)
    retained = {
        k: v
        for k, v in os.environ.items()
        if k.upper()
        in {
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
            "PATHEXT",
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "SYSTEMDRIVE",
        }
    }
    os.environ.clear()
    os.environ.update(retained)
    os.environ.update(
        {
            "KIROCREW_HOME": str(home),
            "USERPROFILE": str(user),
            "HOME": str(user),
            "PATH": str(Path(sys.executable).parent)
            + os.pathsep
            + os.environ["SystemRoot"]
            + r"\System32",
            "USERNAME": "native-fixture",
            "USERDOMAIN": "synthetic",
            "APPDATA": str(user / "AppData/Roaming"),
            "LOCALAPPDATA": str(user / "AppData/Local"),
            "KIROCREW_WINDOWS_SANDBOX_EXE": str(sandbox_runner),
        }
    )
    (home / "trust/sel_hmac.key").write_bytes(b"synthetic-only-test-key-32-bytes!!")
    from kiro_crew import platform_compat
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.mcp_gateway.native_handles import _duplicate, _identity, _kernel, _pipe, _process
    from kiro_crew.platform_compat_wintree import windows_tree_admissions_pending
    from kiro_crew.sandbox_windows import _cleanup_windows_profile, windows_sandbox_argv

    platform_compat.restrict_to_owner(home / "trust/sel_hmac.key")
    KiroCrewConfig.load()
    wrapped, artifact = await asyncio.to_thread(
        windows_sandbox_argv,
        [sys.executable, "-c", PROGRAM],
        extra_hidden_dirs=(str(home / "trust"),),
        extra_private_dirs=(str(scratch),),
    )

    async def spawn(windows_cleanup_owner):
        return await platform_compat._create_windows_subprocess_owned(
            windows_cleanup_owner,
            *wrapped,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(root),
        )

    process = await platform_compat.create_windows_cleanup_owned_process(spawn)
    reader, writer = _pipe()
    closed_writer = False
    try:
        first = await asyncio.wait_for(process.stdout.readline(), 15)
        if not first:
            raise AssertionError((await process.stderr.read()).decode())
        pid = json.loads(first)["pid"]
        assert (await process.stdout.readline()).strip() == b"handoff_already_timed_out"
        with _process(pid) as target:
            if request:
                process.stdin.write(
                    b'{"jsonrpc":"2.0","id":42,"method":"initialize","params":{}}\n'
                )
                await process.stdin.drain()
                reply = json.loads(await asyncio.wait_for(process.stdout.readline(), 5))
                assert reply["id"] == 42 and reply["error"]["data"]["class"] == "connection_failed"
                await asyncio.wait_for(process.wait(), 8)
                try:
                    _identity(target)
                except PermissionError:
                    pass
                else:
                    raise AssertionError("failed stub remained live after its RPC refusal")
            else:
                # Deliver a handle only AFTER handoff failure, while the stub
                # is still alive with open stdin and no request to answer.
                _duplicate(_kernel().GetCurrentProcess(), writer, target, access=0x00100002)
                process.stdin.write(
                    b'null\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                )
                await process.stdin.drain()
                _kernel().CloseHandle(writer)
                closed_writer = True
                await asyncio.wait_for(process.wait(), 8)
            if not closed_writer:
                _kernel().CloseHandle(writer)
                closed_writer = True
            import _winapi

            try:
                data, _ = _winapi.ReadFile(reader, 1)
                assert not data, "unexpected pipe payload"
            except OSError as exc:
                assert exc.winerror in {109, 232}, exc
        assert process.returncode != 0
        print(
            json.dumps(
                {
                    "status": "passed",
                    "rpc_refusal": request,
                    "silent_or_invalid_input_deadline": not request,
                    "late_pipe_handle_closed": True,
                    "native_key_denied": True,
                }
            )
        )
    finally:
        if not closed_writer:
            _kernel().CloseHandle(writer)
        _kernel().CloseHandle(reader)
        await platform_compat.terminate_windows_asyncio_tree(process)
        assert windows_tree_admissions_pending() == 0
        _cleanup_windows_profile(Path(artifact))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runner", required=True, type=lambda value: Path(value).resolve(strict=True)
    )
    parser.add_argument("--rpc", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Real Windows MXC is required")
    with tempfile.TemporaryDirectory(prefix="crew-native-lifetime-") as temp:
        asyncio.run(exercise(Path(temp), args.rpc, args.runner))
