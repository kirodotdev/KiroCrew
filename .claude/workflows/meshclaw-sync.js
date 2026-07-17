export const meta = {
  name: 'meshclaw-sync',
  description: 'Sync fixes from internal MeshClaw (backend) + MeshClawWebsite (frontend) into the de-Amazoned public KiroCrew fork: scan both upstreams from the boundary, adversarially triage each candidate, then (opt-in) port / verify / build DMGs / commit / PR.',
  whenToUse: 'Run the recurring MeshClaw->KiroCrew content sync. Defaults to a read-only triage report; pass {mode:"port"} or {mode:"full"} to mutate, build, and raise a PR. Source of truth is skills/meshclaw-sync/SKILL.md.',
  phases: [
    { title: 'Scan' },
    { title: 'Triage' },
    { title: 'Port' },
    { title: 'Verify' },
    { title: 'Build' },
    { title: 'Ship' },
  ],
}

// ---------------------------------------------------------------------------
// Config (all paths come from args so this runs on any machine). Defaults
// match the documented checkout locations in skills/meshclaw-sync/SKILL.md.
// ---------------------------------------------------------------------------
const cfg = {
  fork: (args && args.workspaceFork) || '/Volumes/workplace/kirocrew',
  backend: (args && args.upstreamBackend) || '/Volumes/workplace/MeshClaw/src/MeshClaw',
  frontend: (args && args.upstreamFrontend) || '/Volumes/workplace/MeshClaw/src/MeshClawWebsite',
  // mode: 'triage' (read-only, default) | 'port' (port + verify, no build/PR) | 'full' (port + verify + build + commit + PR)
  mode: (args && args.mode) || 'triage',
}
const SKILL = `${cfg.fork}/skills/meshclaw-sync/SKILL.md`
const BOUNDARY = `${cfg.fork}/skills/meshclaw-sync/last-synced.txt`
const willPort = cfg.mode === 'port' || cfg.mode === 'full'
const willShip = cfg.mode === 'full'

// The de-Amazon + symbol-map rules every agent must apply. Kept short here;
// the agent MUST read SKILL.md for the authoritative, exhaustive version.
const RULES = `
You are working the MeshClaw -> KiroCrew content sync. The fork shares NO git
history with the upstreams, so nothing is cherry-picked — everything is ported
BY CONTENT, path- and symbol-mapped, then re-verified.

BEFORE doing anything, READ the source of truth in full:
  ${SKILL}
It defines Steps 1-7, the de-Amazon SKIP rubric, the "Anti-miss: a NAME is not
a verdict" section, and every gotcha. If this prompt and the skill ever
disagree, THE SKILL WINS.

Path map: backend src/mesh_claw/X -> fork src/kiro_crew/X ; frontend
MeshClawWebsite src/X -> fork website/src/X (tests -> website/src/test or
website/integration). Symbol map everywhere incl. comments + tests:
  mesh_claw->kiro_crew  MeshClaw->KiroCrew  meshclaw->kirocrew
  MESHCLAW_->KIROCREW_  .meshclaw->.kirocrew  meshclaw-ui->kirocrew-ui
  source:'meshclaw'->'kirocrew'  /api/config/meshclaw->/api/config/kirocrew
KEEP VERBATIM: mc-* localStorage/postMessage keys, the mc_token_ cookie prefix,
inert tool-name allowlist literals, and Mesh-NNNN / CR-NNNNNN ticket refs.

KIROACP-ONLY INVARIANT: the public core supports ONLY the KiroACP (kiro-cli)
provider. ClaudeCodeProvider/BedrockProvider, cc_agent, mirror, the
cc_*/bedrock_* config, and the provider enum beyond ["acp"] were DELETED. The
dormant ACP_BACKEND_CLAUDE / _is_claude seam in acp/client.py is KEPT but you do
NOT re-add registration glue. A commit confined to the Claude-Code/Bedrock
surface is SKIP_NONKIROACP; a commit that REMOVES Bedrock/claude_code from the
UI is KEEP (it aligns the SPA to the enum ["acp"]).

ANTI-MISS (decide by the DIFF, never the commit title/path/symbol): before any
SKIP/ALREADY_PRESENT, open the diff and check (a) gated-on vs merely-named-for
the internal thing, (b) does a SHARED helper/choke point change -> grep its
other fork callers, (c) ALREADY_PRESENT = behavior present (read the cited fork
file) not a similar name — a present feature can still owe a MISSING generic
test (PARTIAL test-only), (d) provenance != port, and cross-check upstream HEAD
for a later revert. DROP only when TRULY confined to an absent/stubbed subsystem
(confirm ABSENT by ls/grep in the fork): mcp_gateway, promptfarm, secretary,
team_manager, code_reviewer, taskkeeper, writing_review, mimir, lcars; or
internal: midway, mwinit, MCS, kerberos, federate, AEA-tunnel, builder-mcp,
arcc, quip, taskei, brazil, toolbox, cognito, codeartifact, GitFarm, AIM,
Artifactory, RUM. NEVER re-add .midway/.ada to a sensitive-path list.
`

