/**
 * The committed transcript must be writable by the committed script.
 *
 * A review round blocked on exactly this drift: the checked-in transcript documented a
 * link affordance the diff had deleted, so the evidence attested UI the code did not
 * render. Nothing detected it, because an artifact and its generator can disagree
 * silently -- the artifact is just bytes. This closes that by deriving the set of
 * headings the script CAN emit and asserting the artifact uses no other.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { EPISODES } from '../../capture/error-card-proxy-challenge.episodes.mjs'

const at = (rel: string) => fileURLToPath(new URL(rel, import.meta.url))

const SCRIPT = readFileSync(at('../../scripts/capture-error-card-proxy-challenge.mjs'), 'utf8')
const TRANSCRIPT = readFileSync(
  at('../../../temp-screenshots/error-card-proxy-challenge/error-card-proxy-challenge.md'),
  'utf8',
)

/** Heading literals the script pushes, as written in its source. */
const literalHeadings = (): string[] =>
  [...SCRIPT.matchAll(/'(#{1,2} [^']*)'/g)].map(m => m[1])

describe('the committed transcript and its generator', () => {
  it('uses only headings the shipped script can emit', () => {
    const emittable = new Set([
      ...literalHeadings(),
      // The per-episode heading is interpolated: `## ${ep}` over the shared list.
      ...EPISODES.map((ep: string) => `## ${ep}`),
    ])
    const used = TRANSCRIPT.split('\n').filter(l => l.startsWith('#'))
    expect(used.length).toBeGreaterThan(0)
    const foreign = used.filter(h => !emittable.has(h))
    expect(foreign, `headings no shipped code path writes: ${foreign.join(' | ')}`).toEqual([])
  })

  it('covers every episode the shared list defines', () => {
    for (const ep of EPISODES) {
      expect(TRANSCRIPT, `episode ${ep} missing from the transcript`).toContain(`## ${ep}`)
    }
  })

  it('is a positive control: a heading the script cannot write is rejected', () => {
    // Without this, the first case would pass on an empty or over-permissive set.
    const emittable = new Set([...literalHeadings(), ...EPISODES.map((e: string) => `## ${e}`)])
    expect(emittable.has('## framed episode — the address is text, not a control')).toBe(false)
    expect(emittable.has('## framed episode — link elements')).toBe(true)
  })

  it('shows the strings the catalogue actually ships', () => {
    // The drift that blocked was partly a stale STRING, not only a stale heading.
    expect(TRANSCRIPT).toContain('then reload this page')
    expect(TRANSCRIPT).toContain('check the proxy’s logs')
  })
})
