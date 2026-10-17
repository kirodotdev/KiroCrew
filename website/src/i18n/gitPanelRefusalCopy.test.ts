/**
 * The Git panel's filter-refusal copy family — a drift guard for issue #12080.
 *
 * ## What kept going wrong
 *
 * The refusal is ONE condition (refused by policy) with two causes and three
 * possible scopes, and the copy for it lives in thirteen catalogs. Every round
 * of review on #11982 named a different member of that family — the shared
 * headline, then the divergent-state over-claim, then the declared sentence's
 * opening words — and settling them one at a time rewrote one string per head
 * and produced the next finding. The catalog values were also hand-written each
 * round, which is what produced a Korean non-word and a malformed Bengali
 * conjunction.
 *
 * So the invariants are asserted here, as STRUCTURE rather than as literals. A
 * literal assertion pins wording that is allowed to change and says nothing
 * about the relationships that actually broke; these say what has to stay true
 * of the set however the sentences are reworded, in every language.
 *
 * ## The invariants
 *
 * 1. **Scope is real.** Three declared titles and three unreadable ones, all
 *    pairwise distinct within their cause. A refusal renders inside a notice
 *    that can be one of two siblings, or alone while the other route is
 *    healthy, so a single title claiming both halves reports a sibling's
 *    failure as its own or denies a list that is on screen.
 * 2. **Permanence is on the headline.** For every scope the declared title
 *    differs from the unreadable one. The two causes take opposite advice —
 *    declared is permanent while the config stands, unreadable can clear on its
 *    own — and a reader who reads only the bold line has to come away with the
 *    right one.
 * 3. **The declared sentence leads with its cause.** `Git LFS` is a
 *    do-not-translate term, so "opens on the cause" is checkable in every
 *    catalog: the value starts with it.
 * 4. **The two hand-offs are distinguishable.** Two notices stack in the
 *    divergent state and each carries its own report; with the shared label
 *    nothing said which failure each one was for.
 *
 * The editorial rules that cannot be expressed language-independently — the
 * retry cue, the panel's own vocabulary, the separate-failure marker — are
 * asserted on English only. `catalogParity.test.ts` and the untranslated
 * ratchet already prove the translations exist and are not English.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'

/** All source files under `src/`, the same walk shape as deadKeys.test.ts. */
function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) {
      if (name === 'node_modules' || name === 'locales') continue
      sourceFiles(p, out)
    } else if (/\.(tsx?|mjs)$/.test(name)) {
      out.push(p)
    }
  }
  return out
}

const GP = 'components.gitPanel'

/** Titles by cause and scope — the same map `utils/gitStatusError.ts` indexes. */
const TITLES = {
  declared: [
    `${GP}.filter_refused_title`,
    `${GP}.filter_refused_title_changes`,
    `${GP}.filter_refused_title_history`,
  ],
  unreadable: [
    `${GP}.filter_refused_title_unreadable`,
    `${GP}.filter_refused_title_unreadable_changes`,
    `${GP}.filter_refused_title_unreadable_history`,
  ],
} as const

const DECLARED_BODY = `${GP}.filter_refused`
const UNREADABLE_BODY = `${GP}.filter_refused_unreadable`
const ASK_CHANGES = `${GP}.ask_agent_changes`
const ASK_HISTORY = `${GP}.ask_agent_history`
const SHARED_ASK = 'components.askAgent.ask_the_agent'

const EVERY_KEY = [
  DECLARED_BODY,
  UNREADABLE_BODY,
  ASK_CHANGES,
  ASK_HISTORY,
  ...TITLES.declared,
  ...TITLES.unreadable,
]

/**
 * The generated pseudolocale.
 *
 * Excluded from the leads-with-its-cause check only. Its values come from
 * `scripts/gen-pseudolocale.mjs`, which pads them and transliterates every ASCII
 * letter — `Git LFS` comes out as look-alike glyphs — so nothing about the copy's
 * opening words is observable there. Every other invariant here still covers it.
 */