const SCAN_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['boundary', 'backendCandidates', 'frontendCandidates'],
  properties: {
    boundary: {
      type: 'object',
      additionalProperties: false,
      required: ['beta', 'frontendBeta', 'mainline'],
      properties: {
        beta: { type: 'string', description: 'backend beta SHA from last-synced.txt' },
        frontendBeta: { type: 'string', description: 'frontend-beta SHA from last-synced.txt' },
        mainline: { type: 'string', description: 'upstream-mainline SHA from last-synced.txt' },
      },
    },
    backendCandidates: {
      type: 'array',
      description: 'New backend commits in beta range + any new mainline-only commits (NOT ancestors of beta).',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['sha', 'title'],
        properties: { sha: { type: 'string' }, title: { type: 'string' } },
      },
    },
    frontendCandidates: {
      type: 'array',
      description: 'New frontend commits in the frontend-beta content window.',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['sha', 'title'],
        properties: { sha: { type: 'string' }, title: { type: 'string' } },
      },
    },
    syncDate: { type: 'string', description: 'Today (YYYY-MM-DD) from the `date` command, for the PR title.' },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['sha', 'repo', 'verdict', 'reason'],
  properties: {
    sha: { type: 'string' },
    repo: { type: 'string', enum: ['backend', 'frontend'] },
    title: { type: 'string' },
    verdict: {
      type: 'string',
      enum: ['KEEP', 'PARTIAL', 'SKIP_INTERNAL', 'SKIP_NONKIROACP', 'ALREADY_PRESENT', 'DEFER'],
    },
    reason: { type: 'string', description: 'One-to-three sentences citing the DIFF + the fork file you read, not the title.' },
    upstreamCr: { type: 'string', description: 'cr:/Task:/Issue: trailer URL(s) from the source commit, or empty.' },
    portInstructions: {
      type: 'string',
      description: 'For KEEP/PARTIAL: the concrete files to touch in the fork, anchors to splice at, fork-divergence adaptations, which hunks to DROP (PARTIAL), and the test files to run. Empty for SKIP/ALREADY_PRESENT.',
    },
    antiMissChecked: { type: 'boolean', description: 'True iff you opened the diff and applied the anti-miss checks before a SKIP/ALREADY_PRESENT verdict.' },
  },
}

// ---------------------------------------------------------------------------
// Phase 1 — SCAN both upstreams from the boundary.
// ---------------------------------------------------------------------------
phase('Scan')
const scan = await agent(
  `${RULES}

TASK (Step 1 of the skill — SCAN, read-only):
1. \`cd ${cfg.backend} && git fetch -q\` and \`cd ${cfg.frontend} && git fetch -q\`.
2. Read the boundary file ${BOUNDARY} — extract the \`beta\`, \`frontend-beta\`,
   and \`mainline\` SHAs (these are the LAST-SYNCED tips; the mainline line is
   the UPSTREAM MeshClaw mainline tip, not the fork's).
3. Backend candidates = \`git -C ${cfg.backend} log --no-merges --oneline <beta>..origin/beta-braveheart\`
   PLUS any \`<mainline>..origin/mainline\` commit that is NOT an ancestor of
   origin/beta-braveheart (\`git merge-base --is-ancestor\`).
4. Frontend candidates = \`git -C ${cfg.frontend} log --no-merges --oneline <frontend-beta>..origin/beta-braveheart\`.
   (The fork's website/ is a DIVERGED partial snapshot, so this range only
   BOUNDS the set — final present/absent is decided by content in triage.)
5. Run \`date -u +%F\` for syncDate.
Return the boundary, both candidate lists (sha + one-line title each), and syncDate.
Do NOT triage yet — just enumerate.`,
  { label: 'scan:both-repos', phase: 'Scan', schema: SCAN_SCHEMA },
)

