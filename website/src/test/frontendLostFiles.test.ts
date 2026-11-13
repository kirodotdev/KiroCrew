/**
 * `.github/scripts/frontend-lost-files.mjs`: the frontend-test shard step's
 * recovery for a test file a Vitest fork lost by exiting before the file
 * started (website/docs/testing.md, "CI gates").
 *
 * Two layers:
 *
 *  - `planRecovery` / `finalizeRecovery` on hand-built manifests and blob
 *    tuples: every way the failure can be something OTHER than "a fork died
 *    before its file started" must refuse, and so must every re-run that did
 *    not pass, ran other files, or collected coverage differently.
 *
 *  - The whole loop against the INSTALLED Vitest: a probe shard whose fork is
 *    SIGKILLed when the pool hands it one file (after the worker started,
 *    before the file is queued, the point the CI forks died at), the manifest
 *    the reporter writes, `plan`, a re-run, `finalize`, and
 *    `vitest --merge-reports`. It pins the private shapes the script relies on
 *    (the pool error text, the blob tuple, the file missing from every count)
 *    so a Vitest upgrade that moves them fails here, and it proves the merge
 *    still fails when the re-run fails or `finalize` refuses.
 */
import { describe, it, expect } from 'vitest'
import { spawnSync } from 'node:child_process'
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { stripVTControlCharacters } from 'node:util'
import {
  MAX_RECOVERABLE_FILES,
  finalizeRecovery,
  planRecovery,
} from '../../../.github/scripts/frontend-lost-files.mjs'

// `__dirname`, not `import.meta.url`: the runner's transform rewrites the
// latter to a non-`file:` URL. Same resolution as the blob wire-format test.
const WEBSITE = resolve(__dirname, '..', '..')
const LOST_FILES = resolve(WEBSITE, '..', '.github', 'scripts', 'frontend-lost-files.mjs')
const VITEST_CLI = resolve(WEBSITE, 'node_modules', 'vitest', 'vitest.mjs')

const POOL_ERROR = '[vitest-pool]: Worker forks emitted error.'
const WORKER_EXIT = { message: POOL_ERROR, cause: 'Worker exited unexpectedly' }
const ROOT = '/work/website'

interface ManifestModule { file: string, state: string }
interface ManifestError { message: string | null, cause: string | null }
interface Manifest {
  version: number
  vitestVersion: string
  root: string
  shard: { index: number, count: number } | null
  projects: { name: string, pool: string, isolate: boolean }[]
  coverageThresholds: boolean
  reason: string
  expected: string[]
  reported: ManifestModule[]
  errors: ManifestError[]
}
interface BlobTask {
  type: string
  name: string
  filepath?: string
  result?: { state: string }
  tasks?: BlobTask[]
}
type Report = [string, BlobTask[], unknown[], unknown, number, Record<string, unknown>]

function manifest(overrides: Partial<Manifest> = {}): Manifest {
  return {
    version: 1,
    vitestVersion: '4.1.12',
    root: ROOT,
    shard: { index: 3, count: 4 },
    projects: [{ name: '', pool: 'forks', isolate: true }],
    coverageThresholds: false,
    reason: 'passed',
    expected: ['src/a.test.ts', 'src/b.test.tsx', 'src/lost.test.ts'],
    reported: [
      { file: 'src/a.test.ts', state: 'passed' },
      { file: 'src/b.test.tsx', state: 'skipped' },
    ],
    errors: [WORKER_EXIT],
    ...overrides,
  }
}

function blobFile(name: string, testState = 'pass'): BlobTask {
  return {
    type: 'suite',
    name,
    filepath: `${ROOT}/${name}`,
    result: { state: testState === 'fail' ? 'fail' : 'pass' },
    tasks: [{ type: 'test', name: 'case', result: { state: testState } }],
  }
}

// What the blob reporter really writes for the pool's Error: flatted keeps only
// enumerable properties, and an Error's message and cause are not.
const shardReport = (): Report => ['4.1.12', [blobFile('src/a.test.ts'), blobFile('src/b.test.tsx')],
  [{ type: 'Unhandled Error' }], { coverageMap: true }, 1, {}]
const rerunReport = (files = [blobFile('src/lost.test.ts')], errors: unknown[] = [], coverage: unknown = { coverageMap: true }): Report =>
  ['4.1.12', files, errors, coverage, 1, {}]

