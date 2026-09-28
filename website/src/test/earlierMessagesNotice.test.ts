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

function chatSection(file: string): Record<string, Record<string, string>> {
  return JSON.parse(readFileSync(resolve(LOCALES, file), 'utf8')).pages?.chat ?? {}
}

/**
 * The `pages.chat` section per LOCALE, not per file.
 *
 * English ships in two files the runtime deep-merges: one is regenerated wholesale
 * by the codemod, the other holds hand-authored keys with no source literal.
 * Enumerating files instead of locales would demand a hand-authored key inside the
 * generated half, which the codemod would drop.
 */
function chatByLocale(): Map<string, Record<string, Record<string, string>>> {
  const out = new Map<string, Record<string, Record<string, string>>>()
  for (const f of readdirSync(LOCALES).filter(f => f.endsWith('.json'))) {
    const locale = f === 'en.json' || f === 'en.manual.json' ? 'en' : f.replace(/\.json$/, '')
    const section = chatSection(f)
    const prior = out.get(locale) ?? {}
    for (const [group, leaves] of Object.entries(section)) {
      prior[group] = { ...(prior[group] ?? {}), ...leaves }
    }
    out.set(locale, prior)
  }
  return out
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

/** A `?msg=` share link is minted for ANY message by copy-link-to-message, so the
 *  reader who follows a dead one may never have pinned anything. Pin wording there
 *  reports an action they did not take, which is why the origin carries its own copy.
 */
describe('deep-link jump notice', () => {
  const LINK = 'message_unavailable'

  it('ships a link string in every LOCALE that carries the pin string', () => {
    const byLocale = chatByLocale()
    const carriers = [...byLocale].filter(([, chat]) => LINK in (chat.pins ?? {}))
    // Positive control: the pin key is real, so the filter matched actual locales.
    expect(carriers.length).toBeGreaterThan(1)
    expect(carriers.map(([l]) => l)).toContain('en')
    const missing = carriers.filter(([, chat]) => !(LINK in (chat.deepLink ?? {}))).map(([l]) => l)
    expect(missing).toEqual([])
  })

  it('does not describe a shared message as pinned', () => {
    const chat = chatSection('en.manual.json')
    // Still states it is gone, without the "loaded history" qualifier: this copy
    // appears only once EVERY page has been checked, so nothing is left to load.
    expect(chat.deepLink[LINK]).toMatch(/no longer/i)
    expect(chat.deepLink[LINK]).not.toMatch(/loaded history/i)
    // The defect in one assertion: the copy a link reader sees must not claim a pin.
    expect(chat.deepLink[LINK]).not.toMatch(/pinned/i)
  })

  it('keeps the pin string pin-phrased, so the two are not interchangeable', () => {
    // Negative control for the pair: collapsing them back would pass the test above
    // only by making the pin copy generic, which is a different regression.
    const pinned = chatSection('en.json').pins[LINK]
    expect(pinned).toMatch(/pinned/i)
    expect(pinned).not.toBe(chatSection('en.manual.json').deepLink[LINK])
  })
})
