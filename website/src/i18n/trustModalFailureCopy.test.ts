/**
 * The trust dialog's failure headlines -- a drift guard on a copy family.
 *
 * The dialog has three paths (enable, first install, update) and two failure
 * states on each: the grant stood (`failed`, `install_failed`, `update_failed`)
 * or nothing was saved (`failed_generic`, `install_failed_generic`,
 * `update_failed_generic`). Each state's advice is one clause shared by its
 * three paths, so a reword on one path only would make the paths read
 * differently. The invariants are asserted as STRUCTURE, in every authored
 * catalog, rather than as literals:
 *
 * 1. The three granted headlines end on one shared clause (how to remove the
 *    trust) and name no retry: that state's retry has its own label, which
 *    does not ask for trust because trust is saved, so it needs no sentence.
 * 2. The not-saved state's next step is a separate line with two forms, and
 *    the headlines name no control. The connected form tells the reader to
 *    choose the retry and names the agent hand-off. The disconnected form
 *    says the retry is off until the reconnect and never names the hand-off,
 *    which hides while the gateway is unreachable.
 * 3. A control named inside a sentence is quoted, in the locale's own quote
 *    pair (the one its title puts around the app name), so a label reads as
 *    a name and not as running text.
 *
 * English only: the granted clause does not describe the retry as trusting a
 * second time, and the not-saved clause does not send the reader to logs.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'

const GRANTED = ['failed', 'install_failed', 'update_failed'] as const
const GENERIC = ['failed_generic', 'install_failed_generic', 'update_failed_generic'] as const
const CANCEL_LINES = [
  'on_cancel', 'on_cancel_granted',
  'install_on_cancel', 'install_on_cancel_granted',
  'update_on_cancel', 'update_on_cancel_granted',
] as const

type Catalog = Record<string, unknown>

function at(catalog: Catalog, path: string): string {
  let node: unknown = catalog
  for (const part of path.split('.')) node = (node as Catalog | undefined)?.[part]
  if (typeof node !== 'string') throw new Error(`missing ${path}`)
  return node
}

function modal(catalog: Catalog, key: string): string {
  return at(catalog, `components.appstore.trustAppModal.${key}`)
}

/** The longest suffix every string shares. */
function sharedSuffix(values: readonly string[]): string {
  const [first, ...rest] = values
  let n = 0
  while (n < first.length && rest.every(v => v.length > n && v[v.length - 1 - n] === first[first.length - 1 - n])) n += 1
  return first.slice(first.length - n)
}

/**
 * A button label as a sentence can carry it. The Bengali hand-off label ends on
 * the আপনি-form imperative (করুন), which the Bengali register (style/bn.md §5)
 * keeps out of running copy, so a sentence names the control by the words
 * before that ending and closes on the তুমি form. Every other label is used
 * whole, and quoted.
 */
function labelStem(label: string): string {
  return label.replace(/\s*করুন$/, '')
}

// The generated pseudolocale rewrites every string on its own, so a label it
// embeds inside a sentence is not the label it renders on the button.
const LOCALES = Object.entries(CATALOGS)
  .filter(([code]) => code !== 'en-XA')
  .map(([code, { translation }]) => [code, translation as Catalog] as const)

/** The locale's quote pair: the characters its title puts around the app name. */
function quotePair(catalog: Catalog): readonly [string, string] {
  const title = modal(catalog, 'title')
  const m = /([“„«‘「\u202f]+)\{\{app\}\}([”“»’」\u202f]+)/u.exec(title)
  if (!m) throw new Error(`title has no quoted {{app}}: ${title}`)
  return [m[1], m[2]]
}

function quoted(catalog: Catalog, label: string): string {
  const [open, close] = quotePair(catalog)
  return `${open}${label}${close}`
}

describe('trust dialog failure headlines', () => {
  it.each(LOCALES)('%s: the granted headlines share one clause and leave the retry to its own label', (_code, catalog) => {
    // The trust-saved retry's label says what it does, so the headline does
    // not explain or repeat it; the shared clause is how to remove the trust.
    const retry = modal(catalog, 'confirm_retry_granted')
    expect(retry).not.toBe(modal(catalog, 'confirm_after_failure'))
    expect(sharedSuffix(GRANTED.map(k => modal(catalog, k))).trim().length).toBeGreaterThan(0)
    for (const key of GRANTED) {
      expect(modal(catalog, key)).not.toContain(retry)
      expect(modal(catalog, key)).not.toContain(modal(catalog, 'confirm_after_failure'))
    }
  })

  it.each(LOCALES)('%s: only the connected next step tells the reader to choose the not-saved retry', (_code, catalog) => {
    // Offline that retry is off, so the headline (shown in both cases) must
    // not send the reader to it; the connected next step does.
    const retry = modal(catalog, 'confirm_after_failure')
    for (const key of GENERIC) expect(modal(catalog, key)).not.toContain(retry)
    expect(modal(catalog, 'failed_generic_next_ask')).toContain(quoted(catalog, retry))
  })

  it.each(LOCALES)('%s: only the connected next step names the agent hand-off', (_code, catalog) => {
    const label = at(catalog, 'components.askAgent.ask_the_agent')
    const ask = labelStem(label)
    // A label used whole is quoted. The Bengali one cannot be used whole (its
    // ending is the আপনি form), so its stem is the sentence's own verb.
    expect(modal(catalog, 'failed_generic_next_ask')).toContain(ask === label ? quoted(catalog, label) : ask)
    expect(modal(catalog, 'failed_generic_next_offline')).not.toContain(ask)
    for (const key of GENERIC) expect(modal(catalog, key)).not.toContain(ask)
  })

  it.each(LOCALES)('%s: the offline next step names the retry button it turns off', (_code, catalog) => {
    expect(modal(catalog, 'failed_generic_next_offline'))
      .toContain(quoted(catalog, modal(catalog, 'confirm_after_failure')))
  })

  it.each(LOCALES)('%s: an exit sentence that opens on the Cancel label quotes it', (_code, catalog) => {
    // Bengali opens on the word as a verb (বাতিল করলে, "if you cancel"), the way
    // hi, ja and ko name the action rather than the control, so it is not a
    // label in running text.
    const cancel = modal(catalog, 'cancel')
    for (const key of CANCEL_LINES) {
      const value = modal(catalog, key)
      const bareLabel = value.startsWith(`${cancel} `) && !value.startsWith(`${cancel} করলে`)
      expect(bareLabel).toBe(false)
    }
  })

  it('en: the retry is not described as trusting again, and nothing points at logs', () => {
    const en = CATALOGS.en.translation as Catalog
    for (const key of GRANTED) expect(modal(en, key)).not.toMatch(/re-confirm/i)
    for (const key of [...GENERIC, 'failed_generic_next_ask', 'failed_generic_next_offline']) {
      expect(modal(en, key)).not.toMatch(/\blogs?\b/i)
    }
  })
})
