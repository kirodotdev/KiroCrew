# Profiling, security, eval and governance commands

Moved verbatim from the kirocrew-commands `SKILL.md` core.

## Profiling (debug-only)

Off unless `KIROCREW_DEBUG=1` is set; the CLI is the only entry point. Emits folded
stacks (open in speedscope / flamegraph.pl). See `docs/architecture/design-notes/profiling.md`.

| Command | Description |
|---------|-------------|
| `KIROCREW_DEBUG=1 kirocrew perf sample --call mod:fn` | Profile that callable in-process (no extra dependency) |
| `KIROCREW_DEBUG=1 kirocrew perf sample` | Attach to the running gateway (needs `pip install "kirocrew[perf]"`) |
| `KIROCREW_DEBUG=1 kirocrew perf sample --pid 1234 --seconds 30` | Attach to a specific PID for N seconds (1-300) |
| `... --interval 0.002` | Seconds between samples (0.001-1.0, default 0.005) |
| `... --output /tmp/p.folded` | Where to write the profile (default `./kirocrew-profile.folded`) |
| `KIROCREW_DEBUG=1 kirocrew desktop metrics` | Per-process CPU/memory of the **Electron** app (`--json`, `--top N`, `--path`) |

On macOS the attach path additionally needs elevated privileges (the OS denies
`task_for_pid`), so it may require sudo; `--call` needs neither py-spy nor sudo.

`desktop metrics` reads a recording rather than querying the app: `getAppMetrics()`
is Electron-main-only, so the app samples itself into an artifact when **started**
with `KIROCREW_DEBUG` set. Setting the variable only for the CLI does not make an
already-running app record -- restart it.

## Security & Eval

| Command | Description |
|---------|-------------|
| `kirocrew secrets import` | Dry-run the migration of plaintext `.env` credentials into the encrypted vault |
| `kirocrew secrets import --apply` | Store the secrets and rewrite `.env` to `secret://` refs |
| `kirocrew secrets authorize NAME https://api.example.com` | Authorize a Custom secret for one exact https origin, so agent write requests may carry it (`--header` sets the header, default `Authorization: Bearer`) |
| `kirocrew secrets authorize NAME --remove` | Remove that authorization. `--reset` discards a policy that does not parse or verify; without it, `authorize` refuses rather than erase entries |
| `kirocrew telemetry status` | Show whether local metric recording is on, and exactly what the anonymous beacon sends and whether it will |
| `kirocrew telemetry disable` | Turn local metric recording and the anonymous beacon off permanently |
| `kirocrew telemetry enable` | Turn local metric recording and the anonymous beacon back on |
| `kirocrew security audit` | Scan conversation history for suspicious tool usage |
| `kirocrew security deny-list` | Show active deny patterns |
| `kirocrew security events` | Show recent security event log entries (last 20) |
| `kirocrew security events -n 50` | Show N entries |
| `kirocrew security verify` | Verify security event log HMAC integrity |
| `kirocrew eval` | Run smoke test evaluation (~30s) |
| `kirocrew eval memory_recall_basic` | Run specific scenario by name |
| `kirocrew eval --all` | Run all scenarios (slow) |
| `kirocrew eval --judge` | Enable LLM judge scoring |
| `kirocrew bench list` | Show the available corpora and what is cached |
| `kirocrew bench fetch CORPUS` | Download a corpus into the local cache and verify its checksum |
| `kirocrew bench retrieval` | Measure retrieval recall/nDCG against a corpus (deterministic) |
| `kirocrew bench kb-retrieval` | Measure Knowledge Library recall/MRR/nDCG against a golden set (deterministic) |
| `kirocrew bench lesson-recall` | Measure lesson ranking recall/MRR/nDCG against a golden set (deterministic) |
| `kirocrew bench session-recall` | Measure session search recall/MRR against a golden set (deterministic) |
| `kirocrew bench compare A B` | Diff two saved JSON reports; refuses to attribute a delta when the runs disagree on corpus |
| `kirocrew acceptance-check --json CHECK REF` | Answer a goal acceptance check that an installed edition provides; prints the report as one JSON object (`CHECK` names the edition's check, `REF` the item it is about) |

## Governance Policy

Inspects the two-level security model (`effective = POLICY ∩ PROFILE`,
tightest-wins). Six verbs: five are read-only, and `fetch` writes — it applies the
central policy when the download is usable. The enterprise ceiling is never
hand-edited through the CLI (its files are keystone-fenced so the agent cannot read
or write them).

| Command | Description |
|---------|-------------|
| `kirocrew policy show` | Show the effective enterprise security policy |
| `kirocrew policy show --ids` | List each denied-command category's rule ids (default: counts only) |
| `kirocrew policy validate` | Load-check the policy + all profiles |
| `kirocrew policy explain SCOPE ITEM` | Explain one tool/scope decision for a surface |
| `kirocrew policy explain SCOPE ITEM --session-key K --agent A --app APP` | Scope the explanation to a surface |
| `kirocrew policy explain SCOPE ITEM --json` | Print the explanation as one JSON object |
| `kirocrew policy explain --batch FILE` | Explain each JSON Lines request in FILE (`-` = stdin) in one run, one JSON answer per line |
| `kirocrew policy profile NAME` | Show a profile by name |
| `kirocrew policy source` | Show whether this host fetches its policy from a central source |
| `kirocrew policy fetch` | Fetch the central policy now and apply it if usable (`--force` re-downloads, ignoring cached validators) |
