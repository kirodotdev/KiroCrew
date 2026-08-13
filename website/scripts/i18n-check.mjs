#!/usr/bin/env node
/**
 * The i18n gate chain — run ALL of it, then report once.
 *
 * ## Why a runner and not `a && b && c`
 *
 * `package.json` used to chain six scripts with `&&`, which short-circuits: the first
 * failure hides the other ten checks, so an author fixes one, pushes, waits ~5
 * minutes for `frontend-lint`, and discovers the next. Eleven checks means up to eleven
 * rounds for one PR. They are independent measurements over the same tree; there is
 * no reason to learn them one at a time.
 *
 * ## What the output is organised around
 *
 * One question: **is this mine to fix?** A `diff`-scoped finding is on a line this
 * branch wrote or in a file it touched — there is no number to raise and no one else
 * to wait for. A `repo`-scoped finding is a whole-repo measurement the branch may
 * simply have inherited. That distinction was previously unstated, and it is exactly
 * what cost other people's PRs in both the `PrivacyPanel` and `projects.latent`
 * incidents: eleven PRs went red on a gate none of their diffs had tripped, and the
 * log gave no way to tell.
 *
 * The `enforce` column is the other half of it, because a reader cannot otherwise
 * know that one of these checks fails when a number goes DOWN.
 *
 * ## Where the verdict lives
 *
 * Not here. `lib/i18n-gate-table.mjs` decides it as a pure function over plain data,
 * because the first version of this file got the exit code wrong in a way nothing
 * could test: a script that crashed before printing anything recognisable had all its
 * rows classified as unknowns, and the run exited 0 where the `&&` chain exited 2.
 * The invariant now under unit test is that a non-zero child always fails.
 *
 * Exit codes: 0 all clear · 1 at least one check failed · 2 a script could not be run.
 */
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import {
  CHECKS,
  ENFORCE_LABEL,
  SCRIPTS,
  resolveRows,
  verdict,
} from './lib/i18n-gate-table.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const CI = !!process.env.GITHUB_ACTIONS
const BASE = process.env.I18N_BASE_REF || ''

const out = line => process.stdout.write(`${line}\n`)
const group = (title, body) => {
  if (!body.trim()) return
  out(CI ? `::group::${title}` : `\n--- ${title}`)
  out(body.replace(/\s+$/, ''))
  if (CI) out('::endgroup::')
}

// ---- run everything, keep every byte ---------------------------------------
const runs = new Map()
for (const s of SCRIPTS) {
  const res = spawnSync(process.execPath, [join(HERE, s.argv[0]), ...s.argv.slice(1)], {
    cwd: join(HERE, '..'), encoding: 'utf-8', env: process.env, maxBuffer: 256 * 1024 * 1024,
  })
  // A script that could not be started, was killed by a signal, or overflowed the
  // buffer is an INFRASTRUCTURE failure, not a finding: exit 2 rather than reporting a
  // table built from truncated text that a regex might read as a pass.
  if (res.error || res.status === null) {
    out(`[i18n] FATAL — ${s.argv[0]} could not run: ${res.error?.message || `killed by ${res.signal}`}`)
    process.exit(2)
  }
  runs.set(s.key, { status: res.status, text: `${res.stdout || ''}${res.stderr || ''}` })
}

const rows = resolveRows({ checks: CHECKS, runs, base: BASE })
const { bad, unknown, unexplained, failed } = verdict({ rows, runs, scripts: SCRIPTS })
const fileOf = key => SCRIPTS.find(s => s.key === key).argv[0]

// ---- report ----------------------------------------------------------------
const shown = bad.length + unexplained.length
const verdictLine = failed
  ? `FAIL ${shown}${unknown.length ? ` · ${unknown.length} not reached` : ''}`
  : 'PASS'
out(`\n[i18n] ${rows.length} checks · ${verdictLine}`
  + `${BASE ? ` · base ${BASE}` : ' · NO BASE (diff-scoped checks not run)'}\n`)

