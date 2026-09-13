/**
 * `utils.offline.gateway_offline_reconnect` is localized but interpolates its verb,
 * so a raw English verb passed into it renders a half-English sentence for every
 * non-English dashboard. The verb must arrive as a catalogue lookup.
 *
 * Absolute, with no allowlist: an allowlist would freeze the remaining English at
 * whatever the scan happened to find, which is a debt ledger rather than a gate.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'

const ROOT = path.resolve(__dirname, '..')

/** The four shapes that feed a verb into the offline template. */
const SHAPES = [
  /offlineProps\([^,)]+,\s*'([^']+)'/g,
  /offlineItem\(\s*'([^']+)'/g,
  /\bofflineVerb\s*\?\?\s*'([^']+)'/g,
  /gateway_offline_reconnect'\s*,\s*\{\s*action:\s*'([^']+)'/g,
]

function sources(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const p = path.join(dir, entry)
    if (statSync(p).isDirectory()) {
      if (entry !== 'node_modules') sources(p, out)
    } else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      out.push(p)
    }
  }
  return out
}

function scan(): string[] {
  const hits: string[] = []
  for (const file of sources(ROOT)) {
    const src = readFileSync(file, 'utf-8')
    for (const shape of SHAPES) {
      for (const m of src.matchAll(shape)) hits.push(`${path.relative(ROOT, file)}: '${m[1]}'`)
    }
  }
  return hits
}

describe('offline verbs reach the template as catalogue keys', () => {
  it('no source passes a raw English verb into the offline sentence', () => {
    const offenders = scan()
    expect(offenders, offenders.join('\n')).toEqual([])
  })

  it('the scan can see a literal at all, so the empty result above means something', () => {
    // Positive control on the SHAPES themselves rather than on real sources: with
    // nothing left to find, a dead regex would otherwise pass silently.
    const sample = "offlineProps(connected, 'rename sessions', label)"
    const found = SHAPES.flatMap(s => [...sample.matchAll(s)].map(m => m[1]))
    expect(found).toEqual(['rename sessions'])
  })
})
