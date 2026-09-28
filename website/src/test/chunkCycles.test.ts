import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { findChunkCycles } from '../../scripts/lib/bundleReport.mjs'

// The gate this covers catches a blank page, not a size regression: two chunks
// that statically import each other have no valid initialization order, so one
// body runs against a binding that is still uninitialized and the page dies
// before React mounts. It exists because a rolldown config edit
// (`includeDependenciesRecursively: false`) produced exactly that while every
// other gate stayed green -- bytes were fine, tsc was fine, and the unit suite
// never loads a built bundle.

/** A version-1 report carrying the given chunks and their static imports. */
function report(chunks: Record<string, string[]>) {
  const names = Object.keys(chunks)
  return {
    version: 1,
    generatedAt: 'T',
    totals: { chunkBytes: 0, assetBytes: 0, chunkCount: names.length, assetCount: 0 },
    chunks: names.map((fileName) => ({
      fileName,
      size: 1,
      moduleCount: 1,
      isEntry: false,
      isDynamicEntry: false,
      imports: chunks[fileName],
    })),
    assets: [],
    owners: [],
  }
}

describe('findChunkCycles', () => {
  it('reports nothing for an acyclic graph', () => {
    const r = findChunkCycles(
      report({
        'assets/main.js': ['assets/vendor-react.js', 'assets/App.js'],
        'assets/App.js': ['assets/vendor-react.js'],
        'assets/vendor-react.js': [],
      })
    )
    expect(r.cycles).toEqual([])
    expect(r.checkedCount).toBe(3)
    expect(r.edgeCount).toBe(3)
    expect(r.missingImports).toBe(false)
  })

  it('finds a two-chunk cycle', () => {
    const r = findChunkCycles(
      report({
        'assets/client.js': ['assets/vendor-react.js'],
        'assets/vendor-react.js': ['assets/client.js'],
      })
    )
    expect(r.cycles).toEqual([['assets/client.js', 'assets/vendor-react.js']])
  })

  it('finds the real shape this gate was written for', () => {
    // The 3-chunk cycle a rejected revision produced across the graph chunks,
    // which also broke the deliberate lazy-physics boundary.
    const r = findChunkCycles(
      report({
        'assets/vendor-graph.js': ['assets/louvain.js'],
        'assets/louvain.js': ['assets/vendor-graph-physics.js'],
        'assets/vendor-graph-physics.js': ['assets/vendor-graph.js', 'assets/louvain.js'],
        'assets/unrelated.js': [],
      })
    )
    expect(r.cycles).toHaveLength(1)
    expect(r.cycles[0]).toEqual([
      'assets/louvain.js',
      'assets/vendor-graph-physics.js',
      'assets/vendor-graph.js',
    ])
  })

  it('reports several cycles largest first', () => {
    const r = findChunkCycles(
      report({
        'assets/a.js': ['assets/b.js'],
        'assets/b.js': ['assets/c.js'],
        'assets/c.js': ['assets/a.js'],
        'assets/y.js': ['assets/z.js'],
        'assets/z.js': ['assets/y.js'],
      })
    )
    expect(r.cycles.map((c) => c.length)).toEqual([3, 2])
  })

  it('counts a self-import as a cycle', () => {
    const r = findChunkCycles(report({ 'assets/loop.js': ['assets/loop.js'] }))
    expect(r.cycles).toEqual([['assets/loop.js']])
  })

  // The three cases below are the ones that make a hand-written SCC walk report
  // ZERO cycles on a graph that has them -- a silently-passing gate, which is
  // worse than no gate. They were hand-traced in review; encoded here because a
  // hand-trace does not survive the next edit to this function.

  it('ignores a cross-edge into a component it has already finished', () => {
    // second-root reaches x AFTER the {x,y} component has been popped, so x is
    // visited but no longer on the stack. Mistaking that for a live edge would
    // fold both roots into one oversized "cycle".
    const r = findChunkCycles(
      report({
        'assets/first-root.js': ['assets/x.js'],
        'assets/x.js': ['assets/y.js'],
        'assets/y.js': ['assets/x.js'],
        'assets/second-root.js': ['assets/x.js'],
      })
    )
    expect(r.cycles).toEqual([['assets/x.js', 'assets/y.js']])
  })

  it('does not invent a cycle from a chunk two roots both import', () => {
    // A re-visited node is the normal shape of a shared leaf, not a cycle.
    const r = findChunkCycles(
      report({
        'assets/root-a.js': ['assets/shared.js'],
        'assets/root-b.js': ['assets/shared.js'],
        'assets/shared.js': [],
      })
    )
    expect(r.cycles).toEqual([])
    expect(r.edgeCount).toBe(2)
  })

  it('reports one component when a smaller cycle sits inside a larger one', () => {
    // n2 <-> n3 is a cycle on its own, and n4 -> n1 closes all four. Every node
    // reaches every other, so this is ONE component of four, not two of two:
    // splitting it would under-report what has to be broken to fix the graph.
    const r = findChunkCycles(
      report({
        'assets/n1.js': ['assets/n2.js'],
        'assets/n2.js': ['assets/n3.js'],
        'assets/n3.js': ['assets/n2.js', 'assets/n4.js'],
        'assets/n4.js': ['assets/n1.js'],
      })
    )
    expect(r.cycles).toHaveLength(1)
    expect(r.cycles[0]).toEqual([
      'assets/n1.js',
      'assets/n2.js',
      'assets/n3.js',
      'assets/n4.js',
    ])
  })

  it('ignores an import that is not an emitted chunk', () => {
    // A cycle can only form inside the emitted set; an edge pointing outside it
    // must not be counted, or every external dependency would inflate edgeCount.
    const r = findChunkCycles(
      report({ 'assets/main.js': ['https://esm.sh/thing', 'assets/missing.js'] })
    )
    expect(r.cycles).toEqual([])
    expect(r.edgeCount).toBe(0)
  })

  it('flags a report whose chunks carry no imports instead of calling it clean', () => {
    // The load-bearing case. An older report has no `imports` field, and reading
    // "no edges" as "no cycles" would be a green gate over an unmeasured graph.
    const stale = report({ 'assets/main.js': [] })
    for (const chunk of stale.chunks) delete (chunk as { imports?: string[] }).imports
    const r = findChunkCycles(stale)
    expect(r.missingImports).toBe(true)
  })

  it('does not flag an empty report as missing imports', () => {
    // Nothing to measure is a different failure, and the CLI reports it with its
    // own exit code, so this flag must not fire for it too.
    expect(findChunkCycles(report({})).missingImports).toBe(false)
  })
})

