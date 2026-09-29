/**
 * Crewmates corpus for the Command Bar's Search Crewmates view.
 *
 * Exercises `createMatesProvider` directly with a mock catalog fetch, a mock
 * navigation callback and a mock running predicate — which is the whole reason the
 * corpus is a plain function rather than a hook.
 *
 * Assertions are on ORDER and MEMBERSHIP, never on a literal fuzzy score: the scores
 * come from the shared `fuzzyMatch`, so pinning a number here would fail whenever
 * that scorer is retuned, for a reason that has nothing to do with the crew. The one
 * score fact worth asserting is the RELATION this provider itself creates — a match
 * on the displayed label must outrank a match on the hidden identity.
 */
import { describe, it, expect, vi } from 'vitest'

import { createMatesProvider, mateRoute } from './matesProvider'
import { i18nT } from '../../i18n/t'
import type { KiroCrewAgent } from '../../components/AgentSelector'

function mate(over: Partial<KiroCrewAgent> & { name: string }): KiroCrewAgent {
  return {
    kiro_agent: '',
    workspace: '',
    memory_store: '',
    description: '',
    source: '',
    selection_kind: 'member',
    ...over,
  }
}

/**
 * The roster the tests read against.
 *
 * `reviewer` carries a display label that HIDES its identity (`qa-bot`), which is
 * the case the two-field match exists for and the one a single-field match would
 * silently lose. `shipper` is a TEMPLATE sharing a name with nothing — it is here to
 * be excluded. `Review & QA` is a real crew-name shape and the reason the route is
 * encoded.
 */
const ROSTER: KiroCrewAgent[] = [
  mate({ name: 'oncall', description: 'Watches the pager and triages alerts.' }),
  mate({ name: 'qa-bot', display_name: 'reviewer', description: 'Reviews diffs.' }),
  mate({ name: 'Review & QA', description: 'Runs the review board.' }),
  mate({ name: 'accountant', description: 'Reconciles the spend ledger.' }),
  mate({ name: 'shipper', description: 'A shared template.', selection_kind: 'template' }),
]

function build(rows: KiroCrewAgent[] = ROSTER, running: string[] = []) {
  const openMate = vi.fn()
  const provider = createMatesProvider({
    fetchMates: async () => rows,
    openMate,
    isRunning: (name: string) => running.includes(name),
  })
  return { provider, openMate }
}