describe('planRecovery', () => {
  it('names the file a fork never started when that is the whole failure', () => {
    expect(planRecovery(manifest())).toMatchObject({ recoverable: true, lost: ['src/lost.test.ts'] })
  })

  it('accepts the later-major cause that adds the exit code, signal and file', () => {
    const cause = 'Worker exited unexpectedly with signal SIGKILL during started state while running test file /w/src/lost.test.ts'
    expect(planRecovery(manifest({ errors: [{ message: POOL_ERROR, cause }] }))).toMatchObject({ recoverable: true })
  })

  const tooMany = MAX_RECOVERABLE_FILES + 1
  const lostMany = Array.from({ length: tooMany }, (_, i) => `src/lost${i}.test.ts`)

  it.each<[string, Manifest | null, string]>([
    ['no manifest', null, 'no run manifest'],
    ['no unhandled error', manifest({ errors: [] }), 'no unhandled error'],
    ['another unhandled error beside the exit', manifest({ errors: [WORKER_EXIT, { message: 'boom', cause: null }] }), 'not a worker exit: boom'],
    ['a pipe error instead of an exit', manifest({ errors: [{ message: POOL_ERROR, cause: 'write EPIPE' }] }), 'not a worker exit'],
    ['a failing file', manifest({ reported: [{ file: 'src/a.test.ts', state: 'failed' }] }), 'is "failed"'],
    ['a file whose worker died after it started', manifest({ reported: [{ file: 'src/a.test.ts', state: 'pending' }] }), 'is "pending"'],
    ['a run Vitest judged failed', manifest({ reason: 'failed' }), 'judged the run "failed"'],
    ['a reported file outside the shard', manifest({ reported: [{ file: 'src/elsewhere.test.ts', state: 'passed' }] }), 'not in the shard'],
    ['more exits than lost files', manifest({ errors: [WORKER_EXIT, WORKER_EXIT] }), '1 file(s) never reported for 2 worker exit(s)'],
    ['too many lost files', manifest({ expected: lostMany, reported: [], errors: lostMany.map(() => WORKER_EXIT) }), `more than ${MAX_RECOVERABLE_FILES}`],
    ['a threads pool', manifest({ projects: [{ name: '', pool: 'threads', isolate: true }] }), 'needs isolated forks'],
    ['a non-isolated pool', manifest({ projects: [{ name: '', pool: 'forks', isolate: false }] }), 'needs isolated forks'],
    ['two projects', manifest({ projects: [{ name: 'a', pool: 'forks', isolate: true }, { name: 'b', pool: 'forks', isolate: true }] }), '2 projects'],
    ['a coverage threshold', manifest({ coverageThresholds: true }), 'coverage threshold'],
  ])('refuses %s', (_label, input, reason) => {
    const plan = planRecovery(input)
    expect(plan.recoverable).toBe(false)
    expect(plan.lost).toEqual([])
    expect(plan.reason).toContain(reason)
  })
})

describe('finalizeRecovery', () => {
  it('drops the worker-exit errors from the shard blob once the re-run passed', () => {
    const shard = shardReport()
    const before = JSON.stringify(shard)
    const result = finalizeRecovery(manifest(), shard, rerunReport())
    expect(result).toMatchObject({ ok: true, lost: ['src/lost.test.ts'] })
    const [version, files, errors, coverage] = result.report as Report
    expect(errors).toEqual([])
    expect([version, files, coverage]).toEqual([shard[0], shard[1], shard[3]])
    expect(JSON.stringify(shard), 'the input blob must not be mutated').toBe(before)
  })

  it.each<[string, Manifest, Report, Report, string]>([
    ['the re-run failed', manifest(), shardReport(), rerunReport([blobFile('src/lost.test.ts', 'fail')]), 'the re-run of src/lost.test.ts failed'],
    ['the re-run recorded an unhandled error', manifest(), shardReport(), rerunReport(undefined, [{ type: 'Unhandled Error' }]), '1 unhandled error'],
    ['the re-run also ran a sibling the filter matched', manifest(), shardReport(),
      rerunReport([blobFile('src/lost.test.ts'), blobFile('src/lost.test.tsx')]), 'not exactly the lost'],
    ['the re-run ran nothing', manifest(), shardReport(), rerunReport([]), 'not exactly the lost'],
    // A blob written without --coverage parses back with `null` in that slot.
    ['the re-run dropped coverage the shard collected', manifest(), shardReport(), rerunReport(undefined, [], null), 'coverage exactly when'],
    ['the shard blob holds a different error', manifest(), ['4.1.12', shardReport()[1], [{ message: 'boom' }], {}, 1, {}],
      rerunReport(), 'do not line up'],
    ['the shard blob holds more errors than the manifest', manifest(), ['4.1.12', shardReport()[1], [{}, {}], {}, 1, {}],
      rerunReport(), 'do not line up'],
    ['the shard blob is from another run', manifest(), ['4.1.12', [blobFile('src/a.test.ts')], [{}], {}, 1, {}],
      rerunReport(), 'does not hold the files'],
    ['the blobs come from another Vitest', manifest(), shardReport(), ['4.2.0', [blobFile('src/lost.test.ts')], [], {}, 1, {}],
      'written by Vitest 4.1.12 and 4.2.0'],
    ['the plan itself refuses', manifest({ errors: [{ message: 'boom', cause: null }] }), shardReport(), rerunReport(), 'not a worker exit'],
  ])('refuses when %s', (_label, input, shard, rerun, reason) => {
    const result = finalizeRecovery(input, shard, rerun)
    expect(result.ok).toBe(false)
    expect(result.reason).toContain(reason)
  })
})