describe('check-chunk-cycles CLI', () => {
  const script = path.resolve(__dirname, '..', '..', 'scripts', 'check-chunk-cycles.mjs')

  /** Run the gate against a report written to a throwaway file. */
  function run(body: unknown): { status: number | null; out: string } {
    const dir = mkdtempSync(path.join(tmpdir(), 'kc-cycles-'))
    const file = path.join(dir, 'bundle-report.json')
    writeFileSync(file, JSON.stringify(body))
    try {
      const r = spawnSync(process.execPath, [script, file], { encoding: 'utf8' })
      return { status: r.status, out: `${r.stdout}${r.stderr}` }
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  }

  it('exits 0 and says what it measured on an acyclic graph', () => {
    const r = run(report({ 'assets/main.js': ['assets/vendor.js'], 'assets/vendor.js': [] }))
    expect(r.status).toBe(0)
    expect(r.out).toContain('2 chunks')
    expect(r.out).toContain('no cycles')
  })

  it('exits non-zero and names the chunks in the cycle', () => {
    const r = run(
      report({ 'assets/a.js': ['assets/b.js'], 'assets/b.js': ['assets/a.js'] })
    )
    expect(r.status).toBe(1)
    expect(r.out).toContain('assets/a.js')
    expect(r.out).toContain('assets/b.js')
    // The remediation has to name the setting, because the failure is almost
    // always chunking config rather than application code.
    expect(r.out).toContain('includeDependenciesRecursively')
  })

  it('refuses an empty report rather than certifying it', () => {
    expect(run(report({})).status).toBe(4)
  })

  it('refuses a report with no imports field rather than certifying it', () => {
    const stale = report({ 'assets/main.js': [] })
    for (const chunk of stale.chunks) delete (chunk as { imports?: string[] }).imports
    const r = run(stale)
    expect(r.status).toBe(5)
    expect(r.out).toContain('imports')
  })
})