describe('createMatesProvider — matching', () => {
  it('finds a crewmate on a PARTIAL name, which is the whole gesture', async () => {
    const { provider } = build()
    const results = await provider.search('onc')
    expect(results.map(r => r.title)).toEqual(['oncall'])
    // Highlight indices are positions in the TITLE, which is what the row splits on
    // to emit <mark> nodes.
    expect(results[0].indices.length).toBeGreaterThan(0)
  })

  it('matches a scattered subsequence, not just a prefix', async () => {
    const { provider } = build()
    expect((await provider.search('acnt')).map(r => r.title)).toEqual(['accountant'])
  })

  it('reaches a crewmate through the identity its display label hides, and ranks it below a label match', async () => {
    const { provider } = build([
      mate({ name: 'qa-bot', display_name: 'reviewer' }),
      mate({ name: 'qa-helper' }),
    ])
    const results = await provider.search('qa')
    const ids = results.map(r => r.id)
    // Both surfaced: one by its own visible name, one only through `name`.
    expect(ids).toContain('mates:qa-helper')
    expect(ids).toContain('mates:qa-bot')
    // The visible name wins — the reader typed at what is on screen.
    expect(ids[0]).toBe('mates:qa-helper')
    const visible = results.find(r => r.id === 'mates:qa-helper')!
    const hidden = results.find(r => r.id === 'mates:qa-bot')!
    expect(visible.score).toBeGreaterThan(hidden.score)
    // The hidden one surfaced on its identity, so there is nothing in its TITLE to
    // highlight; marking characters the reader cannot see would be a lie.
    expect(hidden.indices).toEqual([])
  })

  it('SHOWS the identity that matched, GLOSSED and highlighted, so the row explains itself', async () => {
    // Without this the row is unexplainable: typing `qa` surfaces a row titled
    // "reviewer" with no highlight anywhere, and a reader reads that as a wrong
    // result. The matched field takes the second line, which is the same answer the
    // folders corpus gives when an ancestry path is why a folder surfaced -- but a bare
    // second name reads as a possible SECOND crewmate, so it carries a gloss.
    const { provider } = build([mate({ name: 'qa-bot', display_name: 'reviewer', description: 'Reviews diffs.' })])
    const [row] = await provider.search('qa')
    expect(row.title).toBe('reviewer')
    expect(row.subtitle).toBe(i18nT('apps.commandBar.mate_also_called', { identity: 'qa-bot' }))
    // The gloss must not be a bare name, or it explains nothing.
    expect(row.subtitle).not.toBe('qa-bot')
    expect(row.subtitle).toContain('qa-bot')
    expect(row.subtitleIndices?.length).toBeGreaterThan(0)
  })

  it('highlights the identity where it actually sits INSIDE the gloss', async () => {
    // The gloss shifts every offset. Indices carried over unshifted would mark the
    // gloss's own words, or point past the end of the rendered string.
    const { provider } = build([mate({ name: 'qa-bot', display_name: 'reviewer' })])
    const [row] = await provider.search('qa')
    const shown = row.subtitle!
    const marked = (row.subtitleIndices ?? []).map(i => shown[i]).join('')
    expect(marked.toLowerCase()).toBe('qa')
    expect(Math.min(...(row.subtitleIndices ?? [0]))).toBe(shown.toLowerCase().indexOf('qa-bot'))
  })

  it('marks the IDENTITY and never the gloss, for a query the gloss also contains', async () => {
    // The case a `qa`-shaped fixture cannot catch, and the reason the first version of
    // this was wrong: "also called" contains `al`, so re-searching the glossed string
    // for the query marks the gloss's own prose instead of the name. Every index must
    // land inside the identity's own span.
    const { provider } = build([mate({ name: 'alerts-bot', display_name: 'Pager' })])
    const [row] = await provider.search('al')
    const shown = row.subtitle!
    const at = shown.indexOf('alerts-bot')
    expect(at).toBeGreaterThan(0)
    const idx = row.subtitleIndices ?? []
    expect(idx.length).toBeGreaterThan(0)
    for (const i of idx) {
      expect(i).toBeGreaterThanOrEqual(at)
      expect(i).toBeLessThan(at + 'alerts-bot'.length)
    }
  })

  it('marks the interpolated identity, not the same letters earlier in the phrase', async () => {
    // The case the `alerts-bot` fixture above cannot catch: the offsets were shifted by
    // `gloss.indexOf(identity)`, which finds where the identity's CHARACTERS first appear
    // in the rendered prose rather than where the template put the value. A mate named
    // `cal` glosses to "also called cal", whose first `cal` is inside "called" -- so the
    // highlight marked the catalog's own word and left the name bare. `al`, `ed`, `so`
    // and `led` are the other names that land on it in English, and a locale whose phrase
    // happens to contain the name lands on it in any language.
    const { provider } = build([mate({ name: 'cal', display_name: 'Scheduler' })])
    const [row] = await provider.search('cal')
    const shown = row.subtitle!
    // The fixture is only a fixture if the trap is actually in the rendered string.
    expect(shown.indexOf('cal')).toBeLessThan(shown.lastIndexOf('cal'))
    const idx = row.subtitleIndices ?? []
    expect(idx.length).toBe(3)
    expect(Math.min(...idx)).toBe(shown.lastIndexOf('cal'))
    expect(idx.map(i => shown[i]).join('')).toBe('cal')
  })

  it('still marks a SCATTERED identity match, which a substring search cannot find', async () => {
    // `ab` is a subsequence of `alerts-bot`, not a substring, so `indexOf` answers -1 and
    // a substring-based highlight renders the second name unmarked -- the other outcome
    // the highlight exists to prevent.
    const { provider } = build([mate({ name: 'alerts-bot', display_name: 'Pager' })])
    const [row] = await provider.search('ab')
    expect(row.subtitle).toContain('alerts-bot')
    expect((row.subtitleIndices ?? []).length).toBeGreaterThan(0)
  })

  it('keeps the ROLE on the second line when the visible name is what matched', async () => {
    // The identity only takes that line when it is the reason the row is here; a
    // label match leaves the row saying what the mate is for.
    const { provider } = build()
    const [row] = await provider.search('reviewer')
    expect(row.subtitle).toBe('Reviews diffs.')
    expect(row.subtitleIndices).toBeUndefined()
  })

  it('titles a row with the display label and keeps the identity as its id', async () => {
    const { provider } = build()
    const [row] = await provider.search('reviewer')
    expect(row.title).toBe('reviewer')
    expect(row.id).toBe('mates:qa-bot')
  })

  it('does not let a mate with no display label match itself twice', async () => {
    // `crewDisplayName` falls back to `name`, so a naive second pass would score the
    // same string again and float an unlabelled crew above a labelled one that
    // matched just as well. One row, one match.
    const { provider } = build([mate({ name: 'oncall' })])
    const results = await provider.search('oncall')
    expect(results).toHaveLength(1)
    // Matched on the label, so the alias penalty must NOT have been applied.
    expect(results[0].score).toBeGreaterThan(0)
  })

  it('renders the description as the subtitle and never highlights it', async () => {
    const { provider } = build()
    const [row] = await provider.search('oncall')
    expect(row.subtitle).toBe('Watches the pager and triages alerts.')
    // The description is not a matched field, so marking it would point at
    // characters the query never found.
    expect(row.subtitleIndices).toBeUndefined()
  })

  it('drops crewmates that match neither the label nor the identity', async () => {
    const { provider } = build()
    expect(await provider.search('zzzzz-no-such-crew')).toEqual([])
  })

  it('does NOT match the description, which would make this a content search', async () => {
    // The ticket asks for a fuzzy NAME match. Matching prose would turn a roster
    // lookup into a search over a different corpus with a different cost — and the
    // row could not honestly highlight it, since the subtitle carries no indices.
    const { provider } = build()
    expect(await provider.search('pager')).toEqual([])
    expect(await provider.search('ledger')).toEqual([])
  })

  it('EXCLUDES templates, which have no chat thread to open', async () => {
    const { provider } = build()
    const all = await provider.search('')
    expect(all.map(r => r.id)).not.toContain('mates:shipper')
    // And a query naming one finds nothing rather than offering a row that would
    // navigate to a Crewmates page unable to open it.
    expect(await provider.search('shipper')).toEqual([])
  })

  it('EXCLUDES the built-in default assistant, which the Crewmates page opens for nobody', async () => {
    // `default` arrives tagged as a member, and the page it would navigate to treats
    // a roster holding only `default` as EMPTY and never opens a thread for it. So a
    // row for it is a row whose Enter lands on a page that will not open what the row
    // named -- the same defect as listing a template, in a second shape.
    const { provider } = build([
      mate({ name: 'default', description: 'The built-in assistant.' }),
      mate({ name: 'oncall', description: 'Watches the pager.' }),
    ])
    expect((await provider.search('')).map(r => r.title)).toEqual(['oncall'])
    expect(await provider.search('default')).toEqual([])
  })

  it('shows the EMPTY state for a fresh install that has only the default assistant', async () => {
    // What a fresh install actually holds. Unfiltered, the first thing a new reader
    // sees in this view is a row that cannot be opened; filtered, they get the empty
    // state, which is the honest answer and names where to make a crewmate.
    const { provider } = build([mate({ name: 'default' })])
    expect(await provider.search('')).toEqual([])
  })

  it('keeps a MEMBER whose name a template also carries', async () => {
    // The catalog answers with both namespaces and the two can share a `name`, so
    // this filters on the kind rather than de-duplicating by name — which would be
    // a coin flip over which row survived.
    const { provider } = build([
      mate({ name: 'atlas', description: 'The member.' }),
      mate({ name: 'atlas', description: 'The template.', selection_kind: 'template' }),
    ])
    const results = await provider.search('atlas')
    expect(results).toHaveLength(1)
    expect(results[0].subtitle).toBe('The member.')
  })

  it('lists every crewmate alphabetically for an empty query', async () => {
    const { provider } = build()
    const results = await provider.search('')
    // By the DISPLAYED label, case-insensitively: `reviewer` files under r, not
    // under the q of the identity behind it.
    expect(results.map(r => r.title)).toEqual([
      'accountant',
      'oncall',
      'Review & QA',
      'reviewer',
    ])
  })

  it('breaks a score tie alphabetically', async () => {
    const { provider } = build([mate({ name: 'ab' }), mate({ name: 'aa' })])
    expect((await provider.search('a')).map(r => r.title)).toEqual(['aa', 'ab'])
  })
})

