/** The earlier-messages jump has two failure modes and they must not share copy.
 *
 *  A genuinely-gone anchor is permanent; a failed fetch is transient. Reporting
 *  the second with the first's wording told the reader their history was gone,
 *  with nothing to retry. These read the real catalogues, so a key that exists
 *  in the page but not on disk fails here rather than rendering raw to a user.
 */
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const LOCALES = resolve(dirname(fileURLToPath(import.meta.url)), '../i18n/locales')
const GONE = 'earlier_messages_unavailable'
const FAILED = 'earlier_messages_load_failed'

function chatPane(file: string): Record<string, string> {
  const json = JSON.parse(readFileSync(resolve(LOCALES, file), 'utf8'))
  return json.components.chatPane
}

describe('earlier-messages jump notices', () => {
  it('ships the transient string in every catalogue that carries the gone string', () => {
    const carriers = readdirSync(LOCALES)
      .filter(f => f.endsWith('.json'))
      .filter(f => GONE in chatPane(f))
    // Positive control: the sibling key is present, so the filter found real files.
    expect(carriers.length).toBeGreaterThan(1)
    const missing = carriers.filter(f => !(FAILED in chatPane(f)))
    expect(missing).toEqual([])
  })

  it('does not reuse the gone copy for a failed fetch', () => {
    const en = chatPane('en.manual.json')
    expect(en[FAILED]).not.toBe(en[GONE])
    expect(en[FAILED].length).toBeGreaterThan(0)
  })

  // The whole defect was a permanent claim on a transient failure, so the
  // transient string must not say the history is gone.
  it('states the failure as retryable rather than as lost history', () => {
    const en = chatPane('en.manual.json')
    expect(en[GONE]).toMatch(/no longer available/i)
    expect(en[FAILED]).not.toMatch(/no longer available/i)
    expect(en[FAILED]).toMatch(/try again/i)
  })
})
