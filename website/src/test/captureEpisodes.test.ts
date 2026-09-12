/**
 * The capture runner waits for exactly the episodes the scene renders.
 *
 * It shipped asserting ten while the scene rendered five, so the documented command
 * timed out and exited 1 without writing a frame — and the evidence it was supposed
 * to produce could not have come from that head. A parity check is cheap; running
 * Playwright in a unit test is not.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const WEB = join(__dirname, '..', '..')
const SCENE = join(WEB, 'capture', 'error-card-proxy-challenge.tsx')
const RUNNER = join(WEB, 'scripts', 'capture-error-card-proxy-challenge.mjs')

/** Episode ids the scene actually renders, in source order. */
function rendered(): string[] {
  const src = readFileSync(SCENE, 'utf8')
  return [...src.matchAll(/data-episode="([^"]+)"/g)].map(m => m[1])
}

/** Episode ids the runner waits for before it screenshots. */
function awaited(): string[] {
  const src = readFileSync(RUNNER, 'utf8')
  const list = /for \(const ep of \[([^\]]*)\]\)/s.exec(src)
  expect(list, 'the runner no longer has a recognisable wait list').not.toBeNull()
  return [...(list as RegExpExecArray)[1].matchAll(/'([^']+)'/g)].map(m => m[1])
}

describe('the error-card capture harness', () => {
  it('waits for no episode the scene does not render', () => {
    const missing = awaited().filter(ep => !rendered().includes(ep))
    expect(missing, `the runner would time out waiting for ${missing.join(', ')}`)
      .toEqual([])
  })

  it('waits for every episode the scene renders', () => {
    // The other direction: an episode nobody asserts can regress unnoticed.
    const unchecked = rendered().filter(ep => !awaited().includes(ep))
    expect(unchecked, `these episodes are captured but never asserted: ${unchecked.join(', ')}`)
      .toEqual([])
  })

  it('reads text only from episodes it waited for', () => {
    const src = readFileSync(RUNNER, 'utf8')
    const read = [...src.matchAll(/await text\('([^']+)'\)/g)].map(m => m[1])
    expect(read.filter(ep => !awaited().includes(ep))).toEqual([])
  })

  it('draws no retry on a state production marks unretryable', () => {
    // The scene once handed every episode a stub continue handler, which drew a
    // Resume button beside copy naming a different action.
    const src = readFileSync(SCENE, 'utf8')
    for (const ep of ['after', 'blocked', 'framed']) {
      const block = new RegExp(`data-episode="${ep}"[\\s\\S]{0,400}?</div>`).exec(src)
      expect(block, `episode ${ep} is missing`).not.toBeNull()
      expect((block as RegExpExecArray)[0].includes('onContinue'),
        `episode ${ep} fabricates a retry affordance`).toBe(false)
    }
  })

  it('names an address in the framed message, which a pane cannot otherwise show', () => {
    // "Open this page in a new tab" named something an embedded reader cannot see:
    // the address bar holds the OUTER dashboard. The string has to carry the value.
    const en = readFileSync(join(WEB, 'src', 'i18n', 'locales', 'en.manual.json'), 'utf8')
    const line = en.split('\n').find(l => l.includes('"proxy_session_expired_framed"'))
    expect(line, 'the framed message is gone').toBeDefined()
    expect((line as string).includes('{{origin}}'),
      'the framed message names no address').toBe(true)
    expect(/\bthis page\b/.test(line as string),
      'the framed message still says "this page"').toBe(false)
  })
})