const pad = Math.max(...rows.map(r => r.id.length)) + 2
const sumWidth = Math.max(...rows.map(r => r.summary.length)) + 2
const section = (title, pick) => {
  const picked = rows.filter(pick)
  if (!picked.length) return
  out(`  ${title}`)
  for (const r of picked) {
    out(`    ${r.state.padEnd(8)}[${r.id}]`.padEnd(pad + 14)
      + `${r.summary.padEnd(sumWidth)}${ENFORCE_LABEL[r.enforce]}`)
  }
  out('')
}
// Three sections, because "can this fail" is now a different question from "is this
// mine". Only a diff-scoped check and a whole-repo HARD ZERO can fail the step: a hard
// zero has no ceiling, so a new one is always somebody's diff. Every other whole-repo
// number is a stored total that another branch can move without touching your files, and
// those are reported.
section('YOURS — diff-scoped, fails on any finding', r => r.scope === 'diff')
section('WHOLE REPO, ENFORCED — no ceiling to inherit', r => r.scope === 'repo' && r.enforce === 'hard-zero')
section('WHOLE REPO, REPORT ONLY — cannot fail this step', r => r.scope === 'repo' && r.enforce === 'info')

const ceilings = rows.filter(r => r.enforce === 'info' && r.note)
for (const r of ceilings) out(`  [${r.id}] ${r.summary} — ${r.note}`)
if (ceilings.length) out('')

// ---- what to do about each failure ----------------------------------------
for (const r of bad) {
  if (r.state === 'MISSING') {
    out(`  -> [${r.id}] printed nothing this runner recognises, yet its script PASSED.`)
    out('     That is the shape of a check that quietly stopped measuring, so it fails')
    out(`     the step deliberately. See the ${fileOf(r.script)} group below.\n`)
  } else if (r.scope === 'diff') {
    out(`  -> [${r.id}] is YOURS: ${r.summary}. There is no number to raise —`)
    out(`     fix the sites listed in the ${fileOf(r.script)} group below.\n`)
  } else if (r.enforce === 'hard-zero') {
    // A hard zero is an absolute over the whole repo, so it is normally the branch's
    // own doing — you added a reference to a key that does not exist. Do not offer the
    // inheritance excuse here; it would send the author looking for someone else.
    out(`  -> [${r.id}] is whole-repo but a HARD ZERO, so there is no ceiling and no`)
    out('     inherited number to blame — a new one almost always comes from this diff.')
    out(`     The offending sites are in the ${fileOf(r.script)} group below.\n`)
  } else {
    out(`  -> [${r.id}] is a STORED COUNT (${ENFORCE_LABEL[r.enforce]}), so your diff may not`)
    out('     have caused it: another branch can move this number without touching your')
    out('     files. Read the group below before changing code. Tracked in #1004.\n')
  }
}
for (const s of unexplained) {
  out(`  -> ${s.argv[0]} exited ${runs.get(s.key).status} and NO check could say why.`)
  out('     It failed before printing anything attributable — a crash, an unreadable')
  out('     config, or a base ref it could not resolve. This fails the step: a gate')
  out('     that cannot run must fail, not pass. Its full output is in the group below.\n')
}
for (const r of unknown) {
  if (!BASE && r.scope === 'diff') continue // already stated in the header
  out(`  -> [${r.id}] was NOT REACHED: ${fileOf(r.script)} exited on an earlier check, so`)
  out('     this one has no verdict this run. Fix the failure above and it gets measured.\n')
}

// ---- the detail, collapsed ------------------------------------------------
for (const s of SCRIPTS) {
  const run = runs.get(s.key)
  const tag = rows.filter(r => r.script === s.key).map(r => r.id).join(', ')
  group(`[i18n] ${s.argv[0]} (${tag}) — ${run.status === 0 ? 'ok' : `exit ${run.status}`}`, run.text)
}

process.exit(failed ? 1 : 0)
