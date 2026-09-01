# Verification recipes

How to verify a change against a **live isolated instance** instead of reasoning
about it. The verification controller is three pieces, and none of them is new
machinery you have to stand up:

- **the pod CLI** — [`kirocrew pod`](../../src/kiro_crew/pod/README.md) boots the
  worktree's own full stack on its own port and its own `KIROCREW_HOME`.
  `pod scenarios` lists the states it can be born with, `pod up --seed <name>`
  boots one of them, and `pod api` makes one authenticated call without you
  touching a credential.
- **the feature map** — [`docs/feature-map/`](../feature-map/README.md) answers
  "which page and which handler own this?", so a recipe below can be pointed at
  the right surface by lookup rather than by search.
- **these recipes** — the seven things that are worth doing against a pod and are
  not obvious from either of the above.

Each recipe names the seam it uses. When a seam has a precedence rule or a
credential boundary, that is in the caveats — those are the parts that make a
recipe silently verify nothing.

## 1. A pod whose agent is deterministic

**When:** the change is on an agent path (a turn, a tool card, an approval, a
stop) and you need the same answer every run, offline, with no model spend.

`kiro_crew.testing.fake_acp_backend` speaks the ACP subset `AcpClient` drives and
switches behavior on bracket markers in the prompt: `[[TOOL]]` (tool call +
update), `[[PERMISSION]]` (fire-and-forget permission request), `[[GATED]]`
(waits for the host's answer and reflects a reject as `status: "failed"`),
`[[SLOW]]` (cancellable slow stream), `[[SLOW_NOACK]]` (deaf to cancel — the
"Stop Failed, Session Reset" path), `[[SLOW_LATEACK]]`, `[[ERROR]]`,
`[[MAXTOKENS]]` and `[[REFUSAL]]`. A plain prompt streams one canned chunk.

`resolve_kiro_cli` in `src/kiro_crew/kiro_cli.py` takes `KIROCREW_KIRO_BIN` as its
first candidate; the pod's boot env puts the worktree's `.venv/bin` first on
`PATH` (`src/kiro_crew/pod/runtime.py`). So a launcher in that directory is the
in-pod seam:

```bash
WT=/path/to/worktree                       # .venv is gitignored, so this stays untracked
cat > "$WT/.venv/bin/kiro-cli" <<'SH'
#!/bin/sh
exec python -m kiro_crew.testing.fake_acp_backend "$@"
SH
chmod +x "$WT/.venv/bin/kiro-cli"

# If a real CLI is installed in a fixed dir, the launcher LOSES (see caveats) —
# pin the override on the user manager instead, before the pod boots.
systemctl --user set-environment KIROCREW_KIRO_BIN="$WT/.venv/bin/kiro-cli"
kirocrew pod up my-wt --seed minimal
systemctl --user unset-environment KIROCREW_KIRO_BIN
```

**Caveats.** `known_kiro_cli_dirs` searches `~/.local/bin` and `~/.cargo/bin`
*before* the inherited `PATH`, so on a machine that has a real `kiro-cli` in
either one the `.venv/bin` launcher is never reached — check with
`ls -l ~/.local/bin/kiro-cli ~/.cargo/bin/kiro-cli` before trusting the PATH
route. The override cannot ride in on your shell: the pod unit starts from a
clean environment and `boot` reads only `CHECKOUT`, `SEED`, `APPROVAL` and
`CRONS` out of `~/.kiro/crew/pods/<name>.env`, which is why the manager-level
`set-environment` is the injection point — and why it must be unset again, since
it applies to every user unit started after it. `[[PERMISSION]]` and `[[GATED]]`
need something that can resolve an approval modal, so they belong to a Playwright
run, not a headless one.

## 2. Drive a conversation over session-control

**When:** the change is in turn handling, queueing, stop, or transcript
persistence, and you need a real conversation advanced step by step.

The five routes are registered in `src/kiro_crew/dashboard/server.py` and handled
in `src/kiro_crew/dashboard/handlers/session_control.py`; the operations live in
`src/kiro_crew/dashboard/session_control.py`. They are **strict-internal**:
`X-Internal-Secret` authenticates, and `X-Session-Key` is the caller's own
session key, which every verb authorizes against.

| Route | Request | Response |
|---|---|---|
| `POST /api/session-control/create` | `{title?, agent?, folder_id?}` | `{ok, target, title}` |
| `POST /api/session-control/send` | `{target, message}` | `{ok, target, started}` |
| `GET /api/session-control/read` | query `target`, `limit` (1–100, default 20), `since` | `{ok, target, title, running, streaming?, queue_depth, total, next_since, messages[]}` |
| `POST /api/session-control/stop` | `{target}` | `{ok, target, info, …}` |
| `POST /api/session-control/close` | `{target}` | `{ok, target}` |

Each `messages[]` row is `{index, role, content, ts, truncated?}`. The loop:

```
create                       -> keep `target`
send  {target, message}      -> `started` says the turn was accepted
read  ?target=…              -> keep `next_since`
read  ?target=…&since=<next_since>   # repeat until the turn is done
stop  {target}   (only to cut a turn short)
close {target}   (archive the session)
```

**A turn is complete when `read` returns `running: false`, `queue_depth: 0` and
no `streaming` key.** All three matter. `running` is deliberately wider than the
slot's own flag — it stays true between the stages of a multi-stage plan — and
`streaming` appears while rows exist that the cursor does not cover yet, so "no
new messages" alone is not completion. `queue_depth` is the one that bites: a
`send` against a busy target is QUEUED rather than started, so a turn that never
began reads as `running: false` with nothing streaming, and the `close` below
would archive the slot and take the queued message with it. Answer a tool approval the turn is waiting on with
`GET /api/approvals` then `POST /api/approvals/{id}/{action}`, action one of
`approve`, `reject`, `reject_once` (`src/kiro_crew/dashboard/handlers/sessions.py`).

**Caveats.** `pod api` cannot reach these routes: it sends only
`Authorization: Bearer <token>`, so the handler's internal-auth gate answers 403
`internal_secret_required`. Drive them from a caller that already holds the
internal secret — the MCP tool path in `src/kiro_crew/mcp_core.py` is the
reference — rather than by extracting one. The surface is behind
`agent.session_control`; with it off every verb refuses 400
`session_control_disabled`, and a config read that raises also resolves to
disabled. A `since` below the trimmed prefix or past the end of a rewound
transcript refuses 409 `cursor_unavailable`, and the recovery is a tail read with
no `since`. **Every `send` is a real model turn** unless recipe 1 is in place.

## 3. One-shot WebSocket probe

**When:** you changed something that pushes over the dashboard socket and want to
prove frames actually arrive, without a browser.

```python
import asyncio, json, websockets  # pip install websockets

async def main(base: str, token: str) -> None:
    url = f"{base.replace('http', 'ws', 1)}/api/ws?token={token}"
    async with websockets.connect(url) as ws:
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        assert frame.get("type") == "slots", frame
        print("first frame:", frame["type"], "slots:", len(frame.get("data") or []))

# base_url + token from `kirocrew pod up <wt> --json`; `kirocrew pod url <wt>`
# prints the base_url alone.
asyncio.run(main("http://127.0.0.1:7958", "…"))
```

`GET /api/ws` sends `{"type": "slots", "data": …}` as its first frame
(`src/kiro_crew/dashboard/ws.py`), so one recv is enough to prove the socket is
live and authenticated.

**Caveats.** The upgrade is origin-checked. `check_origin`
(`src/kiro_crew/dashboard/origin.py`) trusts a loopback client that sends **no**
`Origin` header, which is what this script is — but if you add one it must equal
the request's `Host`, or the handshake is refused 403. The socket has a 30s
heartbeat, so a probe that idles longer needs to answer pings (the `websockets`
client does this for you).

## 4. Loop a workload's own state up to scale

**When:** you need many rows to see a cap, a paginator, a filter, or a render
cost — more than a seed scenario ships.

**There is no generic multiplier.** Each entity has its own route, its own body
and its own cap, so read that handler first and write the loop for *your* entity.
Chat folders (`src/kiro_crew/dashboard/chat_folders.py`) as the worked example:

```bash
# POST /api/chat/folders  {"name": …}  -> 201 with the folder object (incl. `id`)
for i in $(seq 1 40); do
  kirocrew pod api my-wt POST chat/folders --data "{\"name\":\"soak-$i\"}" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], (d["body"] or {}).get("id") or d["body"])'
done

kirocrew pod api my-wt GET chat/folders   # confirm the count the UI will render
```

`pod api` prints one JSON document with fixed keys — `{name, method, path,
status, ok, body}` — on every outcome, and exits 1 on a non-2xx, which is what
makes a loop like this readable without branching on output shape.

**Caveats.** Folders cap at 500 (429 `folder_cap_reached`) and a non-dashboard
caller is additionally rate-limited to 10 creations per window (429
`create_rate_limited`) — a loop that gets faster than the limiter will report
429s that are the limiter working, not the feature failing. Both numbers are
this entity's; another entity has different ones or none.

## 5. Soak the e2e suite

**When:** a failure is intermittent and one green run proves nothing.

```bash
E2E=<app-skills-dir>/pod-e2e/scripts/pod-e2e.sh
ART="$HOME/.kirocrew-pods/.e2e-artifacts/my-wt"
RUNS="$(mktemp -d)/runs"; mkdir -p "$RUNS"

for i in $(seq 1 10); do
  bash "$E2E" my-wt; echo "run $i exit=$?"
  cp "$ART/verdict.jsonl" "$RUNS/$i.jsonl"     # BEFORE the next run truncates it
done

grep -h '"status": "fail"' "$RUNS"/*.jsonl | sort | uniq -c | sort -rn
```

The script's exit code is the **number of failed phases**, so `exit=0` is the
only green.

> **`verdict.jsonl` is truncated at the start of every run** — including a run
> that skips the frontend phase or dies before the driver starts. Copy it out
> inside the loop body, as above. A soak that archives after the loop keeps one
> file: the last run's.

**Caveats.** The rest of the artifact directory *persists* across runs, so a
screenshot from run 3 is still sitting there during run 7 — check timestamps
before reading one as evidence. The pod is torn down and rebuilt each iteration
unless it was already up, which is what makes the runs independent; `--keep`
trades that independence for speed.

## 6. Accessibility scan inside a Playwright spec

**When:** the change adds or reworks a surface and you want an axe pass on the
real rendered page.

The e2e runner exec's the spec named by `PLAYWRIGHT_SPEC=` in the worktree's
`.pod-test.sh`, with `page`, `context`, `base_url`, `token`, `artifact_dir`,
`expect`, `expect_true` and `record` already in scope — no imports needed:

```python
# .pod-e2e/a11y.spec.py — PLAYWRIGHT_SPEC in .pod-test.sh points here
import json, pathlib

# The runner exec's this file, so module globals like `__file__` do not exist —
# name the vendored asset by an absolute path instead. `WORKTREE` is the one line
# to edit when the branch moves.
WORKTREE = "/workplace/<you>/kirocrew-wt-<name>"
AXE = pathlib.Path(WORKTREE) / ".pod-e2e" / "axe.min.js"

page.goto(f"{base_url}/chat?token={token}")
page.wait_for_selector("main")
page.add_script_tag(content=AXE.read_text(encoding="utf-8"))
result = page.evaluate("async () => await axe.run(document, "
                       "{runOnly: ['wcag2a', 'wcag2aa']})")
serious = [v for v in result["violations"] if v["impact"] in ("serious", "critical")]
(pathlib.Path(artifact_dir) / "axe.json").write_text(json.dumps(result["violations"], indent=2))
page.screenshot(path=f"{artifact_dir}/a11y.png")
record("axe-serious", not serious, "; ".join(f"{v['id']}x{len(v['nodes'])}" for v in serious))
expect_true(not serious, f"{len(serious)} serious/critical axe violations")
```

`record(name, ok, detail)` appends to `verdict.jsonl` immediately, and an
`ok=False` row **fails the run** rather than leaving a note — so the assertion is
recorded even if a later step stalls.

**Caveats.** This is deliberately **spec-local**: axe is vendored beside the spec
on the branch that needs it, not wired into a repo-wide gate, so it scans the one
surface you changed and no baseline has to be maintained. `axe.min.js` and the
spec are test assets of that branch — commit them, because the worktree has to
end clean (`git status --porcelain` empty). The frontend phase runs under
`POD_E2E_PW_TIMEOUT` (default 600s) and needs `KIROCREW_PW_PY` pointing at a
Playwright interpreter; without it the phase fails rather than skipping.

## 7. Ask how long boot took

**When:** you want the boot cost of a change. There is already a metric and an
endpoint — do not add a stopwatch.

`record_boot_to_ready` in `src/kiro_crew/metrics/http_metrics.py` emits the
`kirocrew.gateway.boot.duration` histogram in ms, with low-cardinality attrs
`server` (`dashboard` or `api`) and `outcome` (`ready`).

```bash
kirocrew pod api my-wt GET telemetry/startup | python3 -c '
import json, sys
d = json.load(sys.stdin)["body"]
print("telemetry enabled:", d["enabled"], "shards:", d["shard_count"])
for m in d["other"]:
    if m["name"] == "kirocrew.gateway.boot.duration":
        print(m["kind"], "count", m["count"],
              "p50", m["p50_ms"], "p90", m["p90_ms"], "max", m["max_ms"])
'
```

`GET /api/telemetry/startup`
(`src/kiro_crew/dashboard/handlers/telemetry.py`) returns `enabled`,
`env_pinned`, `env_var`, `window_days`, `metrics_dir`, `shard_count`, plus the
`startup`, `turn`, `context` and `cost` blocks and a generic `other` list. The
boot histogram lands in `other`, as `{name, kind: "histogram", count, mean_ms,
p50_ms, p90_ms, min_ms, max_ms, other_generations, total_count}` — and `splits`
when the same metric was emitted under several attribute sets.

**Caveats.** `other` is populated from the OTEL metric shards, so a pod with
telemetry off or a `shard_count` of 0 answers with the boot metric simply absent
— read `enabled` and `shard_count` before reading an empty list as a regression.
Selection into `other` is by data-point shape, not by name. `other_generations`
and `total_count` disclose a mixed window: when they are non-zero the quantiles
describe the dominant generation, not every sample.
