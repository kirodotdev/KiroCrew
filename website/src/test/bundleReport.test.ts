import { describe, it, expect } from 'vitest'
import {
  formatBytes,
  ownerOf,
  summarizeBundle,
  renderReport,
  diffSummaries,
} from '../../scripts/lib/bundleReport.mjs'

describe('formatBytes', () => {
  it('scales units', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(2048)).toBe('2.0 KB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.00 MB')
  })

  it('rejects junk rather than printing NaN', () => {
    expect(formatBytes(undefined)).toBe('-')
    expect(formatBytes(-1)).toBe('-')
    expect(formatBytes(Number.NaN)).toBe('-')
    expect(formatBytes('100')).toBe('-')
  })
})

describe('ownerOf', () => {
  it('collapses a node_modules path to its package', () => {
    expect(ownerOf('/repo/node_modules/lodash/index.js')).toBe('lodash')
  })

  it('keeps the scope so two same-named packages stay distinct', () => {
    expect(ownerOf('/repo/node_modules/@tanstack/react-query/dist/x.js')).toBe(
      '@tanstack/react-query'
    )
  })

  it('uses the LAST node_modules segment for nested dependencies', () => {
    // A nested dep must be attributed to itself, not to its parent.
    expect(ownerOf('/repo/node_modules/a/node_modules/b/index.js')).toBe('b')
  })

  it('buckets first-party code by its directory under src', () => {
    expect(ownerOf('/repo/website/src/pages/ChatPage.tsx')).toBe('src/pages')
    expect(ownerOf('/repo/website/src/main.tsx')).toBe('src')
  })

  it('normalizes Windows separators', () => {
    expect(ownerOf('C:\\repo\\node_modules\\lodash\\index.js')).toBe('lodash')
    expect(ownerOf('C:\\repo\\website\\src\\pages\\X.tsx')).toBe('src/pages')
  })

  it('handles junk input', () => {
    expect(ownerOf(null)).toBe('(unknown)')
    expect(ownerOf('')).toBe('(unknown)')
    expect(ownerOf('/somewhere/else/file.js')).toBe('(other)')
  })
})

function bundleFixture() {
  return {
    'assets/index-abc.js': {
      type: 'chunk',
      isEntry: true,
      code: 'x'.repeat(1000),
      modules: {
        '/repo/node_modules/lodash/index.js': { renderedLength: 600 },
        '/repo/website/src/pages/ChatPage.tsx': { renderedLength: 300 },
        '/repo/website/src/pages/Other.tsx': { renderedLength: 100 },
        '/repo/node_modules/shaken/index.js': { renderedLength: 0 },
      },
    },
    'assets/lazy-def.js': {
      type: 'chunk',
      isDynamicEntry: true,
      code: 'y'.repeat(200),
      modules: { '/repo/node_modules/@scope/pkg/x.js': { renderedLength: 200 } },
    },
    'assets/style.css': { type: 'asset', source: 'z'.repeat(50) },
  }
}

describe('summarizeBundle', () => {
  it('totals chunks and assets separately', () => {
    const s = summarizeBundle(bundleFixture(), { now: () => 'T' })
    expect(s.version).toBe(1)
    expect(s.generatedAt).toBe('T')
    expect(s.totals.chunkBytes).toBe(1200)
    expect(s.totals.assetBytes).toBe(50)
    expect(s.totals.chunkCount).toBe(2)
    expect(s.totals.assetCount).toBe(1)
  })

  it('sorts chunks largest first and labels their kind', () => {
    const s = summarizeBundle(bundleFixture())
    expect(s.chunks[0].fileName).toBe('assets/index-abc.js')
    expect(s.chunks[0].isEntry).toBe(true)
    expect(s.chunks[1].isDynamicEntry).toBe(true)
  })

  it('excludes fully tree-shaken modules from owners and the module count', () => {
    const s = summarizeBundle(bundleFixture())
    expect(s.owners.map((o) => o.owner)).not.toContain('shaken')
    // 3 of the 4 modules in the entry chunk contributed bytes.
    expect(s.chunks[0].moduleCount).toBe(3)
  })

  it('aggregates owners across chunks and sorts by weight', () => {
    const s = summarizeBundle(bundleFixture())
    expect(s.owners[0]).toEqual({ owner: 'lodash', size: 600 })
    // Both pages collapse into one src/pages bucket: 300 + 100.
    expect(s.owners.find((o) => o.owner === 'src/pages')?.size).toBe(400)
    expect(s.owners.find((o) => o.owner === '@scope/pkg')?.size).toBe(200)
  })

  it('survives an empty or malformed bundle', () => {
    expect(summarizeBundle({}).totals.chunkBytes).toBe(0)
    expect(summarizeBundle(null).chunks).toEqual([])
    const s = summarizeBundle({ a: null, b: 'nope', c: { type: 'chunk' } })
    expect(s.chunks).toHaveLength(1)
    expect(s.chunks[0].size).toBe(0)
  })

  it('handles a binary asset source with byteLength', () => {
    const s = summarizeBundle({
      'img.png': { type: 'asset', source: new Uint8Array(12) },
    })
    expect(s.totals.assetBytes).toBe(12)
  })

  it('ignores a non-numeric renderedLength instead of producing NaN totals', () => {
    const s = summarizeBundle({
      'a.js': {
        type: 'chunk',
        code: '',
        modules: { '/repo/node_modules/x/i.js': { renderedLength: 'big' } },
      },
    })
    expect(s.owners).toEqual([])
  })
})

describe('renderReport', () => {
  it('includes totals, chunks and contributors', () => {
    const out = renderReport(summarizeBundle(bundleFixture(), { now: () => 'T' }))
    expect(out).toContain('Bundle report')
    expect(out).toContain('assets/index-abc.js')
    expect(out).toContain('lodash')
    expect(out).toContain('entry')
    expect(out).toContain('dynamic')
  })

  it('honours the top cap', () => {
    const out = renderReport(summarizeBundle(bundleFixture()), { top: 1 })
    expect(out).toContain('assets/index-abc.js')
    expect(out).not.toContain('assets/lazy-def.js')
  })

  it('does not throw on a missing summary', () => {
    expect(renderReport(null)).toBe('No bundle summary available.')
  })
})

describe('diffSummaries', () => {
  it('reports growth and shrinkage per contributor', () => {
    const before = summarizeBundle(bundleFixture())
    const after = summarizeBundle({
      'assets/index-abc.js': {
        type: 'chunk',
        isEntry: true,
        code: 'x'.repeat(1500),
        modules: {
          '/repo/node_modules/lodash/index.js': { renderedLength: 900 },
          '/repo/website/src/pages/ChatPage.tsx': { renderedLength: 300 },
        },
      },
    })
    const d = diffSummaries(before, after)
    expect(d.chunkBytesDelta).toBe(300)
    const lodash = d.owners.find((o) => o.owner === 'lodash')
    expect(lodash?.delta).toBe(300)
    // Dropped entirely: shows as a negative rather than vanishing from the diff.
    expect(d.owners.find((o) => o.owner === '@scope/pkg')?.delta).toBe(-200)
  })

  it('reports no per-contributor change for identical builds', () => {
    const s = summarizeBundle(bundleFixture())
    expect(diffSummaries(s, s).owners).toEqual([])
    expect(diffSummaries(s, s).chunkBytesDelta).toBe(0)
  })
})