const GENERATED = 'en-XA'

const DEFAULT = 'en'

/** Resolve a dotted key against a nested catalog object. */
function resolve(catalog: Record<string, unknown>, dotted: string): unknown {
  let node: unknown = catalog
  for (const part of dotted.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return node
}

const LOCALES = Object.entries(CATALOGS).map(
  ([lang, bundle]) => [lang, (bundle as { translation: Record<string, unknown> }).translation] as const,
)

function value(catalog: Record<string, unknown>, key: string): string {
  return resolve(catalog, key) as string
}

describe('gitPanel filter-refusal copy family (#12080)', () => {
  it('scans every shipped catalog', () => {
    // A green suite that resolved no catalogs would pass every assertion below
    // vacuously, which is the failure this whole file exists to prevent.
    expect(LOCALES.length).toBeGreaterThanOrEqual(13)
    expect(LOCALES.map(([lang]) => lang)).toContain(DEFAULT)
  })

  it('every catalog carries the whole family as non-empty strings', () => {
    const missing: string[] = []
    for (const [lang, catalog] of LOCALES) {
      for (const key of EVERY_KEY) {
        const v = resolve(catalog, key)
        if (typeof v !== 'string' || v.trim() === '') missing.push(`${lang}:${key}`)
      }
    }
    // i18next renders a missing key as the key string itself, so a locale that
    // lost one of these ships `components.gitPanel.…` as visible UI text.
    expect(missing, 'keys absent or empty').toEqual([])
  })

  it('names a different half for each scope, within each cause', () => {
    const collisions: string[] = []
    for (const [lang, catalog] of LOCALES) {
      for (const cause of ['declared', 'unreadable'] as const) {
        const values = TITLES[cause].map((k) => value(catalog, k))
        if (new Set(values).size !== values.length) collisions.push(`${lang}:${cause}`)
      }
    }
    // One title for all three scopes is the over-claim: rendered beside a
    // sibling notice it reports that sibling's failure too, and rendered while
    // the other route is healthy it denies a list the panel is drawing below.
    expect(collisions, 'a cause reuses one title across scopes').toEqual([])
  })

  it('tells the permanent cause from the self-healing one on the headline', () => {
    const shared: string[] = []
    for (const [lang, catalog] of LOCALES) {
      for (let i = 0; i < TITLES.declared.length; i += 1) {
        const declared = value(catalog, TITLES.declared[i])
        const unreadable = value(catalog, TITLES.unreadable[i])
        if (declared === unreadable) shared.push(`${lang}:${TITLES.declared[i]}`)
      }
    }
    // A reader habituated to the permanent case gives up on the one that clears
    // itself, so the difference cannot live only in the body.
    expect(shared, 'the two causes share a title at some scope').toEqual([])
  })

  it('opens the declared sentence on its cause', () => {
    const offenders: string[] = []
    for (const [lang, catalog] of LOCALES) {
      // `gen-pseudolocale.mjs` transliterates ASCII letters, so even the
      // do-not-translate term comes out as look-alikes there. Nothing about the
      // copy is observable in that value.
      if (lang === GENERATED) continue
      if (!value(catalog, DECLARED_BODY).trimStart().startsWith('Git LFS')) offenders.push(lang)
    }
    // Both causes used to open on "this repository's Git config", so the only
    // thing telling them apart was a verb several words in.
    expect(offenders, 'the declared sentence does not lead with Git LFS').toEqual([])
  })

  it('gives the two hand-offs their own labels', () => {
    const offenders: string[] = []
    for (const [lang, catalog] of LOCALES) {
      const changes = value(catalog, ASK_CHANGES)
      const history = value(catalog, ASK_HISTORY)
      const sharedLabel = value(catalog, SHARED_ASK)
      if (changes === history) offenders.push(`${lang}: both halves share a label`)
      if (changes === sharedLabel || history === sharedLabel) {
        offenders.push(`${lang}: a label is the undifferentiated shared one`)
      }
    }
    expect(offenders, 'stacked hand-offs are indistinguishable').toEqual([])
  })
})

describe('gitPanel refusal copy — English editorial rules (#12080)', () => {
  const en = LOCALES.find(([lang]) => lang === DEFAULT)?.[1] as Record<string, unknown>

  it('resolves the English catalog', () => {
    expect(en).toBeTruthy()
  })

  it('speaks of changes and history rather than of checks', () => {
    // The surface in front of the reader is labelled Changes and Commits. A
    // reader shown "keeps these checks off here" was not sure the checks being
    // kept off were the CHANGES list two frames earlier.
    for (const key of [DECLARED_BODY, UNREADABLE_BODY, `${GP}.refresh_unavailable`]) {
      expect(value(en, key).toLowerCase(), `${key} still says "check"`).not.toContain('check')
    }
  })

  it('keeps the shared bodies free of any control the surface may not have', () => {
    // These two strings render on THREE surfaces: the Git panel, the chat file
    // rail and the Pierre workspace tree. The last renders no title and has no
    // refresh control at all, so a body that says to refresh names a control one
    // panel over. The panel keeps the invitation where it is true -- its
    // unreadable titles say "right now" and its own control is live there.
    const refresh = value(en, `${GP}.refresh`).toLowerCase()
    for (const key of [DECLARED_BODY, UNREADABLE_BODY]) {
      expect(
        value(en, key).toLowerCase(),
        `${key} names a control that two of its three surfaces do not have`,
      ).not.toContain(refresh)
    }
  })

  it('confines the panel-only strings to the panel', () => {
    // The six titles and the separate-failure notice are allowed to promise what
    // the Git panel can deliver, so they must not leak to a surface that cannot.
    // Scanning source rather than trusting the call sites, because the whole
    // defect this replaces was a shared string quietly acquiring a panel-only
    // promise.
    const panelOnly = [...TITLES.declared, ...TITLES.unreadable, `${GP}.status_failed_no_history`]
    const allowed = new Set(['components/GitPanel.tsx', 'utils/gitStatusError.ts'])
    const offenders: string[] = []
    for (const file of sourceFiles(join(__dirname, '..'))) {
      const rel = relative(join(__dirname, '..'), file).split('\\').join('/')
      if (allowed.has(rel) || /\.test\.tsx?$/.test(rel)) continue
      const text = readFileSync(file, 'utf8')
      for (const key of panelOnly) {
        const leaf = key.slice(GP.length + 1)
        if (text.includes(key) || text.includes(`gitPanel.${leaf}`)) {
          offenders.push(`${rel} references ${key}`)
        }
      }
    }
    expect(offenders, 'a panel-only string reached another surface').toEqual([])
  })

  it('marks the status notice as a separate failure, and says what to do', () => {
    // In the divergent state the two stacked notices are two different problems,
    // and a reader could not tell that from copy where both were about reading
    // changes. It also has to carry a next step: a reader shown the frame said it
    // was the one red box they would least know what to do with. The cue is safe
    // on THIS string -- it renders only in the branch where at least one route is
    // not refusing, and the control goes inert only when BOTH are, so refresh is
    // live wherever these words appear.
    const separate = value(en, `${GP}.status_failed_no_history`)
    expect(separate.toLowerCase()).toContain('separate')
    expect(separate.toLowerCase()).toContain(value(en, `${GP}.refresh`).toLowerCase())
    // Its sibling keeps NO cue: there the commit list is still on screen and the
    // sentence is about staleness, not about something that cannot be shown.
    expect(value(en, `${GP}.status_failed`).toLowerCase())
      .not.toContain(value(en, `${GP}.refresh`).toLowerCase())
  })
})