describe('createMatesProvider — running state', () => {
  it('says BUSY in the one word the bar already uses for it, not a second one', async () => {
    // The sessions view is one keystroke away and shows the same dot, colour and
    // pulse for a running agent. Two different WORDS for one state left a reader
    // unable to tell whether they meant the same thing.
    const { provider } = build(ROSTER, ['oncall'])
    const [row] = await provider.search('oncall')
    expect(row.statusLabel).toBe(
      i18nT('components.commandPalette.providers.recentsProvider.thinking'),
    )
    expect(row.statusColorVar).toBe('--accent')
  })

  it('marks a working crewmate with a DOT, never the pill reserved for what the reader owes', async () => {
    const { provider } = build(ROSTER, ['oncall'])
    const [row] = await provider.search('oncall')
    expect(row.statusStyle).toBe('dot')
    expect(row.statusLabel).toBeTruthy()
    expect(row.statusPulse).toBe(true)
    // A pill says the session is parked on the reader. "Busy" must never look like
    // "needs me".
    expect(row.statusStyle).not.toBe('pill')
  })

  it('leaves an idle crewmate with no status at all', async () => {
    const { provider } = build(ROSTER, ['oncall'])
    const [row] = await provider.search('accountant')
    expect(row.statusStyle).toBeUndefined()
    expect(row.statusLabel).toBeUndefined()
    expect(row.statusColorVar).toBeUndefined()
  })

  it('asks the predicate with the IDENTITY, not the display label', async () => {
    // The running set is built from member slots, whose `agent` is the crew's exact
    // name — so a provider asking with the label would report a renamed crew as idle
    // while it is working.
    const isRunning = vi.fn().mockReturnValue(false)
    const provider = createMatesProvider({
      fetchMates: async () => [mate({ name: 'qa-bot', display_name: 'reviewer' })],
      openMate: vi.fn(),
      isRunning,
    })
    await provider.search('reviewer')
    expect(isRunning).toHaveBeenCalledWith('qa-bot')
    expect(isRunning).not.toHaveBeenCalledWith('reviewer')
  })
})

