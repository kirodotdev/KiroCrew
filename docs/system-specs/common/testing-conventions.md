# Testing Conventions

Last Updated: 2026-07-18

## Framework

- `pytest` with `pytest-asyncio` for async tests
- Coverage via `pytest-cov`

## File Layout

```
test/
├── test_acp_types.py     # ACP type dataclasses
├── test_acp_client.py    # ACP client (mocked subprocess)
├── test_config.py        # Config loader
└── test_cli.py           # CLI commands
```

## Patterns

### Grouping
Group related tests in classes:
```python
class TestAcpClientInit:
    def test_defaults(self): ...
    def test_custom_work_dir(self, tmp_path): ...
```

### Async tests
```python
@pytest.mark.asyncio
async def test_read_message(self, tmp_path):
    ...
```

### Mocking kiro-cli
Never spawn real `kiro-cli` in tests. Mock the subprocess:
```python
mock_process = MagicMock()
mock_stdout = AsyncMock()
mock_stdout.readline = AsyncMock(return_value=line.encode())
mock_process.stdout = mock_stdout
mock_process.returncode = None
client._process = mock_process
```

### Config overrides
Use `monkeypatch` to override config paths:
```python
def test_load_from_file(self, tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
```

### Filesystem tests
Use `tmp_path` fixture:
```python
def test_custom_work_dir(self, tmp_path):
    client = AcpClient(work_dir=tmp_path)
```

### Patch the defining module, not a re-export

`monkeypatch.setattr`/`patch` rebind a NAME in one module namespace. Code
reads its globals from its **defining** module, so patching a package
re-export (e.g. `kiro_crew.dashboard.handlers.X`, imported there from
`handlers/sessions.py`) is a **silent no-op** — the test still passes but
exercises the production value. Symptom: a test that "shortens" a timeout yet
still takes the full production duration.

```python
# WRONG — handlers/__init__.py only re-exports the constant; sessions.py
# still reads its own module global (test silently waits the real 10s):
monkeypatch.setattr("kiro_crew.dashboard.handlers._SHUTDOWN_TIMEOUT_SECS", 0.05)

# RIGHT — patch where the constant is defined and read:
monkeypatch.setattr("kiro_crew.dashboard.handlers.sessions._SHUTDOWN_TIMEOUT_SECS", 0.05)
```

### Loop-wiring tests stub every dispatched operation

A test that drives a periodic/maintenance loop (e.g. `SessionManager.
_cleanup_loop`) pins the loop's *wiring* — which operations run, with what
args, and when. Stub **all** of them: any sweep left unstubbed runs for real
against the dev machine (process-table scans, `~/.kirocrew` PID files), which
violates the isolation rules below and costs seconds per test (an unstubbed
`find_orphan_mcp_candidates` alone added ~9s to every `TestCleanupLoop`
test). The sweep's own behavior belongs in its own module's tests.

## Rules

- Tests MUST NOT spawn real kiro-cli processes
- Tests MUST NOT depend on `~/.kirocrew/` existing
- Tests MUST NOT write into the operator's real data dir. A data-dir path that is
  bound **at import time** (e.g. `subagent_persistence._SUBAGENTS_DIR`, set to
  `config_dir() / "subagents"` on first import; or `sel._DEFAULT_DIR`) is NOT
  covered by the `KIROCREW_HOME` env safety net, because that env var is read
  after the module already captured the path. `conftest.py` pins each such global
  with a dedicated autouse fixture (`_isolate_subagents_dir`,
  `_isolate_sel_default_dir`, …). Paths that instead call `config_dir()` lazily on
  each use (e.g. `agent_state`) already honor `KIROCREW_HOME`. A test that spawns
  subagents or persists agent folders without isolating the import-time global
  leaks stub folders into `~/.kirocrew/subagents/`, which a running gateway then
  sweeps as orphans on its next restart.
- Tests SHOULD be fast (< 1s each)
- Async tests MUST use `@pytest.mark.asyncio`

## Exploratory Testing via Manual Command Execution

For integration issues involving external processes (kiro-cli, MCP servers, build
tools), use the **observe → diagnose → fix → verify** pattern:

### When to Use

- Debugging protocol-level issues (ACP JSON-RPC, MCP handshake)
- Investigating timing/ordering problems (async init, notification delivery)
- Verifying build pipeline behavior (setuptools, npm, pip)
- Any issue where mocked unit tests can't reproduce the real behavior

### Method

1. **Write a minimal script** that reproduces the exact subprocess interaction:
   - Spawn the real process (`kiro-cli acp`, `aim mcp install`, etc.)
   - Send inputs step by step
   - Log every output with timestamps
   - Use large stdout buffers (`limit=10*1024*1024`) to avoid truncation

2. **Observe raw behavior** — don't assume, capture everything:
   - Log all JSON-RPC messages (method, id, params keys)
   - Record timing (when does each message arrive relative to start?)
   - Note message classification (notification vs response vs request)

3. **Identify root cause** from observations, not from reading code alone

4. **Apply minimal fix** targeting the observed root cause

5. **Re-run the same script** to verify the fix works end-to-end

### Example: ACP Protocol Testing

```python
"""Test ACP handshake and MCP server loading."""
import asyncio, json, time

async def main():
    kiro = await asyncio.create_subprocess_exec(
        "kiro-cli", "acp", "--agent", "kirocrew",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=10 * 1024 * 1024,
    )
    req_id = 0
    buffered = []

    async def send(method, params):
        nonlocal req_id; req_id += 1
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        kiro.stdin.write((json.dumps(msg) + "\n").encode())
        await kiro.stdin.drain()
        return req_id

    async def wait_response(rid, timeout=120):
        """Wait for response, buffer notifications."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = await asyncio.wait_for(kiro.stdout.readline(), timeout=3)
                if not line.strip(): continue
                msg = json.loads(line)
                if msg.get("method") and msg.get("id") is None:
                    buffered.append(msg)  # notification
                    continue
                if msg.get("id") == rid:
                    return msg.get("result", {})
            except (asyncio.TimeoutError, json.JSONDecodeError):
                continue
        return {}

    # Step through protocol, log everything
    t0 = time.time()
    await wait_response(await send("initialize", {
        "protocolVersion": "2024-11-05",
        "clientInfo": {"name": "kirocrew", "version": "0.1.0"},
    }))
    await wait_response(await send("session/new", {"cwd": "/tmp", "mcpServers": []}))

    # Check what was buffered during handshake
    for msg in buffered:
        method = msg.get("method", "")
        name = msg.get("params", {}).get("serverName", "")
        print(f"  [{time.time()-t0:.1f}s] {method} name={name}")

    kiro.kill()

asyncio.run(main())
```

### Example: Build Pipeline Testing

```bash
# Reproduce: run build N times, check for flaky failures
pip install -e . && pip install -e . && pip install -e .

# Diagnose: find stale cached files
find build/ -name "SOURCES.txt" -exec grep "basePickBy" {} +

# Verify fix: same sequence must pass consistently
rm -rf build/ && pip install -e . && pip install -e . && pip install -e .
```

### Key Principles

- **Observe before fixing** — capture raw data, don't guess
- **Reproduce reliably** — if you can't trigger it on demand, you can't verify the fix
- **Test the exact flow** — simulate what the real code does (same process, same protocol, same ordering)
- **Verify N times** — flaky issues need multiple runs to confirm (3+ consecutive passes)
- **Keep test scripts** — save in `/tmp/test_*.py` during debugging, discard after fix is verified
