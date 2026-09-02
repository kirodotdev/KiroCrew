/**
 * `.github/scripts/frontend-blob-reconcile.mjs` — the diagnostic the
 * frontend-coverage-merge CI job runs to name failing tests when a shard or
 * the merge step exits non-zero without saying why.
 *
 * The coupling this locks (issue #3496): the reconcile script parses vitest's
 * PRIVATE blob wire format — the flatted-encoded tuple
 * `[version, files, unhandledErrors, coverage, executionTime,
 * environmentModules]` and the `blob-<index>-<count>.json` shard file naming.
 * Neither is a public vitest API, so a vitest upgrade can shift either without
 * any type error. Because the script deliberately warns-and-continues on parse
 * failures (it must never turn a green merge job red), such a shift would
 * degrade the diagnostic SILENTLY: warnings in a log nobody reads, an empty
 * scan, or — worst — a real test failure mislabeled as a runner-level exit.
 *
 * So this test does not use a checked-in fixture: it runs the INSTALLED
 * vitest's own blob reporter on a tiny deliberately-failing probe test, then
 * feeds the produced blob through the reconcile script and asserts the failing
 * test is named. A checked-in fixture would keep passing across a vitest bump
 * (the old bytes still parse); generating the blob with the version under
 * `node_modules` is what makes the coupling break loudly ON the upgrade PR:
 *
 *  (1) the reporter still writes `blob-<index>-<count>.json` — the name the
 *      script's shard-completeness regex parses;
 *  (2) the script extracts the failing test's COMPOSED full name (the probe
 *      lives inside a `describe`, so `fullName` and `name` are distinguishable
 *      and a vitest change that drops `fullName` fails the match) — which
 *      requires the flatted tuple position of `files` and the task-tree shape
 *      (`tasks[]`, `result.state`, `result.errors[].message`) to all hold;
 *  (3) the script exits 0 — it is diagnostic-only and must never gate.
 *
 * Driven through real `node` child processes, not imports: the blob reporter
 * only exists behind the vitest CLI, and the reconcile script resolves
 * `flatted` from its invoking cwd (website/) by design — spawning exercises
 * both exactly as CI uses them.
 */
import { describe, it, expect } from 'vitest'
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

// `__dirname`, not `import.meta.url`: the runner's transform rewrites the
// latter to a non-`file:` URL. Same resolution as serveDist.routes.test.ts.
const WEBSITE = resolve(__dirname, '..', '..')
const RECONCILE = resolve(WEBSITE, '..', '.github', 'scripts', 'frontend-blob-reconcile.mjs')
const VITEST_CLI = resolve(WEBSITE, 'node_modules', 'vitest', 'vitest.mjs')

const PROBE_SUITE_NAME = 'wire-format probe'
const PROBE_TEST_NAME = 'deliberately fails'
// What the reconcile script must print: the COMPOSED full name, proving the
// blob still carries `fullName` (a top-level probe would make `fullName` and
// `name` identical and the pin vacuous for suite-qualified output).
const PROBE_FULL_NAME = `${PROBE_SUITE_NAME} > ${PROBE_TEST_NAME}`

/**
 * Child env for the nested runs. `VITEST*` keys are dropped so the nested CLI
 * cannot mistake itself for one of this run's workers. `NODE_V8_COVERAGE` is
 * dropped as defence-in-depth: vitest 4.1.x's v8 provider collects via the
 * inspector and does not set it, but a provider or config that DOES would make
 * an inheriting child write the probe run's coverage into this run's report.
 * `NODE_OPTIONS` caps the nested processes' heap — this test runs inside a
 * shard whose workers are deliberately memory-capped (see the worker-OOM note
 * in vite.config.ts), so the children must not be the thing that trips it.
 */
function childEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {}
  for (const [k, v] of Object.entries(process.env)) {
    if (k === 'NODE_V8_COVERAGE' || k.startsWith('VITEST')) continue
    env[k] = v
  }
  env.NODE_OPTIONS = '--max-old-space-size=512'
  return env
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

