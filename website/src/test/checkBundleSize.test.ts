import { describe, it, expect, afterEach } from 'vitest'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { logicalChunkName, checkChunkBudgets } from '../../scripts/lib/bundleReport.mjs'
import { CHUNK_BUDGETS, DEFAULT_BUDGET_BYTES } from '../../scripts/check-bundle-size.mjs'

const KB = 1024

/** A minimal version-1 report with the given chunks, as the plugin emits it. */
function report(chunks: Array<{ fileName: string; size: number }>) {
  return {
    version: 1,
    generatedAt: 'T',
    totals: {
      chunkBytes: chunks.reduce((a, c) => a + c.size, 0),
      assetBytes: 0,
      chunkCount: chunks.length,
      assetCount: 0,
    },
    chunks: chunks.map((c) => ({ ...c, moduleCount: 1, isEntry: false, isDynamicEntry: false })),
    assets: [],
    owners: [],
  }
}

describe('logicalChunkName', () => {
  it('strips the assets prefix and the content hash', () => {
    expect(logicalChunkName('assets/main-CZ3WY91T.js')).toBe('main')
    expect(logicalChunkName('assets/t-BiAnPtAI.js')).toBe('t')
  })

  it('keeps dots and inner dashes that are part of the logical name', () => {
    expect(logicalChunkName('assets/editor.api2-Cz39VSsw.js')).toBe('editor.api2')
    expect(logicalChunkName('assets/vendor-markdown-UXZ1DEem.js')).toBe('vendor-markdown')
    // Mermaid's prebuilt chunks carry their own name segment before the hash.
    expect(logicalChunkName('assets/chunk-KEIR6QF5-BICK3FdT.js')).toBe('chunk-KEIR6QF5')
  })

  it('falls back to the plain basename when there is no hash', () => {
    expect(logicalChunkName('main.js')).toBe('main')
    expect(logicalChunkName('assets/short-ab.js')).toBe('short-ab')
  })

  it('handles junk input', () => {
    expect(logicalChunkName('')).toBe('')
    expect(logicalChunkName(undefined as unknown as string)).toBe('')
  })
})

describe('checkChunkBudgets', () => {
  it('fails an over-budget chunk with size, budget, and overage', () => {
    const r = report([{ fileName: 'assets/new-thing-AAAAAAAA.js', size: 600 * KB }])
    const { breaches } = checkChunkBudgets(r, { budgets: {}, defaultBudget: 500 * KB })
    expect(breaches).toHaveLength(1)
    expect(breaches[0]).toMatchObject({
      fileName: 'assets/new-thing-AAAAAAAA.js',
      logicalName: 'new-thing',
      size: 600 * KB,
      budget: 500 * KB,
      overage: 100 * KB,
    })
  })

  it('passes a report whose chunks are all within budget', () => {
    const r = report([
      { fileName: 'assets/a-AAAAAAAA.js', size: 100 * KB },
      { fileName: 'assets/b-BBBBBBBB.js', size: 499 * KB },
    ])
    const { breaches, checkedCount } = checkChunkBudgets(r, { budgets: {}, defaultBudget: 500 * KB })
    expect(breaches).toEqual([])
    expect(checkedCount).toBe(2)
  })

  it('lets an allowlisted chunk exceed the default but not its own ceiling', () => {
    const budgets = { big: 1000 * KB }
    const under = report([{ fileName: 'assets/big-AAAAAAAA.js', size: 900 * KB }])
    expect(checkChunkBudgets(under, { budgets, defaultBudget: 500 * KB }).breaches).toEqual([])

    const over = report([{ fileName: 'assets/big-AAAAAAAA.js', size: 1100 * KB }])
    const { breaches } = checkChunkBudgets(over, { budgets, defaultBudget: 500 * KB })
    expect(breaches).toHaveLength(1)
    // The breach is judged against the chunk's OWN ceiling, not the default.
    expect(breaches[0].budget).toBe(1000 * KB)
  })

  it('sorts breaches by overage so the worst offender is first', () => {
    const r = report([
      { fileName: 'assets/small-AAAAAAAA.js', size: 600 * KB },
      { fileName: 'assets/huge-BBBBBBBB.js', size: 2000 * KB },
    ])
    const { breaches } = checkChunkBudgets(r, { budgets: {}, defaultBudget: 500 * KB })
    expect(breaches.map((b) => b.logicalName)).toEqual(['huge', 'small'])
  })

  it('reports allowlist entries that matched no chunk instead of silently keeping them', () => {
    const r = report([{ fileName: 'assets/a-AAAAAAAA.js', size: 10 * KB }])
    const { unusedBudgets } = checkChunkBudgets(r, {
      budgets: { gone: 1000 * KB, a: 600 * KB },
      defaultBudget: 500 * KB,
    })
    expect(unusedBudgets).toEqual(['gone'])
  })
})

