# Changelog

All notable changes to KiroCrew are documented in this file.

## [Unreleased]

- **xlsx files now render inline in the file viewer** instead of a
  download-only card. A new `GET /api/file-sheet` endpoint parses OOXML
  workbooks server-side with openpyxl (read-only, worker thread, ZIP
  magic-byte check, 500x100 per-sheet cap with explicit truncation flags) and
  the new `SheetViewer` renders sheet tabs, a column-letter header, and a
  row-number gutter. Formula cells with no cached value — the shape of every
  agent-generated workbook — show the formula source rather than an empty
  cell. Legacy `.xls` and ODF formats keep the download card, and any parse
  failure degrades to it. (#3865)

- **A session that times out on startup now names the MCP server it was waiting
  for.** Every cause of a stalled session start reported the same sentence,
  `Request session/new timed out`, which named none of them: a slow MCP fleet, a
  single unreachable remote server, a pending authorization, and an expired
  credential were indistinguishable. The runtime already held both halves of the
  answer at that moment and threw them away. It sends the server roster in the
  request's own `mcpServers` array, and the reader loop stages every
  `server_initialized` / `server_init_failure` / `oauth_request` frame that
  arrives before the response, each carrying its server's name. A session-start
  timeout now reads both and reports the difference, so the error says how many
  servers reported out of how many were expected, which ones never reported,
  which failed and why, and which are waiting on authorization. `session/load`
  shares the budget and the staging, so it gets the same report. A failed
  server's error text takes the same redaction the dashboard banner applies,
  because a server's startup error can carry its own connection string, and each
  listed name is redacted, collapsed onto one line and length-capped as well --
  a name is config-derived, so an installed app chooses it, and an embedded
  newline would otherwise forge a line in the gateway log while an unbounded one
  would defeat the cap on how many names are listed. The reported count is taken
  against the roster rather than against every staged frame, since the agent
  spec's own servers report too and counting them produced impossible readings
  like "2/1 reported". Timeouts now raise a dedicated `AcpRequestTimeout`, a subclass of
  `AcpRuntimeError`, so a stall is distinguishable from a protocol fault without
  changing what existing handlers catch. Startup telemetry also stops losing the
  starts that failed: the `session_new` phase duration is recorded in a `finally`
  like `session_load` already was, instead of only on success.

- **Subagent rows in System → Sessions now report their process and MCP-stub
  counts.** Both columns rendered an em dash on every task row, which read as
  "a subagent carries no MCP stubs" — the opposite of the truth: a subagent
  session spawns its own poolable stub set and reaches the shared backends
  through the same gateway daemon a top-level session does (measured on a live
  host: 18 `--poolable` stubs under one shared runtime, none of them falling
  back to a private spawn). The reason was structural, not cosmetic: nothing
  ever counted them. `task_memory_rows()` carried no `procs`/`mcp` fields at
  all, and the reaper sweep that samples a task's RSS and CPU took no such
  reading, so the frontend hardcoded both to null. The sweep now counts the
  run's subtree in the same pass (one walk, no extra syscalls per column) and
  attributes the counts the way RSS and CPU are already attributed — split
  across the co-tenants of a shared runtime — with two rules a count needs and
  a byte figure does not: the quotient rounds to a whole process, and a nonzero
  total never rounds down to zero. Unmeasured stays null rather than becoming
  "0", so an unsampled task still reads as an em dash instead of claiming to be
  empty. The stub cmdline fingerprint is now one constant shared by the
  rewriter that emits the launch line and both counters that match it, pinned
  by a test: a private copy that drifted would not fail loudly, it would report
  zero stubs.

- **The subagent cost sweep no longer runs on the gateway event loop.**
  ``_sample_live_costs`` walks ``/proc`` several times per live agent, and the
  reaper called it inline, so every chat turn and heartbeat waited behind those
  walks. It now runs on the maintenance executor. The body was made
  thread-safe to go with it: the agent registry is snapshotted once and the
  sharer count is derived from that snapshot, because iterating the live
  registry from a worker thread raises ``RuntimeError`` the moment a spawn or
  eviction lands mid-sweep.

- **A delivered message no longer warns that it may not have been delivered.**
  Every message typed in the dashboard composer grew a "Message not confirmed —
  may not have been delivered" warning 30 seconds after it was sent, for the rest
  of the turn, while the agent was visibly answering it. The indicator's
  "delivered" signal was a `chat_message` echo of the user row — but that echo is
  suppressed for dashboard sends by design (`DashboardState.append` defaults
  `broadcast_user=False` precisely BECAUSE the composer already rendered the
  bubble; only a row replayed from a channel transcript opts in). So the
  pending-confirmation flag survived every composer send, the 30s sweep flagged
  all of them, and the flag cleared only as a side effect of the end-of-turn
  transcript refresh. Both composer surfaces now confirm from the send's own HTTP
  response, which is the actual delivery receipt: an accepted immediate dispatch
  retires the pending state and keeps the correlation id so a later echo still
  reconciles in place instead of pushing a duplicate bubble. A `queued`
  acceptance is deliberately NOT a receipt for that bubble -- the busy branch
  queues only a non-empty message yet answers `{ok, queued}` either way, and when
  it does queue, its own `queue_push` card owns the message, so cancelling it
  leaves the bubble behind. The genuine failure paths are untouched -- a rejected
  response, a transport error and the 10s client-side abort all leave the bubble
  unconfirmed, which is what the indicator exists to say.

- **Video and audio files now play inline in the file viewer.** Opening
  `.mp4`, `.webm`, `.mp3`, `.wav` and friends previously fell through to the
  code renderer, which displayed the binary as mojibake. A new
  `GET /api/file-stream` endpoint serves media with HTTP Range support
  (seeking needs 206 Partial Content) through the same security envelope as
  the other file endpoints -- path validation, sensitive-path block,
  symlink-refusing open, content sniffing so the served bytes decide the
  type, and bounded chunked reads so memory stays constant regardless of
  file size. The viewer routes video to an inline `<video>` player and audio
  to an `<audio>` bar; any playback failure degrades to the download card.
  (#4021)

- **A succeeded publish no longer renders as a blank error, and a failed
  re-publish no longer renders as a success.** The Publish panel recognized only
  the deploy-shaped `{url}` response, so a provider that hands its confirmed
  publish to `POST /api/artifacts/{slug}/publish` — the supported way to reuse the
  core's single publish authorization and audit trail rather than growing a second
  one — got its serialized-artifact response read as "no url", fell through to
  `{url: ''}` and rendered the ERROR branch with an undefined message: a bare red
  icon, no text, on a publish that had in fact succeeded (the bytes were pushed
  and `publication` was persisted). `readPublishOutcome` now reads both shapes and
  returns an outcome rather than a url: success is signalled by the return shape
  instead of inferred from a non-empty url (a destination can publish and expose
  no browsable link), an `error` field wins over anything else in the same body,
  `publication: null` is not success, and an unrecognized shape is reported as a
  NAMED error instead of an empty one. The mirror-image lie is fixed too — a 200
  whose `publication.last_error` is non-empty is now reported as that error rather
  than as "Published!", because `publish_sync.publish()` treats the version push
  as best-effort on a re-publish: it persists the failure and returns normally, so
  the remote content is stale behind a 200. The public-exposure warning and its
  blocking acknowledgment are unchanged and still unconditional.

- **Every builtin app now starts its content 8px from a phone screen edge, not 24px.**
  The narrow-first page gutter (`px-2 md:px-6`) reached the core pages and Issue
  Radar, while the remaining builtin apps kept an unconditional `px-6`, so their
  content was inset 24px before any card inset stacked on top. Meetings, Code
  Review Sage, Auto Research, Workflows, Mochi, Papyrus, PPTX Maker and Ops
  Mission Control now carry the same gutter, converted a whole file at a time so
  a header and the rows beneath it cannot render on two different left edges.
  Centered empty states keep their `px-6`, where it is the element's only inset
  and flushing it would push centered copy toward the edge for no width gain; a
  guard test states that exclusion so a later pass does not read it as a miss.

- **Destructive buttons look destructive on a phone.** `Btn`'s `danger`
  variant coloured its label only on `:hover`, and a touch viewport never
  produces `hover` — so a destructive button rendered identically to the
  non-destructive buttons beside it. Same class of defect as a hover-revealed
  control: the affordance existed only under a pointer. Found on the Channels
  page at 390px, where `Close` (which dismisses every agent in the channel) sat
  in a wrapped header row beside the frequent `3 agents` and `Clear Context`
  buttons at identical visual weight. The label is now `text-danger`
  unconditionally; hover still raises the border and adds a subtle fill, so the
  pointer affordance is not lost, only made unnecessary for recognising the
  control. (#3937)

- **A parent agent can read its own sub-agent's result file again on a host
  whose home is a symlink.** The result path handed back in a completion event
  was built through `Path.resolve()`, which is correct for the traversal check
  it exists to serve and wrong for a path somebody is told to go read: on an
  Amazon cloud desktop, where `/home/<user>` is a symlink to
  `/local/home/<user>`, that resolved spelling carries a `/local/home/...`
  prefix the reader's own path allowlist -- keyed on the `$HOME` it was given --
  does not match. The file was always readable; only the spelling was
  unrecognized. So the read was refused, and refused as an approval prompt that
  times out rather than as an error, which made a whole wave of sub-agent
  results look unreadable while the parent concluded it lacked permission.
  Paths emitted as TEXT (the completion event, the batch digest, `spawn_status`,
  the prior-result hint on an unresumable conversation, and `info.result_path`
  wherever it surfaces) now carry the declared home spelling, while every path
  used to open a file stays symlink-resolved so the traversal check is unchanged.
  Validation is delegated to the resolving helper rather than duplicated, so the
  two cannot drift apart.
- **Code Review Sage's "Ask the reviewer" no longer dies with the review's
  session.** The reviewer's reasoning lives in the session that produced the
  findings, and that session was kept resident only briefly — a 1800s idle TTL,
  a 6h cap, a 4-session LRU cap, and gateway restart. Worse than "unloaded":
  retiring it called `handle.destroy()`, which unlinks the kiro-cli transcript
  unless `keep_transcript` is set, so the reasoning was DELETED and no amount of
  waiting or re-opening could bring it back. Sage now keeps the transcript and
  a follow-up RESUMES it as an ordinary chat session (`session/load`), filed in
  a `Sage Review` folder and titled `followup-pr#<n>-<title>`. Nothing is held
  resident between the review and the question: asking is the rare case, so a
  follow-up pays a cold load from disk instead of pinning the shared reviewer
  subprocess on the chance that someone asks. Because the follow-up is a normal
  session, its tool use now runs through the dashboard's approval pipeline,
  which sees real permission requests and can reject BEFORE execution — closing
  the documented limitation that Sage's own gate was post-hoc for
  spec-pre-approved tools. A resume that would not restore the review is refused
  rather than attempted: the dashboard's fallback for a failed resume is to
  replay Kiro Crew's conversation log, and a follow-up session has none, so a
  session opened anyway would answer confidently about a review it knows nothing
  about; the same reason a run that is still going is not offerable, since a
  second coverage pass can replace the findings a mid-run conversation was
  opened on. Follow-up offers are retired after two weeks with no activity,
  measured from the transcript's own mtime so a conversation still in use keeps
  its offer. Retiring an offer removes only Sage's own descriptor: the one
  session id available there comes from a file the reviewer can write, so
  deleting on that authority would let a prompt-injected review name any session
  on the machine and have this app remove it.

- **A long-running cron job's next tick is no longer dispatched up to 30s
  late.** A job is invisible to the scheduler's wake computation while it's
  executing (`_next_wake_secs` skips anything in `self._executing`), so for
  a job whose run takes most of its interval, the timer's last wake before
  completion — capped at the 30s poll interval — is what the next dispatch
  had to wait for, since finishing a job never re-armed the timer. Measured
  in the field on a 60s-interval job with a ~57s run: 20% of ticks landed
  ≥20s late, costing roughly double the job's own `interval - duration`
  idle-time floor. Job completion now re-arms the timer with its real
  next-due delay. The naive version of this fix is unsafe: a job's
  completion runs on its own task, not the timer's, so a blind
  `_arm_timer()` call racing the timer's own in-flight dispatch sweep
  (`_on_timer`, yielded at its worker-thread scan) would cancel that sweep
  mid-flight and drop any due jobs not yet spawned for that tick. A job
  completing in that narrow window now no-ops instead — `_on_timer`'s own
  tick already unconditionally re-arms once the sweep finishes, by which
  point the completed job is no longer `_executing`, so the corrected delay
  still gets picked up, just moments later rather than being computed
  twice.

- **Reload session: relaunch a session's agent process in place.** A live
  agent process mounts its MCP servers and builds its tool table once, at
  session-init time, so config that changes afterwards — a newly added MCP
  server, an env or agent-spec fix — never reaches an already-open session;
  the only remedies were restarting the whole gateway or abandoning the
  conversation for a new chat. The session actions menu now carries a
  "Reload session" item (disabled while a turn runs) backed by
  `POST /api/chat/slots/{slot}/reload`: it targets the slot's linked session
  key, applies the cancel-route app-isolation policy, refuses with 409 while
  a turn is in flight or sub-agent children are attached, tears the process
  down through the same chokepoint the agent/workspace switches use, appends
  a feed notice, and eagerly re-arms the resume spawn, so the relaunched
  process re-reads its agent spec and environment and re-initializes MCP
  servers via session/load with the conversation preserved. The busy check
  is evaluated atomically with the session pop, closing the check-then-reset
  race, and the notice kind is skipped by the last-real-message scans on
  both backend and frontend.

- **Removing a worktree in Dev Fleet no longer strands its pod's isolated
  HOME.** Reclamation was gated on the pod's unit still being ACTIVE, which the
  ordinary path never is: you stop the pod when testing ends and prune days
  later once the PR merges. So the delete path that reclaims the HOME was
  effectively never called, and every removal leaked a full isolated
  `KIROCREW_HOME` — dominated by a per-instance copy of the embedding model, so
  ~0.6 GB each — with the directory becoming unattributable the moment the
  worktree's env pin went away. Removal now reclaims the HOME whether or not the
  pod is running, using the same `orphan_homes` predicate as `pod ls` / `pod
  prune` (so symlinks are skipped, and a macOS name mid-`up` is treated as
  installed rather than orphaned). Attribution and teardown are ONE transaction
  held under `pod_name_mutex`: pod identities are global basenames, so checking
  ownership in one process and tearing down in another leaves a window where a
  concurrent `pod up` from a different checkout claims the same name and the
  teardown would stop that pod and delete its HOME. Both call sites — the
  live-unit path and the orphaned-HOME path — go through the one locked helper,
  which is necessarily in-process: the mutex is held per open-file-description
  and `stop_pod` re-acquires it, so holding it around a `pod down` shell-out
  would block the child being waited on. Because the delete needs positive
  attribution, an ABSENT checkout pin refuses rather than assuming ownership
  (`pod prune` still reclaims those), and a name handed to a new pod mid-teardown
  refuses the removal outright, since that pod may be running out of the very
  worktree about to be deleted.
  The two outcomes are reported separately (`stopped_pod` vs
  `reclaimed_pod_home`) rather than conflated into a shutdown that never
  happened. Liveness checks keep failing CLOSED — they guard against deleting a
  checkout under a live pod — while a reclamation that cannot run now degrades
  to a logged leftover instead of refusing the removal, and a provably absent
  pod backend logs the HOME it is leaving behind at WARNING with the verb that
  reclaims it, replacing a debug-level line that hid the residue entirely.

- **The one-time config migration no longer leaves a `.json.bak` orphan beside a
  config path it does not own.** `KiroCrewConfig.load()` copied the
  pre-migration config to `<path>.bak`, where `<path>` is whatever
  `config_path()` resolved to -- so every caller that redirects the loader at
  its own `tempfile` entry (tests and embedders do) silently accumulated one
  orphan per load, since the caller unlinks the path it created and never learns
  a sibling appeared. One dev host reached 72,327 such files in `/tmp`, 7% of a
  tmpfs inode budget whose exhaustion fails every process on the box. The copy
  is now gated on the config living in `config_dir()`, the one directory whose
  contents we own; the production backup is unchanged, and a copy that fails
  still aborts the migration save exactly as before, so a config we could not
  copy aside is not rewritten either. The name is also built by
  appending rather than `with_suffix(".json.bak")`, which REPLACED the final
  suffix and so renamed a non-`*.json` config instead of backing it up.


- **A knowledge source that errored during ingestion is no longer re-synced on
  every sweep.** `KnowledgeIngestion` marks failure in the `sync_status`
  **column**, but `SyncScheduler.sync_all`'s skip predicate read only the
  `sync_status` copy inside the properties JSON, which the ingestion path never
  sets. So an ingestion-errored source (bad credentials, deleted remote,
  unparseable content) was retried on every sweep forever, flooding logs with
  the same failing network call and giving the user no way to quiesce it short
  of deleting the source. `sync_all` now treats an `'error'` value in EITHER
  store as errored (legacy JSON-only rows are still skipped), and
  `_record_failure` writes the column alongside the properties copy it keeps for
  `consecutive_failures`.

- **`test_redaction_timing_scales_linearly` no longer fails CI
  intermittently** (observed "Redaction scaled super-linearly: 3.2x, limit
  3.0x" on an otherwise-healthy matcher). The test took ONE
  `perf_counter` sample per input size, so it billed itself for whatever
  the OS gave the core to the sibling pytest-xdist workers — and one
  unlucky reading of the SMALL input, the ratio's denominator, was enough
  to push it over the bound. It now measures with `time.thread_time()`
  (redaction is single-threaded pure-regex work, so per-thread CPU is its
  complete cost) and takes best-of-3 per size, since scheduler noise only
  ever adds and the minimum is the closest estimate of the true cost —
  the same two techniques `TestIsDeniedReDoSResistance` already uses for
  this class of assertion. The 3.0x bound is unchanged and detection is
  intact: a genuinely quadratic implementation still measures ~4.3x.

- **Opted-in MCP servers no longer silently fall back to unpooled backends.**
  On one live host, 988 degradations accrued in 15 hours with no signal: 79%
  were guaranteed-ENOENT pooled spawns of bare commands the gateway daemon's
  systemd PATH cannot resolve, and 20% were crash-loops of servers whose
  declared `env` the shared backend deliberately withholds
  (`mcp_gateway.forward_declared_env` off). The rewriter now resolves bare
  commands through the same augmented search path the MCP probe uses and
  refuses to emit a stub it can prove will degrade — such servers are left
  for the session to launch directly, with a warning naming the fix (absolute
  path / the forwarding knob). The fallback audit log gains the reader it
  never had: gatewayd's `stats` reply now carries per-server fallback counts
  for the last 24 h, and the log rotates at 1 MiB instead of growing without
  bound. (#3495)

- **`TestIsDeniedReDoSResistance::test_cpu_cost_is_immune_to_other_threads_where_process_time_is_not`
  no longer intermittently fails CI** (observed "process_time did not exceed
  thread_time under a 2-spinner burst"). The test's 5-sample loop required
  every single process-time/thread-time comparison to succeed, so one sample
  where a shared CI runner's scheduler didn't interleave the burst threads
  within the narrow measurement window failed the whole test even though the
  invariant it checks — that `_cpu_cost` doesn't see other threads' CPU —
  held on every other sample. Now tolerates a minority (≤1 of 5) of failed
  samples; a genuine break in `_cpu_cost` still fails every sample. (The
  companion flake in `test_mid_dotstar_chain_spam_stays_linear`, tracked in
  the same upstream issue kirodotdev/KiroCrew#3080, was independently fixed
  by #3692 while this PR was open — this change covers the one flaky
  assertion #3692 didn't touch.)

- **The instance token-mint timeout is now user-configurable.** The remote
  `kirocrew token` mint ran with a hardcoded 30s budget, so a user behind a
  slow ProxyCommand/jump host timed out in the mint step even when the ssh
  forward itself came up (the connect flow spawns two proxy-bound ssh
  children, and the mint is the second one). A new
  `instances.mint_timeout_secs` (unset by default: SSH 30s, SSM 90s; clamped
  to [10, 120]) now threads through the tunnel manager to both the SSH and
  SSM mint paths; an explicit value applies to both transports, including a
  value equal to either transport's default. (#3566)

- **A Teams answer no longer gets silently truncated by a rate-limited
  chunk.** The Bot Framework Connector API enforces per-bot rate limits and
  can return HTTP 429, but the Teams outbound send raised immediately with
  no retry, unlike the Discord/Telegram/Webex clients (which all absorb a
  single 429 honoring the server's back-off hint). A multi-chunk answer
  stops at its first failed chunk, so a throttled chunk dropped it and
  everything after it, with only a backend log line. Outbound sends now
  retry once on 429, honoring the Connector API's `Retry-After` header. (#3738)

- **Telegram slash commands (`/new`, `/compact`, `/model`, `/yolo`, `/link`,
  `/unlink`, `/stop`, `/help`, `/queue`, `/steer`) no longer silently break
  in a group or forum-topic chat — without executing a command addressed to
  a different bot in the same group.** Telegram's own clients append
  `@BotUsername` to a slash command in any chat with more than one
  participant/bot — standard client behavior triggered by registering a
  command menu, not something the bot's UI controls — but the command
  parser matched the raw token verbatim against alias sets defined without
  that suffix. In the forum-topic supergroups this integration explicitly
  supports, every command fell through to being sent to the LLM as ordinary
  chat text with no error. A trailing `@BotUsername` is now stripped before
  alias matching — but only when it names THIS bot: Telegram fans a command
  addressed to a different bot in the same group out to every bot present
  (Bot API convention is to ignore what isn't addressed to you), so
  stripping any mention unconditionally would let e.g. `/yolo@OtherBot on`
  match this bot's own alias and enable auto-approval here. The gateway now
  resolves its own username via `getMe` at startup and only strips a mention
  that matches it (case-insensitively); any other mention, or none resolved
  yet, is left attached and falls through as unrecognized. (#3734)

- **`agent.dangerously_skip_permissions` no longer treats a string value as an
  affirmative grant.** The config loader coerced this field with a bare
  `bool(...)`, so a plausible config shape like `"dangerously_skip_permissions":
  "false"` (any non-empty string is truthy in Python) silently activated the
  standing, unattended tool-auto-approve grant this key controls — every tool
  call gets auto-approved with no confirmation prompt — instead of the
  explicit disable the value said. Now requires a real boolean, matching
  every other boolean field in the loader; a non-bool value falls through to
  the next accepted spelling instead of being read as a grant. (#3730)

- **A session no longer risks two interleaved turns after a mid-turn reset.**
  `record_failure`'s circuit breaker calls `reset(key)` while the failing
  caller still nominally holds that session's turn semaphore; `reset` pops the
  session and tears it down without touching the semaphore. If a concurrent
  `get_or_create` for the same key registered a replacement session in that
  window, the original caller's later `release(key)` — a fresh lookup by key,
  not the specific session object it acquired — released the REPLACEMENT's
  semaphore instead, an over-release that could hand out a surplus permit and
  let a third message start a turn while a second was still in flight on the
  same live provider session. The per-session semaphore is now a
  `BoundedSemaphore`, so a stray release beyond its one permit raises instead
  of silently succeeding; `release()` catches that specific error and logs a
  warning rather than propagating into a caller's `finally`. (#3749)

- **Notes: a failed GitHub token Save/Clear in Settings no longer gets stuck
  disabled with no explanation.** Neither the Save/Clear button handlers nor
  the `savePat` action they call had any error handling, so a rejected
  request (an invalid token, a transient network error) left `busy` stuck
  `true` — the button permanently disabled — with neither the success
  confirmation nor any error shown, an unhandled promise rejection, and the
  only recovery being to close and reopen Settings. Failures are now caught
  and reported inline next to the button, styled like the sibling per-vault
  knowledge-toggle error state, and the button always recovers. A review
  pass caught a sibling with the same root cause: the vault Remove confirm
  button's `onForget` call still swallowed its failure into the shared
  `error` banner, which only renders in the main-editor branch and is
  invisible while Settings is open — the confirm bar also dismissed itself
  immediately, so nothing indicated the removal was even attempted. Remove
  now catches inline too, keeps the confirm bar up on failure so the user
  can retry without reopening it, and clears on success. (#3743)

- **A folder knowledge source added from the dashboard can now be started.**
  The row's `sync_status` was stored twice — as a table column and inside the
  properties JSON — and the create path wrote `pending_confirmation` only into
  the JSON, leaving the column at its `pending` default. The dashboard list
  reads the column, so a freshly-added `local_folder` / `obsidian_vault` source
  showed a Pause button instead of the Confirm button that starts the scan and
  sat at "pending · 0 items · never synced" forever (the workaround was Pause
  then Resume). Both insert paths (`add_source` and the auto-source path used
  by drop-folder and project-docs sources) now derive the column from the
  passed properties, and the store migration repairs already-divergent rows on
  open, so existing stuck sources become startable without the workaround. (#3701)

- **The Speech-to-Text settings page no longer offers to install Whisper when
  the provider is AWS Transcribe.** With `stt.provider = "transcribe"` the page
  showed an "Install Whisper" button (installing an engine Transcribe never
  uses, so Status stayed "Not installed" forever), listed a Python/Whisper
  prerequisite toolchain that is irrelevant to Transcribe, and rendered a
  Runtime row that could only ever read "Native" because the backend never
  serves `docker_mode`. The install button is now hidden for Transcribe, the
  prerequisite block surfaces the real requirement — installing the `voice`
  extra into the gateway's own interpreter, plus a restart hint — the backend
  refuses `POST /api/stt/install` for Transcribe instead of silently installing
  the wrong package, and the dead Runtime row is gone. Where no install channel
  can make the extra importable (frozen build, pip-less interpreter, PEP 668
  externally-managed python) the page shows an honest unsupported notice
  instead of a command that cannot succeed, and a missing ffmpeg — which
  Transcribe's availability check treats as optional even though browser
  recordings need it — is now flagged with its install command even while the
  status reads ready. (#3559)

- **`kirocrew` commands start up to ~0.8 s faster, and each MCP stdio server
  drops ~58 MB of resident memory.** `cli.py` imported its full 132-subcommand
  dispatch table at module scope — including the Slack gateway, the dashboard
  state module and (through it) numpy — so every CLI invocation and every
  long-lived MCP backend process (`mcp-core`, `mcp-cron`, `mcp-computer`) paid
  ~1.3 s and ~112 MB for subcommands that never run. The four heavy import
  statements now execute inside the one dispatch branch that uses each name,
  cutting a fresh `import kiro_crew.cli` to ~0.5 s / ~54 MB. Each command now
  pays only for the modules its own branch uses: the MCP stdio servers and
  most verbs save the full ~0.8 s / ~58 MB, while commands that dispatch into
  the deferred modules (e.g. `gateway`, `cron`) save the portion they don't
  touch. A ratchet test keeps the deferred modules out of module scope and
  verifies every deferred import still resolves. Behavior is unchanged: the
  entry point, the fail-closed security prelude, and all subcommand dispatch
  are untouched. (#3504)

- **A managed deployment can now withhold the external services the core offers
  unconditionally.**
  Three surfaces had no composition point. Two are installable-content registries
  — skill discovery (skills.sh) and MCP server discovery (the official MCP
  registry) — which fetch from the public internet and then offer to install what
  they return, but hardcoded their public provider at registration time. The third
  is cloud deployment: `kiro_crew/deploy/` provisions S3, CloudFront, IAM roles and
  a reaper Lambda in the operator's own account and carried no capability gate at
  all, so `capabilities.publish` (which bounds publish-provider destinations) did
  not reach it. Together that made "source installable code only from our own
  registry, and never provision cloud infrastructure" impossible to express without
  patching the core — a hard blocker for any deployment where third-party code must
  be reviewed first, or where provisioning is centrally controlled. A new
  `external_access` platform slot adds `admits_registry(kind, name, api_base)` and
  `admits_cloud_deployment(target)`. A refused registry is never registered, so it
  is absent rather than failing per request; a refused cloud deployment makes the
  deploy surface report itself disabled — so the UI hides the console instead of
  rendering one whose every button 403s — and refuses every mutating route, wrapped
  at registration so a new endpoint is gated by being listed rather than by
  remembering an in-handler check. Both decisions take the concrete target as well
  as a label, because a name is self-chosen while the URL or target determines
  where bytes go, so an allowlist stops admitting a provider that later repoints at
  a different host instead of letting it inherit trust from its name. Both outcomes
  are SEL-audited: a log carrying only denials cannot show whether the permitted
  path was ever taken. The public default admits everything, so an ordinary install
  is unchanged.

- **An MCP server that declares `env.PATH` no longer loses its inherited PATH.**
  A spec's `env` is applied per key, so naming one directory to add — a Node
  version manager's shim dir, say — replaced the child's PATH instead of
  extending it, leaving the server with only that one directory. A launcher that
  execs a sibling binary then died with "not found" for a binary that was
  plainly installed, while the dashboard probe — which merged rather than
  replaced — reported the same server healthy, so nothing in the UI
  distinguished it from a working server. The full effective PATH (the spec's
  own entries first, deduped) now backs the probe, command resolution, and the
  value written into the agent config, so "probes healthy" and "works in a
  session" can no longer disagree.

- **Every emitted MCP config surface now goes through one env normalization
  point (`env.emit_env`).** The agent config, the kiro-global entries the sync
  creates, and the Claude Code `~/.mcp.json` sidecar all expand a declared
  `env.PATH` the same way, so a server can no longer work under one consumer
  and die under another. The cosmetic `kiro-cli mcp add` subprocess inside the
  sync — an unsynchronized second writer whose output the rebuild overwrote —
  is removed, and the discover→write sequence is a single mutex-serialized
  entry point (`sync_discovered_servers`) shared by the sync endpoint, the
  restart pre-sync, and the config watcher, closing their read-modify-write
  race.

- **The Online badge now means "tools usable", dated.** A probe whose
  `initialize` succeeds but whose `tools/list` fails reports an error instead
  of `ok` with an empty list; every probe result carries `probedAt` so the
  dashboard can show when a status was established instead of presenting a
  cached one as current; and a managed server served from its in-process
  declaration is marked `declared` — the tool list is correct, but nothing
  verified the server can start — instead of rendering identically to a
  handshake-proven server.

- **Apply & Restart now really mounts a newly installed server, and says so
  honestly when it cannot.** The restart path runs the one serialized
  discover→write entry point and reconciles the consumed agent config
  unconditionally, so an edit that produces an empty discovery delta (a
  `disabled: true` flip, a changed `env`) is still written out instead of
  being skipped as "nothing new". A reconcile that FAILS is reported through
  `mcp_sync_ok` on the restart response rather than being dressed up as a
  successful apply.

- **Publishing an artifact to the public internet now requires an explicit
  acknowledgment, and an operator can remove the path entirely.** The warning
  next to each confirm button could be scrolled past and read as decoration, and
  the public-web destination was the one publish destination exempt from the
  operator's publish policy — `deploy-web-aws` was appended to
  `/api/publish-providers` unconditionally and `POST /api/deploy/deploy` consulted
  no ceiling, so a team that had closed every other destination still had a
  one-click path to a world-readable URL. Every surface that creates the public
  resource (the Publish panel, its scan-override branch, and **Confirm deploy** on
  a pending entry) now ends at a blocking dialog that names the artifact, states
  that anyone with the link can view it, states how long the link stays public,
  and requires pressing **I understand, publish publicly** — a button that is
  neither pre-focused nor the default action, so no keystroke that dismisses an
  ordinary dialog can publish by accident. The destination itself now goes through
  the same `capabilities.publish` chokepoint as artifact publish: closing it in the
  trust-root policy (or narrowing `publish.allowed_destinations` in `config.json`)
  removes the button from the provider registry **and** answers 403 from
  `/api/deploy/deploy` and `/api/deploy/pending/{id}/confirm`, including for the
  agent-mediated `deploy_artifact` preview. Operators who had already narrowed
  `publish.allowed_destinations` must add `deploy-web-aws` to keep deploying. (#3599)

- **The Linux desktop app no longer shows two title bars on GNOME-family
  Wayland desktops.** The window manager's native decoration used to stack on
  top of the dashboard's own 42px header, wasting vertical space and
  duplicating controls. On Wayland sessions of desktops that prefer
  client-side decorations (GNOME, Ubuntu, Unity, Pantheon, Budgie) the window
  now drops the native frame: the header doubles as the title bar via an
  injected drag region, and a minimize/maximize/close cluster is injected at
  the header's top-right (frameless Linux gets no OS-painted controls, unlike
  the macOS traffic lights and the Windows caption overlay). X11 sessions,
  desktops that expect server-side decorations (KDE, XFCE, tiling window
  managers — including hybrids like Regolith that also report a GNOME token),
  and unknown environments keep the native frame: frameless X11 windows lose
  mouse edge-resize, which would be worse than the doubled bar. The
  `linuxFrameless` key in the desktop app's own config (Connection → Open
  Config File, also in the tray menu; read once at launch) forces either
  shape. On frameless windows the menu bar auto-hides (press Alt to reveal
  it) — kept visible it would re-create the stacked-bars problem, removed it
  would take the menu away entirely. Connection windows follow the same
  decision. (#3606)

- **A lesson from a previous embedding-model generation could no longer get
  silently deleted or offered as a false contradiction.** `write_lesson`'s
  semantic dedup and `find_contradiction_candidates` compared raw embeddings
  with a cosine helper that silently truncated a dimension mismatch to the
  shorter vector instead of rejecting it, so a row embedded at a different
  dimensionality (e.g. left over from an old embedding model) could score a
  plausible-looking ~0.5 similarity against an unrelated new rule — landing
  either past the 0.85 dedup line (deleting the old lesson as a "duplicate")
  or inside the [0.4, 0.85) contradiction band (offered as a false
  contradiction candidate). Both paths now converge onto the same
  dimension-checked, float64-precision scorer the ranking paths already use,
  which also removes a per-row query re-derivation from both loops. (#3466)

- **Computer use no longer costs a 109 MB backend process per chat when it is
  off — or on platforms where it cannot run at all.** `kirocrew-computer` was
  registered into the agent spec unconditionally, and the keystone enable was
  only checked *inside* the process the spec had already caused kiro-cli to
  spawn: it suppressed the tool list, never the process. Every chat process paid
  ~109 MB for a disabled capability, including every `spawn_run` subagent, and
  on Linux/Windows it paid that for a feature with no driver (macOS is the only
  supported platform) — measured at 16 processes / 1.75 GB on one Linux host.
  The server is now withheld from the emitted spec, unless this is macOS *and*
  the keystone is on; enabling it from Settings rebuilds the spec before
  restarting sessions, so the tools still appear in the session you are sitting
  in. Your `tools` entries are left untouched — a ref whose server the spec does
  not define resolves to nothing, so a mount you had narrowed to a single tool
  comes back exactly as you left it. Only the entry's own `autoApprove` and
  custom `env` keys are reset by an off/on cycle and need re-applying,
  deliberately: restoring an approval from a file the agent can write would
  bypass the PreToolUse gate. The two in-process checks are kept as defence in
  depth for a mid-session disable. (#3482)

- **Folder-write audit lines now name the internal component that made the
  write, instead of inferring the caller's identity from the internal secret's
  presence.** Every MCP stdio server now declares its component name on
  loopback gateway requests (`X-Internal-Caller`, attached centrally by the
  shared request helpers), and the folder endpoints validate it against a
  known-caller set before trusting it into the security event log's `caller`
  field — `source` stays in SEL's interface vocabulary (`mcp`), so operator
  queries over `source == "mcp"` keep matching folder writes. The old
  inference was correct only while exactly one internal caller existed — a
  second internal caller would have silently inherited the same label. An
  authenticated internal write with a missing or unrecognized caller name is
  audited as `caller="unknown-internal"` with a warning, so a new caller shows
  up loudly until it is added to the known set alongside its own test. Browser
  writes still audit as `dashboard`; the caller header alone grants nothing.
  (#3503)

- **`kirocrew policy show` no longer hides the 139 built-in denied-command
  rules from the agent.** The rules are visible and configurable to the
  user (Settings → Security), but the agent's only way to discover them was
  to attempt a command and be refused — so it could plan multi-step work
  that turned out to be impossible from the first step, walking the user
  through setup effort (e.g. exporting AWS credentials) for a task a
  hard-denied command would block later anyway. `policy show` now prints
  the rule count grouped by category on every install, enterprise policy or
  not; `--ids` lists each category's rule ids for citing a specific rule
  when relaying a refusal. (#3454)

- **Side-panel oversize-question refusal now reports an accurate character
  target for every script, not just emoji.** The refusal derived its
  character count from a fixed worst-case floor (4 bytes/char, the emoji
  case), so an ASCII user over the byte budget was told to cut to ~8,192
  characters when trimming a single character would do (4x over-deletion),
  and a zh-CN user (3 bytes/char) was told 8,192 when ~10,922 actually fit.
  The target is now derived from the submitted question's own byte density,
  so it's accurate per script — the all-emoji case is unaffected (it already
  sat at the 4-byte floor). (#3432)

- **The skill browser no longer serves a different skill than the one you asked
  for.** Three `package/` lookups compared a bare leaf name and returned the
  first hit, so a request for `package/<name>` could answer with a file under
  `<root>/<Pkg>/<name>`, or with whichever of two identically named files the
  filesystem happened to yield. Exact keys now decide first, leaf matching
  survives only where it is unambiguous, and a real collision resolves to
  nothing — a 404, with the competing candidates logged — because the
  `package/<path>` key cannot express which of the two files was meant. Every
  lookup that previously resolved correctly still resolves to the same file.
  **Edition maintainers:** roots the core already keys itself (`~/.kiro/skills`,
  the data home, configured extra paths) are no longer *also* enumerated under
  `package/`, which previously presented an editable skill as a read-only
  package one. A stored reference to one of those duplicate `package/` keys
  stops resolving; the file itself is untouched and still reachable under its
  canonical key, but the stored reference has to be re-pointed. (#3369)

- **MCP gateway daemons no longer leak when their launcher dies.** A `gatewayd`
  whose launcher exited without signalling it (a torn-down `pytest` run, for
  example) used to stay resident forever — invisible to every sweep, ~27 MB
  each, accumulating without bound. The daemon now watches its own listening
  socket path and gracefully self-exits once the path is gone (three
  consecutive checks, POSIX only), and the untracked-orphan sweep reaps any
  gatewayd whose `--socket` path no longer exists on disk, TERM-first so
  pooled backends drain cleanly. (#3315)

- **Aggregate memory ceiling across all concurrent agent spawns.** The cgroup
  memory limit was per-spawn only (65% of RAM each), so many concurrent
  subagents could collectively request several times host RAM without any
  single limit breaching. The gateway now also caps their shared parent slice
  (`kirocrew-agents.slice`) at 80% of RAM plus an aggregate task ceiling —
  override via `resource_limits.max_total_memory_mb` /
  `max_total_processes` — and logs which scopes were OOM-killed when the
  aggregate ceiling engages. (#3316)

- **Slack manifest: private channels now work out of the box.** The shipped app
  manifest adds the `groups:history` and `users:read` bot scopes and subscribes
  to the `message.groups` event, so a tracked private channel actually delivers
  messages and profile lookups resolve real names. **Existing installs are not
  fixed by upgrading alone**: Slack only grants new scopes on reinstall — update
  the app's manifest (or re-import it), then reinstall the app to the workspace
  and copy the new bot token. (#3206)

## [0.2.0] — 2026-08-09

The first feature release after launch: a real browser for the agent, four new
built-in apps, a native Windows desktop build, Korean and Japanese interfaces,
setup that no longer assumes Slack, and several hundred fixes from the first
weeks in the open.

### The agent gets a browser

- **Persistent Browser Mode** — Flip one switch in Settings and the agent can
  operate a real browser: navigate, click, type, and fill forms, with the live
  view streaming into the dashboard's Browser panel. Installation happens for
  you and recovers on its own — enabling it never errors out — and the agent can
  also serve browser work from the native embedded view.

### Eight new built-in apps

- **Spec Builder** — a spec-driven development surface: shape requirements into
  a spec, then hand it to the agent to implement.
- **Ops Mission Control** — an autonomous ops first responder with an incident
  board and a knowledge ledger of fix patterns.
- **Crew Companion** — a desk companion that reflects what your agent is doing.
- **Auto-Improvement** — measurement-first self-improvement that proposes,
  lands, and verifies its own changes GitHub-natively.
- **Meetings** — transcribes a live meeting, keeps structured notes and diagrams
  as it goes, and extracts action items you can review afterwards. Recordings
  and notes can now be deleted from the app.
- **Papyrus** — a LaTeX paper editor with a split-pane view, live PDF preview,
  and an AI co-author.
- **Mochi** — a desktop companion that lives on your screen in its own panel,
  watches pages and feeds for you, and plans its day around your schedule.
- **PPTX Maker** — describe the deck you want in chat and get a real `.pptx`
  back, by way of an agent that interviews you and writes a brief, an outline,
  and an art direction first.
- Every one of these is **opt-in**: install it from the App Store and enable it
  before it does anything.
- Installed apps are searchable and launchable from the command palette, and
  third-party apps now run under **per-app trust grants**, with a denial that
  tells you exactly what to do about it.
- **MCP Apps has its own switch** instead of riding the connection-pooling
  toggle, and the shared MCP gateway follows it.
- **Connections** gained a provider registry, so an integration declares what it
  is asking for and its consent URL is validated before you are sent to it.
- Pasting an OAuth return address for an approval that has already expired now
  says so, instead of blaming the paste — a spent approval is told apart from a
  failed delivery, so you know to start a fresh one rather than re-copy a dead
  address.
- Clicking **Connect** now asks for the provider's approval link instead of
  waiting for one, so the card offers it within seconds rather than only after
  some later chat happens to reach that server.
- Code Review Sage works against **GitHub Enterprise Server** hosts.
- An MCP server that authenticates with OAuth now receives the scope list and
  client id in the fields kiro-cli actually reads, so those connections
  authorize instead of silently failing.

### Windows, properly

- The desktop build moved to an **NSIS installer** with an integrated titlebar,
  launcher spawn/stop fixes, and a configurable sandbox tier for agent
  subprocesses. Skills, the usage ledger, and build tooling all learned the
  platform's rules.

### A dashboard you can operate

- **System is now a task manager** — live per-session resource usage, plus a
  **Storage** screen that reports what sessions cost on disk and reclaims space
  to a trash, with an inventory that no longer calls idle sessions "in use".
- **Releases tab** — this changelog, rendered per version in Settings.
- **Webhooks** — named tokens, HMAC signing, and a kill switch for inbound
  automation. The page is still being finished, so it now sits behind a
  per-device **Preview pages** toggle under Developer and is hidden by default.
- Redesigned sidebar folders, drag a session into an open chat to reference it,
  suggested folders for new sessions, consistent empty states with a next step,
  and a notification sound when an approval prompt needs you.
- **Continue instead of retyping** — resume an interrupted turn from where it
  stopped, on any idle session, and recover cleanly from tool-hook blocks and
  failed restores. Queued messages can be reordered before they send.
- The terminal panel pops out into its own window, completes subcommands and
  flags (not just paths), and takes a configurable font.
- **Agent Templates became a two-pane inspector**, and agents defined in the
  project you are working in are discovered alongside your user-level ones.
- **Send a copy of a session to another instance** — hand a conversation, with
  its context, to a different Kiro Crew you run.
- Jira issue URLs and setting references render as **link chips** you can click
  straight through.
- Stale auto-titles refresh in the background, the command palette tells a
  failed scoped search apart from an empty one, sidebar search keeps its
  relevance order, and the chat action footer grows to 40px targets on touch
  devices.
- Bold, italic, and strikethrough now render correctly in **CJK prose**.
- While the agent is waiting on something, the wait shows a **live countdown**
  with a button to end it early instead of leaving you guessing.

### Channels, and setup that no longer assumes Slack

- **`kirocrew setup` stops asking for Slack tokens.** The wizard finishes on the
  dashboard and points at the full set of chat channels; walk through the Slack
  credentials only when you ask for them with `kirocrew setup --slack`. Docs and
  in-app copy describe Kiro Crew as multi-channel rather than Slack-first.
- **Telegram** accepts inbound attachments — images for vision, documents, and
  audio that is transcribed on arrival. Serving **multiple bot accounts per
  gateway** was withdrawn before this release: a second bot is a second inbound
  door, and it is only worth having once a bot can be turned off, given its own
  security posture, and named honestly in the audit log on its own. A
  `telegram.accounts` entry written by an earlier release candidate is preserved
  in config but no longer starts a bot — move the token you want served to
  `telegram.bot_token`.
- A sub-agent's completion now reports back into **non-Slack** parent sessions,
  Discord continues the connected session when a reply arrives, and Slack
  renders an `OPTIONS` prompt as a real control everywhere it appears.

### Voice, language, and models

- **Korean and Japanese** join the dashboard — twelve interface languages.
- **On-device Apple speech-to-text** with live streaming; switch the microphone
  mid-recording; dictation lands at the cursor.
- The model picker shows each model's **credit multiplier** and scopes itself to
  what the account can actually use; background and sub-agent work take a
  **configurable per-role model** and reasoning effort.

### Autonomy with a governor

- Sub-agents can be steered with queued follow-ups, scoped to exactly the
  context a task needs, and report completions as cards in the chat.
- Monitoring loops accept a **wall-clock runtime budget**; cron jobs group into
  collapsible folders and start from a **template gallery** of 15 presets.
- Skills show their **per-injection context cost** on a budget screen, can opt
  out of injection, and the knowledge library adds documents automatically,
  dedupes per document, and honors `.kiroignore`.

### Diagnostics and trust

- **Report a Problem** collects a support bundle from the CLI or the UI, and
  every error message carries an "Ask the agent" hand-off.
- Loopback requests no longer leak the internal secret to a proxy; sensitive
  paths and credential redaction got faster without getting looser.
- The ACP runtime survives oversize output frames, worker sessions are no longer
  reaped as orphans, and `kirocrew update` works for wheel and `cli.sh` installs.
- A refusal from one of **your own** deny patterns can carry your note
  explaining it, and the seven always-on git-publish rules now render locked in
  Settings instead of offering a toggle that never took effect.
- The gateway **refuses to boot when its data home cannot persist state**,
  rather than running and losing your work silently.
- The tool-approval window and the watchdog's stall windows are both bounded by
  the turn ceiling, so neither outlives the turn it belongs to.

Plus roughly 280 further fixes across the dashboard, chat, the chat channels,
ACP transport, history consolidation, packaging, and CI.

## [0.1.3] — 2026-08-07

A hot patch for model entitlement: the model picker scopes itself to what the
account can use, a model the account cannot use is never sent, and an
unavailable model is reported as an access problem instead of a capacity error
or a raw JSON-RPC dump.

## [0.1.2] — 2026-07-30

First public release of KiroCrew — an open-source personal AI agent that runs on
your own machine, driving [kiro-cli](https://kiro.dev) over the Agent Client
Protocol. Install it, sign in once, and it is yours: no server to rent, no
account to create, and your conversations, memory, and files stay on your disk.

### Chat from wherever you already are

- **One agent, ten ways in** — A web dashboard, a native desktop app, a terminal
  CLI (`kirocrew chat`, plus a full TUI), and bots for **Slack, Discord,
  Telegram, Microsoft Teams, Webex, WeCom (企业微信), and WeChat** all drive the
  same gateway with the same memory and the same tools. Start
  something at your desk, follow up from your phone. Each Slack thread or
  Discord DM is its own isolated session, and a dashboard session can be handed
  off to a Slack thread and stay in sync both ways.
- **A dashboard built for long sessions** — Multiple concurrent chats with
  auto-generated titles, live streaming tool status, and a context-usage ring.
  Edit and resend an earlier message, rewind a conversation to any point, fork a
  session into a new tab with its full context, or regenerate a reply and browse
  the variants. Organize with project folders, tags, Trello-style columns, and
  per-session colors; search across every session by content. 18 color themes,
  a Monaco code editor, `@filename` fuzzy file attach, and an incognito mode
  whose sessions never write to memory.
- **Speak and be spoken to** — Live streaming speech-to-text over WebSocket,
  voice memos transcribed on arrival, and local Piper text-to-speech for replies
  with no cloud round-trip.
- **Ten languages** — The interface ships in English, German, Spanish, French,
  Italian, Portuguese, Russian, Hindi, Bengali, and Chinese.

### Work that continues while you are away

- **Unattended multi-step tasks** — Hand it a spec and it decomposes, executes,
  tests, and retries (`kirocrew run TASK.md`), designed for 10+ hour runs. It
  checkpoints to disk, so a crash or Ctrl+C resumes where it stopped; if
  kiro-cli dies it rebuilds the session and carries on; a watchdog catches
  stalls; and an LLM reviewer checks the result against the spec before calling
  it done. Failed steps become lessons it keeps.
- **Autopilot** — A per-session toggle that turns ordinary chat into
  plan-then-execute, with visible, editable plans, for when a request is bigger
  than one turn.
- **Cron scheduling** — Recurring jobs with per-job timezones, skip-dates for
  holidays, per-job timeouts, and jitter to spread load. Each job chooses
  whether it remembers the previous run. A job that finds a broken build at 3am
  can fix it and tell you over breakfast.
- **Parallel subagents** — Split one job across background agents
  (`kirocrew spawn run`), blocking or fire-and-forget, with progress visible in
  the chat header and completions delivered back into the conversation.
- **Dynamic workflows** — For work too structured for one agent, an authored
  Python script drives many agents through fan-out, pipelines, and
  judge-and-verify stages. An agent will usually write the script for you from a
  plain-English goal.
- **Proactive push** — The agent can pause mid-session to poll something, or
  register a webhook so an external system (CI, an alert, an inbox) wakes it up
  later.

### It remembers, and it learns

- **Memory that survives restarts** — Preferences, project context, and daily
  conversation history persist and are searched both by keyword and by meaning.
  Embeddings run **locally and in-process**, so nothing leaves your machine to
  make memory work. A graph explorer shows how memories relate.
- **Corrections stick** — Correct the agent once and it is kept as a lesson
  injected into every future session, so the same mistake does not return next
  week.
- **Knowledge Library** — Ingest your own documents and code into a searchable
  personal knowledge graph the agent can consult.
- **Snapshot and restore** — One command backs up config, memory, lessons,
  crons, skills, and history; restore all of it or just selected components,
  with a dry-run preview.

### Extend it

- **Apps, with six built in** — An App Store in the dashboard, an `app.json`
  manifest, TypeScript and Python SDKs, and gateway lifecycle hooks. Shipping in
  the box: **Auto Research** (multi-cycle research campaigns that keep going
  after you walk away), **Code Review Sage** (reviews each changed file of a PR
  in its own agent session), **Issue Radar** (GitHub/GitLab triage that
  remembers its notes), **Workflows**, **File Explorer**, and **Dev Fleet**.
- **Skills** — Plain markdown files that teach the agent a workflow, loaded
  automatically when a message matches or on demand when it decides it needs
  one. Twelve ship built in; write your own with no code and no rebuild.
- **Any MCP server** — Discover, probe, enable, and disable MCP servers from the
  dashboard. KiroCrew's own capabilities are exposed the same way, so the agent
  calls structured tools instead of shelling out.
- **Artifacts** — Documents, code files, and interactive widgets with a stable
  identity, version history, and a dashboard library. Deploy a webapp artifact
  to **your own** AWS account and get a public HTTPS link with a TTL.

### Drive your desktop, not just a browser tab

- **Computer use** — The agent can read a native application through the
  accessibility layer and operate it: take a window as a numbered outline of its
  buttons, fields, and rows, then press, type, set a value, scroll, or drag.
  This reaches work with no web UI — pulling a figure out of a spreadsheet,
  walking a desktop-only internal tool, reading an error dialog and telling you
  what it says. **Your mouse pointer never moves by accident**: actions are
  delivered to the target app, so a background window works without stealing
  your cursor or focus, and the one path that does take your real pointer has to
  be named explicitly by the model — the automatic choice never resolves onto it.
  **Off by default and macOS-only in this release**; enable it in Settings →
  Computer Use. Password fields are never read and a window holding one is never
  photographed, destructive-command-shaped text is refused rather than typed, and
  every call — allowed or refused — is written to the audit log.
- **Browser automation** — Playwright-driven navigation, form filling, and
  screenshots, including the ability to look at its own front-end changes and
  judge them.

### Security you can reason about

- **An OS sandbox you can switch on** — kiro-cli subprocesses can be confined by
  Linux namespaces or macOS Seatbelt, with three modes controlling which
  credential directories are even visible. This ships **opt-in**: the default
  (`agent.sandbox: "off"`) defers to whatever sandboxing kiro-cli applies itself,
  so set `agent.sandbox` to `"auto"` to have KiroCrew wrap the subprocess.
- **Layered controls** — 137 built-in denied-command patterns that hold even in
  YOLO mode, credential redaction scanning everything the model emits, blocked
  access to `~/.aws` and `~/.ssh`, XSS sanitization with CSP, and an audit log of
  every command.
- **A ceiling the agent cannot raise** — A two-level governance model
  (`POLICY ∩ PROFILE`, tightest-wins) enforced at KiroCrew's own tool gate. The
  policy files live where the agent can neither read nor write them, so a
  prompt-injected agent cannot widen its own limits. Tool calls are auto-approved
  by default (`agent.approval_mode: "auto"`) with the deny and governance gates
  still applied first — set it to `"interactive"` to be asked before each call.
  The dashboard is loopback-only and the Slack bot is locked to its owner.

### Run it your way

- **Install however suits you** — A signed and notarized universal macOS DMG, a
  Linux AppImage, a multi-arch Docker image for always-on servers, and a
  `pip`-installable wheel. The desktop app bundles its own Python, so end users
  need no toolchain. Runs on **macOS, Linux, and Windows**.
- **Three release channels** — **stable** is the default; **insider** gets
  release candidates a week or two early and is a switch away in Settings, since
  the two share one app and just follow different update lanes; **nightly**
  tracks the latest code and installs alongside your production app rather than
  replacing it, so you can run both. The desktop app updates itself, and nothing
  downloads or installs without you asking.
- **Always on** — Install as a systemd or launchd service, and manage several
  remote instances (dev boxes, EC2, a home server) from one hub over SSH.

### For app developers

- **`ctx.cron` mutators stay synchronous, with `*_async` siblings.** The App Kit
  surface (`add_job` / `remove_job` / `update_job` / `remove_all`) is
  synchronous, as published. Called from a genuinely loop-less context (CLI, MCP
  process, worker thread — what apps overwhelmingly use) they run inline as
  before. Called from a **running event loop** — an on-loop `on_startup` hook or
  route handler — they now raise `CronSyncOnLoopError` instead of parking the
  gateway loop for the cron-store lock window and stalling chat, timers, and
  heartbeats for every session. Migration is one line:
  `ctx.cron.add_job(...)` → `await ctx.cron.add_job_async(...)`, identical
  arguments and return value. The error is raised before any mutation, so a
  refused call never half-applies.

### Notes

- **kiro-cli is required** — KiroCrew orchestrates it. `kirocrew setup` walks you
  through installing and signing in; `kirocrew doctor` verifies the whole wiring.
- **Data lives in `~/.kiro/crew`** — override with `KIROCREW_HOME`. Installs
  using the earlier `~/.kirocrew` layout migrate automatically on first launch.
- **The dashboard defaults to `http://localhost:5476`** — override with
  `KIROCREW_PORT`.
- **Optional extras** — speech-to-text needs `pip install kirocrew[voice]`; the
  OS sandbox is POSIX-only; computer use is macOS-only in this release.
