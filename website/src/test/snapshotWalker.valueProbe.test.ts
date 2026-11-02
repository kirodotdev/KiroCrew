/**
 * A selector probe cannot spell out a password the person typed during a takeover.
 *
 * React mirrors a field's live value into its `value` attribute, so a `reflex`
 * read like `{sel: 'input[value^="hun"]', prop: 'count'}` answers 1 or 0 about
 * the password one prefix at a time. A field the person filled through a quick
 * ask carries `data-kc-asked`; a password typed by hand during a takeover does
 * not, and still must not answer.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { setHtml } from './domFixture'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walkerSource(): string {
  const lines = readFileSync(join(HARNESS_DIR, 'snapshot_walker.js'), 'utf8').split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return lines.join('\n').trim()
}

type Read = Record<string, unknown>
const walker = () =>
  new Function(`return (${walkerSource()})`)() as { observe: (o: unknown) => { read?: Read } }

const probe = (sel: string) =>
  walker().observe({ read: [{ name: 'n', sel, prop: 'count' }] }).read?.n

// `observe` would start its change counters: a MutationObserver and a frame loop
// that never ends, which keeps the test DOM from settling. Present ones are reused.
type TickWindow = { __kcTick?: unknown; __kcTickDoc?: unknown }
beforeEach(() => {
  ;(window as unknown as TickWindow).__kcTick = { m: 0, f: 0 }
  ;(window as unknown as TickWindow).__kcTickDoc = document
})
afterEach(() => {
  delete (window as unknown as TickWindow).__kcTick
  delete (window as unknown as TickWindow).__kcTickDoc
  document.body.replaceChildren()
})

describe('value-attribute probes', () => {
  it('a hand-typed password answers no prefix probe', () => {
    setHtml(document.body, '<main><input id="p" type="password" value="hunter2"></main>')
    expect(probe('input[value^="hun"]')).toBeNull()
    expect(probe('body:has(input[value^="x"])')).toBeNull()
  })

  it('a hand-typed secret-named field answers no prefix probe', () => {
    setHtml(document.body, '<main><input id="otp" name="otp" type="text" value="123456"></main>')
    expect(probe('input[value^="12"]')).toBeNull()
  })

  it('an empty password field leaves ordinary value probes alone', () => {
    setHtml(document.body, '<main><input id="p" type="password"><input id="q" value="cats"></main>')
    expect(probe('input[value="cats"]')).toBe(1)
  })
})