const candidates = [
  ...scan.backendCandidates.map((c) => ({ ...c, repo: 'backend' })),
  ...scan.frontendCandidates.map((c) => ({ ...c, repo: 'frontend' })),
]
log(`Scan: ${scan.backendCandidates.length} backend + ${scan.frontendCandidates.length} frontend candidate(s). Boundary beta=${scan.boundary.beta} fe=${scan.boundary.frontendBeta}.`)

if (candidates.length === 0) {
  log('No new candidates across both repos — nothing to sync. Exiting (no commit, no PR).')
  return { boundary: scan.boundary, candidates: [], verdicts: [], note: 'no new commits' }
}

// ---------------------------------------------------------------------------
// Phase 2 — TRIAGE + adversarial verify (read-only). One analyzer + one
// skeptic per candidate, pipelined (no barrier between the two stages).
// ---------------------------------------------------------------------------
phase('Triage')
const repoPath = (repo) => (repo === 'backend' ? cfg.backend : cfg.frontend)
const triaged = await pipeline(
  candidates,
  // Stage 1 — analyze the diff against fork content, produce a draft verdict.
  (c) =>
    agent(
      `${RULES}

TASK (Step 2 — TRIAGE one commit by CONTENT, read-only):
Commit ${c.sha} ("${c.title}") in the ${c.repo} upstream (${repoPath(c.repo)}).
- Read its full diff: \`git -C ${repoPath(c.repo)} show ${c.sha}\` and its
  trailers: \`git -C ${repoPath(c.repo)} log -1 --format=%b ${c.sha}\`.
- For EVERY touched path, decide present/absent IN THE FORK by reading the
  corresponding fork file under ${cfg.fork} (ls/grep/Read) — never by SHA.
- Apply the anti-miss checks. A name is not a verdict.
- Emit a verdict (KEEP/PARTIAL/SKIP_INTERNAL/SKIP_NONKIROACP/ALREADY_PRESENT/DEFER)
  with a reason citing the diff + the fork file you read, the upstream cr:/Task:
  trailer URL(s), and — for KEEP/PARTIAL — concrete portInstructions (files,
  anchors, fork-divergence adaptations, hunks to DROP, tests to run).
- Set antiMissChecked=true once you've opened the diff and run the checks.`,
      { label: `analyze:${c.sha.slice(0, 8)}`, phase: 'Triage', schema: VERDICT_SCHEMA },
    ),
  // Stage 2 — skeptic: try to OVERTURN the draft (esp. a SKIP that hides a
  // generic hunk, or a KEEP that re-adds a forbidden coupling).
  (draft, c) =>
    agent(
      `${RULES}

TASK (adversarial verify — try to OVERTURN this verdict):
Commit ${c.sha} ("${c.title}", ${c.repo}). Draft verdict: ${draft.verdict}.
Draft reason: ${draft.reason}

Re-open the diff (\`git -C ${repoPath(c.repo)} show ${c.sha}\`) and the cited
fork files. Be skeptical:
- If draft is SKIP/ALREADY_PRESENT: hunt for a GENERIC, ungated hunk wired into
  a SHARED choke point that the headline hid (the #1 cause of wrongly-dropped
  fixes). If you find one, flip to PARTIAL and say which hunk + fork callers.
- If draft is KEEP/PARTIAL: confirm no hunk re-introduces a forbidden coupling
  (midway/builder-mcp/brazil/toolbox/bedrock/etc.) and that the port
  instructions match the fork's ACTUAL divergence. Cross-check upstream HEAD for
  a later revert of a removal.
Return the FINAL verdict (keep the draft if it holds, or correct it). Preserve
the upstream cr trailer and tighten portInstructions.`,
      { label: `verify:${c.sha.slice(0, 8)}`, phase: 'Triage', schema: VERDICT_SCHEMA },
    ),
)

const verdicts = triaged.filter(Boolean)
const keepers = verdicts.filter((v) => v.verdict === 'KEEP' || v.verdict === 'PARTIAL')
const skipped = verdicts.filter((v) => v.verdict !== 'KEEP' && v.verdict !== 'PARTIAL')
log(`Triage: ${keepers.length} to port (KEEP/PARTIAL), ${skipped.length} SKIP/ALREADY_PRESENT/DEFER.`)