describe('createMatesProvider — activation', () => {
  it('opens the mate on Enter, through both the declarative action and the closure', async () => {
    const { provider, openMate } = build()
    const [row] = await provider.search('oncall')

    row.onActivate()
    expect(openMate).toHaveBeenCalledWith('oncall')

    // The declarative `enter` contract is what the central dispatcher reads, and it
    // is also what makes the row copyable; it must name the same destination.
    expect(row.enter).toEqual({ kind: 'navigate', route: '/members?member=oncall' })
  })

  it('ENCODES the crew name in the route', async () => {
    // A crew may legitimately be called `Review & QA`. Unencoded, the `&` ends the
    // parameter and the page opens on nothing.
    const { provider, openMate } = build()
    const [row] = await provider.search('Review & QA')
    expect(row.enter).toEqual({
      kind: 'navigate',
      route: '/members?member=Review%20%26%20QA',
    })
    // The closure hands over the RAW name — encoding belongs to the route builder,
    // so a caller cannot double-encode it.
    row.onActivate()
    expect(openMate).toHaveBeenCalledWith('Review & QA')
  })

  it('builds the same route from the exported helper the overlay uses', async () => {
    // Two spellings of one route is how one of them ends up wrong; the provider and
    // the overlay's own navigation must come from this single builder.
    const { provider } = build()
    const [row] = await provider.search('oncall')
    expect(row.enter).toEqual({ kind: 'navigate', route: mateRoute('oncall') })
  })

  it('leaves ⌘Enter unbound so the dispatcher falls back to the plain open', async () => {
    // A crewmate has no split-pane or new-session variant — its thread is durable and
    // pinned, so there is only one of it. Documented fallback, asserted so a future
    // edit cannot make ⌘Enter silently inert.
    const { provider } = build()
    const [row] = await provider.search('oncall')
    expect(row.onCmdActivate).toBeUndefined()
  })

  it('navigates nothing merely by searching', async () => {
    const { provider, openMate } = build()
    await provider.search('onc')
    expect(openMate).not.toHaveBeenCalled()
  })
})