describe('frontend-blob-reconcile pins the vitest blob wire format', () => {
  it(
    'names a failing test from a blob the installed vitest reporter produced',
    () => {
      expect(
        existsSync(RECONCILE),
        `reconcile script missing at ${RECONCILE} — this test and the ci.yml `
        + 'reconcile step pin the same path; change both together',
      ).toBe(true)

      const probeDir = mkdtempSync(join(tmpdir(), 'blob-wire-probe-'))
      try {
        // One tiny test that MUST fail, so the blob records a failing task.
        // Inside a describe so the blob's `fullName` differs from `name`.
        writeFileSync(
          join(probeDir, 'probe.test.mjs'),
          "import { describe, test, expect } from 'vitest'\n"
          + `describe(${JSON.stringify(PROBE_SUITE_NAME)}, () => {\n`
          + `  test(${JSON.stringify(PROBE_TEST_NAME)}, () => { expect(1).toBe(2) })\n`
          + '})\n',
        )

        // (1) Produce the blob with the INSTALLED vitest — `--shard=1/1` makes
        // the reporter use the sharded file naming CI produces; one bounded
        // fork keeps the nested run inside this shard's memory budget. Exit
        // code 1 is the expected outcome (the probe fails by design); any
        // other non-zero status is a real error and is rethrown.
        let childOut = ''
        try {
          childOut = execFileSync(
            process.execPath,
            [VITEST_CLI, 'run', '--root', probeDir, '--reporter=blob',
              '--shard=1/1', '--pool=forks', '--maxWorkers=1'],
            { cwd: WEBSITE, env: childEnv(), encoding: 'utf8', timeout: 120_000 },
          )
          expect.unreachable('the probe test is built to fail, but the nested vitest run exited 0')
        } catch (err) {
          const e = err as { status?: number; stdout?: string; stderr?: string }
          if (e.status !== 1) throw err
          childOut = `${e.stdout ?? ''}\n${e.stderr ?? ''}`
        }

        // A status-1 exit is also what a flag rejection or "no test files
        // found" produces — in those cases no blob dir exists. Surface the
        // child's own output instead of a bare ENOENT from readdirSync.
        const blobDir = join(probeDir, '.vitest-reports')
        expect(
          existsSync(blobDir),
          `nested vitest exited 1 but wrote no blob dir; its output:\n${childOut}`,
        ).toBe(true)

        // Shard file naming: the script's shard-completeness detection parses
        // `blob-<index>-<count>.json`; a rename breaks that silently.
        expect(readdirSync(blobDir)).toEqual(['blob-1-1.json'])

        // (2)+(3) Feed the blob through the reconcile script from website/
        // (where `flatted` resolves). execFileSync throwing here would mean a
        // non-zero exit — a breach of the script's never-gates contract.
        const out = execFileSync(
          process.execPath,
          [RECONCILE, blobDir],
          {
            cwd: WEBSITE,
            env: { ...childEnv(), FRONTEND_TEST_RESULT: 'failure', MERGE_RESULT: '' },
            encoding: 'utf8',
            timeout: 60_000,
          },
        )

        // The parse must have succeeded (not warned-and-skipped)…
        expect(out).toContain('scanned 1 blob(s)')
        // …and the failing test must be named by its composed FULL name on a
        // FAIL line (file name > suite > test would also match — the anchor is
        // the suite-qualified tail, which a bare leaf `name` cannot produce).
        expect(out).toMatch(
          new RegExp(`^\\s*FAIL .*${escapeRegExp(PROBE_FULL_NAME)}`, 'm'),
        )
      } finally {
        rmSync(probeDir, { recursive: true, force: true })
      }
    },
    // Headroom above the summed child timeouts (120s + 60s), so a slow child
    // surfaces as its own timeout error, not this test's.
    240_000,
  )
})