// In triage-only mode, stop here with the report. This is the safe default.
if (!willPort || keepers.length === 0) {
  if (!willPort) log('mode=triage (read-only) — returning the triage report. Re-run with {mode:"port"} or {mode:"full"} to apply changes.')
  else log('No KEEP/PARTIAL commits — boundary-only advance. Nothing to port.')
  return { boundary: scan.boundary, syncDate: scan.syncDate, verdicts, ported: [], note: willPort ? 'no keepers' : 'triage-only' }
}

// ---------------------------------------------------------------------------
// Phase 3 — PORT (mutating). ONE sequential agent ports every keeper in
// chronological order onto the sync branch. Sequential — never parallel —
// because keepers routinely share files (a parallel fan-out corrupts the tree).
// ---------------------------------------------------------------------------
phase('Port')
const keeperBrief = keepers
  .map((v) => `- ${v.sha} [${v.repo}] ${v.verdict}: ${v.title || ''}\n    reason: ${v.reason}\n    port: ${v.portInstructions}\n    upstream-cr: ${v.upstreamCr || '(none)'}`)
  .join('\n')

const portReport = await agent(
  `${RULES}

TASK (Step 3 + 4 + 5 — PORT the keepers, verify each, commit each). MUTATING.
Work in ${cfg.fork}. Sync date: ${scan.syncDate}.

BRANCH: if a sync/beta-* branch is already checked out AND based on
origin/main, use it (a prior PR may still be pending — stacking is correct).
Otherwise \`git fetch origin\` then create sync/beta-${scan.syncDate} off
origin/main.

Port these ${keepers.length} commit(s), IN CHRONOLOGICAL ORDER (oldest first),
ONE AT A TIME (never parallel — they may share files):
${keeperBrief}

For EACH commit:
1. Read the upstream diff AND the fork file(s) it maps to — apply the intent by
   content, re-anchoring where the fork diverged (the portInstructions note the
   adaptations). For a NEW file whose fork pre-image == upstream pre-image, you
   may regenerate from the post-image via the sed symbol-map trick in the skill.
   For PARTIAL, port only the generic hunks and DROP the internal ones.
2. VERIFY (do NOT trust grep — RUN it):
   - backend: \`PYTHON_GIL=0 PYTHONPATH=src python3 -m pytest <touched files> --override-ini="addopts=" -p no:cacheprovider -q\`, then \`flake8 <touched files>\` (the gate; watch F824). Do NOT run black/isort over the tree.
   - frontend: \`cd website && npx tsc -b\` (the REAL typecheck, NOT --noEmit) and \`npx vitest run <touched test files>\`.
3. \`git add\` the touched files and commit one fix per commit:
   "<type>: <summary>" + body "Ported by content from MeshClaw[Website] upstream
   <full-sha>: https://code.amazon.com/packages/<MeshClaw|MeshClawWebsite>/commits/<full-40-char-sha>"
   plus the Upstream-CR / Task links (full https:// URLs) from the trailer.

After all ports, in a FINAL commit update ${BOUNDARY}: advance \`beta\`,
\`frontend-beta\`, and \`mainline\` to the new tips (boundary advances past
resolved SKIPs too), and prepend a dated batch note block summarizing
PORTED/SKIP with reasons.

Then run the cumulative de-Amazon audit from the skill (Step 6) over the live
added lines and confirm ZERO live couplings (only inert comments/allowlist
literals are allowed). Report: the branch name, each commit SHA + message, the
verify results, and the de-Amazon audit outcome.`,
  { label: 'port:sequential', phase: 'Port' },
)
log('Port phase complete. See the report for per-commit verify results.')

// ---------------------------------------------------------------------------
// Phase 4 — VERIFY (independent confirmation pass on the cumulative diff).
// ---------------------------------------------------------------------------
phase('Verify')
const verifyReport = await agent(
  `${RULES}

TASK (Step 4 — independent CUMULATIVE verify of the ported branch in ${cfg.fork}).
This is a fresh check, separate from the per-commit runs:
- Backend: run pytest on every touched test file together
  (\`PYTHON_GIL=0 PYTHONPATH=src python3 -m pytest <files> --override-ini="addopts=" -p no:cacheprovider -q\`)
  and \`flake8\` on every touched src+test file.
- Frontend: \`cd website && npx tsc -b\` then \`npx vitest run\` (full suite — the
  ports touched hot shared files).
- De-Amazon audit: \`git diff origin/main...HEAD\` filtered to live added
  lines, grep for midway|mwinit|mcs|kerberos|federate|aea|cognito|codeartifact|
  builder-mcp|arcc|quip|taskei|brazil|toolbox|bedrock — expect ONLY inert
  comments/allowlist literals. Any live new usage is a bug to flag.
Report pass/fail counts for each and the audit result. If anything fails, name
the file + failure precisely so the orchestrator can fix it.`,
  { label: 'verify:cumulative', phase: 'Verify' },
)
log('Verify phase complete.')