describe('createMatesProvider — palette contract', () => {
  it('declares no minimum query length, because the corpus is in hand once fetched', async () => {
    const { provider } = build()
    expect(provider.minQueryChars).toBeUndefined()
    // One character must therefore actually narrow.
    expect((await provider.search('o')).length).toBeGreaterThan(0)
  })

  it('tags every row with its provider id and an icon', async () => {
    const { provider } = build()
    const rows = await provider.search('')
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) {
      expect(row.providerId).toBe('mates')
      // The avatar IS the icon here — it is what identifies the row in a list of
      // faces the reader already recognises.
      expect(row.icon).toBeTruthy()
    }
  })

  it('fetches only when searched, so constructing the provider costs no request', async () => {
    const fetchMates = vi.fn().mockResolvedValue([])
    const provider = createMatesProvider({ fetchMates, openMate: vi.fn(), isRunning: () => false })
    // This is the property that keeps the launcher's first page request-free: the
    // engine is built on every render of the overlay and must stay inert until the
    // view is entered.
    expect(fetchMates).not.toHaveBeenCalled()
    await provider.search('')
    expect(fetchMates).toHaveBeenCalledTimes(1)
  })

  it('survives a stored crew whose name or description is not a string', async () => {
    // The values come off the config file on disk, so a hand edit or an older writer
    // can leave a number, null or an object there. `fuzzyMatch` calls
    // `.toLowerCase()` on its candidate, so an unguarded read throws inside the
    // view's own query and takes the launcher down in render.
    const rows = [
      ...ROSTER,
      mate({ name: 7 as unknown as string }),
      mate({ name: null as unknown as string }),
      mate({ name: 'weird', description: { en: 'Deals' } as unknown as string }),
    ]
    const { provider } = build(rows)
    // The well-formed match still comes back…
    expect((await provider.search('onc')).map(r => r.title)).toEqual(['oncall'])
    // …nothing answers to a stringified value…
    expect(await provider.search('object')).toEqual([])
    // …the malformed description is simply absent rather than rendered…
    const [weird] = await provider.search('weird')
    expect(weird.subtitle).toBeUndefined()
    // …and an empty query lists rows without throwing on the malformed ones.
    expect((await provider.search('')).length).toBeGreaterThan(0)
  })
})
