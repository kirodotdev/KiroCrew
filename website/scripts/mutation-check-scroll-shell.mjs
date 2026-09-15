/**
 * Mutation check for the ChatPage scroll-shell characterization net.
 *
 * Proves the net has teeth line by line: for EVERY code line in the shell
 * region (header fade → jump pill), delete that line and require at least one
 * pinning test to fail. A mutant that survives is a hole in the net — the
 * harness exits 1 and lists it, so the fix is to add a pin (or explicitly
 * scope the line out below, with a reason).
 *
 * A mutant that breaks compilation counts as caught: vitest fails on it, and a
 * migration that broke compilation could never ship either. Comment-only and
 * whitespace lines are skipped (they are not code).
 *
 * Usage (from website/):
 *   node scripts/mutation-check-scroll-shell.mjs            # full run
 *   node scripts/mutation-check-scroll-shell.mjs --list     # show target lines, run nothing
 */
import { readFileSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { execSync } from 'node:child_process'
import { resolve } from 'node:path'
import { createRequire } from 'node:module'

/** A mutant that no longer PARSES is caught by construction: `tsc -b` blocks
 *  the push gate and CI, so a migration that broke the syntax could never
 *  ship. Checking it here (fast, ~100ms via the project's own TypeScript
 *  parser) keeps the per-line guarantee honest without running the full
 *  type-checker per mutant. */
const ts = createRequire(import.meta.url)('typescript')
const parses = (src) => {
  const out = ts.transpileModule(src, {
    compilerOptions: { jsx: ts.JsxEmit.Preserve, target: ts.ScriptTarget.ESNext },
    reportDiagnostics: true,
    fileName: 'ChatPage.tsx',
  })
  return (out.diagnostics ?? []).length === 0
}

// The shell region may live in ChatPage or, post-extraction, in the shell
// component; mutate whichever file currently holds each region's anchors.
const FILES = ['src/pages/ChatPage.tsx', 'src/pages/chat/TranscriptScrollShell.tsx'].map(f => resolve(f))
const SUITES = [
  'src/test/ChatPage.scrollShell.recipe.test.tsx',
  'src/test/ChatPage.scrollShell.render.test.tsx',
  'src/test/ChatPage.fadeClearance.test.tsx',
]

const START = '{/* Header fade'
const END = 'Status chrome never claims'

/** The shell is SIX sub-regions, each an explicit (start, end) anchor pair —
 *  exactly the pieces the migration moves. Code between them (PinnedPrompt,
 *  WelcomeView, drop overlay) stays on the page verbatim and is out of scope.
 *  `includeEnd` pulls the end-anchor line itself into scope — used where that
 *  line is load-bearing code, so the anchor choice cannot exempt it. */
const REGIONS = [
  { name: 'header-fade', start: '<EdgeFade side="top"', end: '{/* Fold sentinel' },
  // The shell IMPORT on the page — one line, but deleting it is precisely the
  // "extraction silently undone" mutation, so it gets its own region.
  { name: 'shell-import', start: "import TranscriptScrollShell", end: 'import { useVirtualChat' },
  // The prop-plumbing seam the split invented: the wiring interface, the
  // component signature, and the destructured parameter list. This block did
  // not exist pre-extraction — anchoring the skeleton region below it would
  // exempt exactly the code under review via anchor PLACEMENT (the exclusion
  // channel the deadExclusions guard cannot see).
  { name: 'shell-props-seam', start: 'export interface TranscriptVirtWiring', end: 'return (' },
  // The scroller SKELETON lives in TranscriptScrollShell; rows/footer are
  // CONTENT threaded through as slots (their wrapper contract is pinned by the
  // recipe suite's row-wrapper tests, their props by their own suites).
  // includeEnd: `{belowRows}` is the line that mounts the below slot — its
  // deletion must be a caught mutant, not an anchor exemption.
  { name: 'scroller-skeleton', start: 'ref={scrollerRef}', end: '{belowRows}', includeEnd: true },
  // The page keeps the scalar wiring props on the call site.
  { name: 'shell-call-props', start: '<TranscriptScrollShell', end: 'aboveRows={' },
  // The slot BODIES on the call site: the earlier-messages gate/bar, footer,
  // survey, tail spacer. This is the cross-file seam the extraction created —
  // exactly where a silent drop or slot swap would live. Ends at the children
  // boundary: the message-row body was NOT moved by the extraction and stays
  // pinned by the row-wrapper recipe tests and the page's own suites.
  { name: 'call-site-slots', start: 'aboveRows={', end: '{/* Message items' },
  { name: 'bottom-mask', start: '{/* Transcript bottom mask', end: '<div className="relative">' },
  { name: 'jump-pill', start: '<JumpToBottomButton', end: '{/* Status chrome' },
]

/** Lines that are CONTENT rendered inside the shell, not the shell itself —
 *  the later migration moves the shell around them and leaves them verbatim,
 *  so their props are pinned by their own component suites, not this net.
 *  Every entry needs a reason; an unexplained exclusion defeats the harness. */
const OUT_OF_SCOPE = [
  { match: /^\s*(key=|sessionId=|kiroCrewVersion=|turnCount=|slotOrigin=|onLayoutChange=)/, reason: 'SessionPulseSurveyCard props — content child, pinned by its own suite' },
  { match: /^\s*<ChatFooter .*\/>$/, reason: 'whole footer line — presence+slot membership pinned by the render suite; props by ChatFooter suites' },
  { match: /^\s*<SessionPulseSurveyCard$/, reason: 'opening tag only; presence+order IS pinned, props are the child suite\'s' },
]

const sources = FILES.map(f => { try { return { file: f, text: readFileSync(f, 'utf8') } } catch { return null } }).filter(Boolean)

const isCode = (line, inComment) => {
  const t = line.trim()
  if (inComment.on) {
    if (t.endsWith('*/') || t.endsWith('*/}')) inComment.on = false
    return false
  }
  if ((t.startsWith('{/*') || t.startsWith('/*')) && !(t.endsWith('*/}') || t.endsWith('*/'))) { inComment.on = true; return false }
  if (t === '' || t === '}' || t === ')}' || t === ')' || t === '>' || t === '/>' || t === '})}') return false
  if (t.startsWith('//') || t.startsWith('*') || t.startsWith('{/*') || t.endsWith('*/}')) return false
  return true
}

const targets = []
for (const region of REGIONS) {
  // Find the ONE file currently holding this region's anchors.
  const holder = sources.find(s => {
    const i = s.text.indexOf(region.start)
    return i >= 0 && s.text.indexOf(region.end, i) > i
  })
  if (!holder) {
    console.error(`anchor not found for region ${region.name} in any shell file — update REGIONS`)
    process.exit(2)
  }
  const lines = holder.text.split('\n')
  const startIdx = lines.findIndex(l => l.includes(region.start))
  const endIdx = lines.findIndex((l, i) => i > startIdx && l.includes(region.end))
  const inComment = { on: false }
  const stopIdx = region.includeEnd ? endIdx + 1 : endIdx
  for (let i = startIdx; i < stopIdx; i++) {
    const line = lines[i]
    if (!isCode(line, inComment)) continue
    const excluded = OUT_OF_SCOPE.find(e => e.match.test(line))
    if (excluded) { targets.push({ i, line, file: holder.file, region: region.name, skip: excluded.reason }); continue }
    targets.push({ i, line, file: holder.file, region: region.name })
  }
}

// A documented exclusion that matches nothing is dead config: it means the
// region layout shifted and the harness silently lost the scope the exclusion
// was written for (exactly how "0 scoped out" once masked an unmutated seam).
const deadExclusions = OUT_OF_SCOPE.filter(e => !targets.some(t => t.skip === e.reason))
if (deadExclusions.length) {
  console.error('DEAD OUT_OF_SCOPE entries (match no in-region line — fix REGIONS or delete the entry):')
  for (const e of deadExclusions) console.error(`  ${e.match} — ${e.reason}`)
  process.exit(2)
}

if (process.argv.includes('--list')) {
  for (const t of targets) console.log(`${t.i + 1}: ${t.skip ? `[SKIP: ${t.skip}] ` : ''}${t.line.trim().slice(0, 100)}`)
  console.log(`\n${targets.filter(t => !t.skip).length} mutation targets, ${targets.filter(t => t.skip).length} scoped out`)
  process.exit(0)
}

/** Thrown when vitest did not run to completion — the caller must restore the
 *  mutant FIRST and only then exit. process.exit() inside this function would
 *  skip the caller's finally block and leave a mutated source file on disk
 *  (this exact failure happened: mutant #1's giant assertion diff overflowed
 *  the execSync pipe buffer, the truncated output carried no failure summary,
 *  and the abort path exited without restoring). */
class InfraError extends Error {}

/** Verdicts come from the JSON reporter ON DISK, not from parsing the pipe: a
 *  red toContain against a whole-file source dump prints the entire file per
 *  failing test (tens of MB with ANSI codes), which overflows any reasonable
 *  pipe buffer and truncates away the console summary line. The JSON file is
 *  immune to that, and lets us demand NAMED failing tests as the evidence. */
const RESULT_FILE = resolve(tmpdir(), `mutation-scroll-shell-${process.pid}.json`)
const run = () => {
  rmSync(RESULT_FILE, { force: true })
  let exitedNonzero = false
  try {
    execSync(`npx vitest run ${SUITES.join(' ')} --silent --reporter=json --outputFile="${RESULT_FILE}"`, { stdio: 'pipe', timeout: 120_000, maxBuffer: 64 * 1024 * 1024 })
  } catch (err) {
    if (err.killed || err.signal) throw new InfraError(`vitest did not run to completion (${err.signal || 'timeout'})`)
    exitedNonzero = true
  }
  let report
  try {
    report = JSON.parse(readFileSync(RESULT_FILE, 'utf8'))
  } catch {
    throw new InfraError('vitest produced no readable JSON report — runner/config error, not a red test')
  }
  if (!exitedNonzero && report.numFailedTests === 0 && report.numFailedTestSuites === 0) return 'green'
  // NAMED failing tests are the strong evidence — a pin went red.
  if (report.numFailedTests > 0) return 'caught-test'
  // A suite that fails to COLLECT (deleted line broke the import surface)
  // names no test and exercises no pin; it could never ship (tsc/CI), but it
  // must not be laundered into the pin-caught number.
  if (report.numFailedTestSuites > 0) return 'caught-collect'
  throw new InfraError('vitest exited nonzero but its report names no failing test — infra error')
}

console.log('baseline run (must be green)...')
if (run() !== 'green') { console.error('baseline is RED — fix the suites before mutation-checking'); process.exit(2) }

const survivors = []
let caughtParse = 0
let caughtTest = 0
let caughtCollect = 0
let done = 0
const active = targets.filter(t => !t.skip)
// Crash/termination safety: while a mutant is on disk the working tree is
// corrupted (one deleted line, possibly uncommitted). Restore in finally AND
// on every catchable termination signal — SIGTERM/SIGHUP during execSync
// would otherwise bypass finally and leave the deleted line on disk.
// Restoration is GUARDED: it only overwrites the file if it still holds the
// mutant we wrote. An editor autosave landing during the vitest window means
// the file is no longer ours to clobber — warn and leave it for the human.
let restore = null
const safeRestore = (r) => {
  let now
  try { now = readFileSync(r.file, 'utf8') } catch { return }
  if (now === r.mutated) { writeFileSync(r.file, r.original); return }
  if (now !== r.original) console.error(`NOT restoring ${r.file}: it changed while a mutant was on disk (concurrent edit?) — recover the deleted line manually`)
}
for (const [sig, code] of [['SIGINT', 130], ['SIGTERM', 143], ['SIGHUP', 129]]) {
  process.on(sig, () => { if (restore) safeRestore(restore); process.exit(code) })
}
for (const t of active) {
  const original = readFileSync(t.file, 'utf8')
  const mutated = original.split('\n')
  mutated.splice(t.i, 1)
  const mutatedSrc = mutated.join('\n')
  done++
  if (!parses(mutatedSrc)) {
    caughtParse++
    console.log(`[${done}/${active.length}] caught (parse)  L${t.i + 1}`)
    continue
  }
  let verdict
  restore = { file: t.file, original, mutated: mutatedSrc }
  try {
    writeFileSync(t.file, mutatedSrc)
    verdict = run()
  } finally {
    safeRestore(restore)
    restore = null
  }
  if (verdict === 'green') {
    survivors.push(t)
    console.log(`[${done}/${active.length}] SURVIVED  L${t.i + 1}: ${t.line.trim().slice(0, 90)}`)
  } else if (verdict === 'caught-collect') {
    caughtCollect++
    console.log(`[${done}/${active.length}] caught (collect) L${t.i + 1}`)
  } else {
    caughtTest++
    console.log(`[${done}/${active.length}] caught (test)   L${t.i + 1}`)
  }
}

// Parse-caught mutants are guaranteed by the tsc push/CI gate, not by the
// pins — report the split so the headline number cannot launder one as the
// other (a net that is mostly parse-caught has few real teeth).
console.log(`\n=== mutation report: ${caughtParse + caughtTest + caughtCollect}/${active.length} caught (${caughtTest} by tests, ${caughtParse} by parse/tsc, ${caughtCollect} by collection failure), ${survivors.length} survived, ${targets.length - active.length} scoped out ===`)
if (survivors.length) {
  console.log('SURVIVORS (add a pin or scope out with a reason):')
  for (const s of survivors) console.log(`  L${s.i + 1}: ${s.line.trim()}`)
  process.exit(1)
}
console.log('net verified: every in-scope line deletion turns at least one test red')