if (!willShip) {
  log('mode=port — stopping before build/commit/PR. Review the branch, then ship manually or re-run with {mode:"full"}.')
  return { boundary: scan.boundary, syncDate: scan.syncDate, verdicts, portReport, verifyReport, note: 'ported, not shipped' }
}

// ---------------------------------------------------------------------------
// Phase 5 — BUILD both macOS DMGs (dual-arch). Only in full mode.
// ---------------------------------------------------------------------------
phase('Build')
const buildReport = await agent(
  `${RULES}

TASK (Step 7.1 — rebuild BOTH macOS DMGs dual-arch; recipe in
${cfg.fork}/docs/DESKTOP_APP.md). Work in ${cfg.fork}. macOS host only.
- Detach any stale KiroCrew disk mounts first (hdiutil) to avoid "Resource
  temporarily unavailable (35)". rm stale DMGs in website/electron/dist.
- Frontend bundle: \`cd website && npm run build && cd .. && rm -rf
  src/kiro_crew/static/dist && cp -R website/dist src/kiro_crew/static/dist\`.
- arm64: \`SKIP_FRONTEND=1 PYTHON="$PWD/.venv/bin/python" bash packaging/build-desktop.sh\`.
- x86_64: PyInstaller under \`arch -x86_64 .venv-x86/bin/python -m PyInstaller
  packaging/kirocrew-backend.spec --noconfirm --distpath build/pyinstaller-x86/dist
  --workpath build/pyinstaller-x86/build\` -> stage into
  website/electron/backend-dist/kirocrew-backend -> \`(cd website/electron && npx
  electron-builder --mac --x64)\` -> RESTORE the arm64 backend from
  build/pyinstaller/dist into backend-dist.
- pip install pyinstaller into .venv/.venv-x86 if missing; keep electron version 0.1.0.
- MOUNT-VERIFY each DMG: \`file .../Resources/backend-dist/kirocrew-backend/kirocrew-backend\`
  — the arm64 DMG must carry arm64, the x64 DMG x86_64. A mismatch crashes on launch.
DMGs + static/dist are gitignored (not in the PR). Report both DMG paths + the
verified arch of each.`,
  { label: 'build:dual-arch-dmg', phase: 'Build' },
)
log('Build phase complete.')

// ---------------------------------------------------------------------------
// Phase 6 — SHIP: PR + exhaustive provenance comment. Only in full mode.
// ---------------------------------------------------------------------------
phase('Ship')
const shipReport = await agent(
  `${RULES}

TASK (Step 7.3 — raise the PR). Work in ${cfg.fork}. THIS RUN IS AUTHORIZED to
commit/push/PR (the recurring auto-sync standing authorization).
- Push the sync branch: \`git push -u origin HEAD\`.
  (origin = https://github.com/kirodotdev/KiroCrew.git)
- Create the PR: \`gh pr create --base main\`.
- TITLE: "sync: MeshClaw dual-repo beta sync ${scan.syncDate} (N ported)"
  (say backend+frontend counts). The PR BODY should contain a concise
  high-signal summary with the two upstream commit-browser links.
- Post the EXHAUSTIVE per-commit provenance as a PR COMMENT via
  \`gh pr comment <PR-number> --body "..."\`: every KEEP/PARTIAL with its full
  upstream SHA link + summary, and every SKIP/ALREADY_PRESENT/DEFER with the
  reason. Every SHA/CR/Task id MUST be a full clickable https:// URL
  (code.amazon.com/packages/<Pkg>/commits/<full-sha>, /reviews/CR-<id>,
  taskei.amazon.dev/tasks/<id>).
Here are the triaged verdicts to source the provenance from:
${verdicts.map((v) => `${v.sha} [${v.repo}] ${v.verdict}: ${v.reason} (cr: ${v.upstreamCr || 'n/a'})`).join('\n')}
Report the PR number + URL and confirm the comment was published.`,
  { label: 'ship:pr', phase: 'Ship' },
)

return {
  boundary: scan.boundary,
  syncDate: scan.syncDate,
  verdicts,
  portReport,
  verifyReport,
  buildReport,
  shipReport,
  note: 'full pipeline complete',
}