describe('CHUNK_BUDGETS (the shipped allowlist)', () => {
  it('keys are logical names, never hashed file names or asset paths', () => {
    for (const name of Object.keys(CHUNK_BUDGETS)) {
      expect(name).not.toMatch(/^assets\//)
      expect(name).not.toMatch(/\.js$/)
      // A hashed name would silently stop matching on the next build.
      expect(logicalChunkName(`assets/${name}-DEADBEEF.js`)).toBe(name)
    }
  })

  it('every ceiling is above the default budget, or the entry is pointless', () => {
    for (const [name, ceiling] of Object.entries(CHUNK_BUDGETS)) {
      expect(ceiling, `ceiling for '${name}'`).toBeGreaterThan(DEFAULT_BUDGET_BYTES)
    }
  })
})

describe('check-bundle-size.mjs as a process (the CI entry point)', () => {
  // __dirname is src/test, so two levels up is the website root; every other
  // test in this folder resolves the same way.
  const scriptPath = path.resolve(__dirname, '..', '..', 'scripts', 'check-bundle-size.mjs')
  let dir: string

  afterEach(() => {
    if (dir) rmSync(dir, { recursive: true, force: true })
  })

  function run(reportBody: string | null) {
    dir = mkdtempSync(path.join(tmpdir(), 'bundle-gate-'))
    const file = path.join(dir, 'bundle-report.json')
    if (reportBody !== null) writeFileSync(file, reportBody)
    return spawnSync(process.execPath, [scriptPath, file], { encoding: 'utf-8' })
  }

  it('refuses a valid report that lists no chunks, rather than certifying it', () => {
    // The failure this closes is a GREEN one: with an empty chunk list the summary
    // read "0 chunks within budget" and exited 0, so a build that emitted nothing
    // measurable passed the gate that exists to measure it.
    const res = run(JSON.stringify({ version: 1, chunks: [] }))

    expect(res.status).toBe(4)
    expect(res.stderr).toContain('measured nothing')
    // Actionable: name the command that produces a real report.
    expect(res.stderr).toContain('--mode analyze')
    // The refusal must come BEFORE the unused-budget warnings, or it arrives
    // under one warning per allowlist entry and reads as noise.
    expect(res.stdout).not.toContain('within budget')
  })

  it('exits non-zero on an over-budget non-allowlisted chunk, naming size, budget and overage', () => {
    const res = run(JSON.stringify(report([{ fileName: 'assets/rogue-AAAAAAAA.js', size: 700 * KB }])))
    expect(res.status).toBe(1)
    expect(res.stderr).toContain('assets/rogue-AAAAAAAA.js')
    expect(res.stderr).toContain('700.0 KB')
    expect(res.stderr).toContain('500.0 KB')
    expect(res.stderr).toContain('200.0 KB')
  })

  it('exits zero on a report within budget, including allowlisted chunks over the default', () => {
    const res = run(
      JSON.stringify(
        report([
          { fileName: 'assets/tiny-AAAAAAAA.js', size: 10 * KB },
          // Over 500 KB but under its allowlisted ceiling.
          { fileName: 'assets/PierreImpl-Cz39VSsw.js', size: 550 * KB },
        ])
      )
    )
    expect(res.status).toBe(0)
    expect(res.stdout).toContain('within budget')
  })

  it('fails closed on a missing report rather than passing an unmeasured build', () => {
    const res = run(null)
    expect(res.status).toBe(2)
    expect(res.stderr).toContain('No bundle report')
  })

  it('refuses a report version it does not understand', () => {
    const res = run(JSON.stringify({ version: 2, chunks: [] }))
    expect(res.status).toBe(3)
  })
})