/**
 * Child env for the nested Vitest runs: no `VITEST*` keys (so a nested CLI
 * cannot mistake itself for one of this run's workers), no inherited
 * `NODE_V8_COVERAGE` or manifest path, and a heap cap so the children stay
 * inside this shard's memory budget.
 */
function childEnv(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
  const kept = Object.entries(process.env).filter(([key]) =>
    key !== 'NODE_V8_COVERAGE' && key !== 'FRONTEND_RUN_MANIFEST' && !key.startsWith('VITEST')
    && !key.startsWith('PROBE_'))
  return { ...Object.fromEntries(kept), NODE_OPTIONS: '--max-old-space-size=512', ...extra }
}

function node(args: string[], env: NodeJS.ProcessEnv): { status: number | null, out: string } {
  const child = spawnSync(process.execPath, args, { cwd: WEBSITE, env, encoding: 'utf8', timeout: 120_000 })
  if (child.error) throw child.error
  // Vitest colors its summary when it runs under CI, which puts escape codes
  // between "Test Files" and the counts; match on the plain text.
  return { status: child.status, out: stripVTControlCharacters(`${child.stdout}\n${child.stderr}`) }
}

describe('frontend-lost-files against the installed Vitest', () => {
  it(
    're-runs only the file a dead fork never started, and the merge stays red unless that re-run passed',
    () => {
      expect(existsSync(LOST_FILES), `script missing at ${LOST_FILES}; ci.yml and this test pin the same path`).toBe(true)
      const probe = realpathSync(mkdtempSync(join(tmpdir(), 'lost-files-probe-')))
      try {
        const passing = "import { test, expect } from 'vitest'\ntest('passes', () => { expect(1).toBe(1) })\n"
        writeFileSync(join(probe, 'ok-a.test.mjs'), passing)
        writeFileSync(join(probe, 'ok-b.test.mjs'), passing)
        writeFileSync(
          join(probe, 'lost.test.mjs'),
          "import { test, expect } from 'vitest'\n"
          + "test('passes once its fork survives', () => { expect(process.env.PROBE_FAIL).not.toBe('1') })\n",
        )
        // Registered before Vitest's own listener, so the fork dies on the
        // "run" request for lost.test.mjs: started, but the file not yet queued.
        const preload = join(probe, 'die-on-run.cjs')
        writeFileSync(
          preload,
          "process.on('message', (m) => {\n"
          + "  const files = process.env.PROBE_DIE === '1' && m && m.type === 'run' && m.context && m.context.files\n"
          + "  if (Array.isArray(files) && files.some((f) => String(f.filepath).endsWith('lost.test.mjs'))) process.kill(process.pid, 'SIGKILL')\n"
          + '})\n',
        )
        const manifestPath = join(probe, 'manifest.json')
        const reports = join(probe, '.vitest-reports')

        const shard = node(
          [VITEST_CLI, 'run', '--root', probe, '--pool=forks', '--maxWorkers=1', '--shard=1/1',
            '--reporter=blob', '--reporter=default', `--reporter=${LOST_FILES}`],
          childEnv({
            PROBE_DIE: '1',
            FRONTEND_RUN_MANIFEST: manifestPath,
            NODE_OPTIONS: `--max-old-space-size=512 --require ${preload}`,
          }),
        )
        expect(shard.status, shard.out).toBe(1)
        // The CI shape: both reported files pass, the lost one is in no count,
        // and the only error is the pool's worker exit.
        expect(shard.out).toContain(POOL_ERROR)
        expect(shard.out).toContain('Worker exited unexpectedly')
        expect(shard.out).toMatch(/Test Files\s+2 passed \(2\)/)

        const runManifest = JSON.parse(readFileSync(manifestPath, 'utf8')) as Manifest
        expect(runManifest).toMatchObject({
          shard: { index: 1, count: 1 },
          projects: [{ pool: 'forks', isolate: true }],
          reason: 'passed',
          expected: ['lost.test.mjs', 'ok-a.test.mjs', 'ok-b.test.mjs'],
          errors: [WORKER_EXIT],
        })
        expect(runManifest.reported.map((m) => [m.file, m.state]).sort())
          .toEqual([['ok-a.test.mjs', 'passed'], ['ok-b.test.mjs', 'passed']])

        // Why the manifest exists: the blob keeps the pool error with no message.
        const { parse } = createRequire(join(WEBSITE, 'noop.js'))('flatted') as { parse: (s: string) => Report }
        const shardBlob = parse(readFileSync(join(reports, 'blob-1-1.json'), 'utf8'))
        expect(shardBlob[2]).toEqual([{ type: 'Unhandled Error' }])

        const plan = node([LOST_FILES, 'plan', manifestPath], childEnv())
        expect(plan.status, plan.out).toBe(0)
        expect(plan.out.split('\n')[0]).toBe('lost.test.mjs')

        // Three copies of the shard's blobs: re-run passes, re-run fails, and a
        // passing re-run whose shard blob `finalize` never rewrote.
        const scenario = (name: string) => {
          const dir = join(probe, name)
          mkdirSync(dir)
          cpSync(join(reports, 'blob-1-1.json'), join(dir, 'blob-1-1.json'))
          return dir
        }
        const rerun = (dir: string, extra: Record<string, string> = {}) => node(
          [VITEST_CLI, 'run', '--root', probe, '--pool=forks', '--maxWorkers=1', '--reporter=blob',
            `--outputFile.blob=${join(dir, 'blob-1-1.recovery.json')}`, 'lost.test.mjs'],
          childEnv(extra),
        )
        const finalize = (dir: string) => node(
          [LOST_FILES, 'finalize', manifestPath, join(dir, 'blob-1-1.json'), join(dir, 'blob-1-1.recovery.json')],
          childEnv(),
        )
        const merge = (dir: string) => node([VITEST_CLI, 'run', '--root', probe, `--merge-reports=${dir}`], childEnv())

        const passed = scenario('passed')
        expect(rerun(passed).status).toBe(0)
        const done = finalize(passed)
        expect(done.status, done.out).toBe(0)
        expect(done.out).toContain('::warning::')
        const green = merge(passed)
        expect(green.status, green.out).toBe(0)
        expect(green.out).toMatch(/Test Files\s+3 passed \(3\)/)

        const failed = scenario('failed')
        expect(rerun(failed, { PROBE_FAIL: '1' }).status).toBe(1)
        const untouched = readFileSync(join(failed, 'blob-1-1.json'), 'utf8')
        const refused = finalize(failed)
        expect(refused.status, refused.out).toBe(1)
        expect(refused.out).toContain('the re-run of lost.test.mjs failed')
        expect(readFileSync(join(failed, 'blob-1-1.json'), 'utf8')).toBe(untouched)
        expect(merge(failed).status).toBe(1)

        const unfinalized = scenario('unfinalized')
        expect(rerun(unfinalized).status).toBe(0)
        const red = merge(unfinalized)
        expect(red.status, 'the shard blob\'s worker-exit error alone must fail the merge').toBe(1)
      } finally {
        rmSync(probe, { recursive: true, force: true })
      }
    },
    // Six nested Vitest runs and three script runs, each bounded by node()'s
    // own timeout; this only has to outlast their usual cost.
    240_000,
  )
})
