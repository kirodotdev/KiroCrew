import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import { hasBuiltinComponent, getBuiltinComponent } from '../apps/builtinRegistry'
import { describeSourceHealth, type SourcePollHealth } from '../apps/ops-mission-control/api'
import EN_CATALOG from '../i18n/locales/en.json'

/**
 * The English catalog, loaded once. These panels were converted to `i18nT('key')`
 * calls, so the operator-facing copy no longer lives inline in the source — it lives
 * here. A test that reads the raw `.tsx` and matches on a sentence would find only the
 * key. `expandI18n` below folds each key's shipped English back INTO the source so the
 * copy assertions keep verifying the words that reach the screen, while the code-shape
 * assertions still see the surrounding JSX. Mirrors `settingsExtractI18n.test.ts`, which
 * resolves the same `t()` calls to catalog values for the settings search index.
 */
const CATALOG = JSON.parse(
  readFileSync(resolve(__dirname, '../i18n/locales/en.json'), 'utf-8'),
) as Record<string, unknown>

/** Resolve a dotted catalog path (`apps.opsMissionControl.signalsPanel.key`) to its value. */
const catalogValue = (key: string): string | undefined => {
  let node: unknown = CATALOG
  for (const seg of key.split('.')) {
    if (node && typeof node === 'object' && seg in (node as Record<string, unknown>)) {
      node = (node as Record<string, unknown>)[seg]
    } else {
      return undefined
    }
  }
  return typeof node === 'string' ? node : undefined
}

/**
 * Append each `i18nT('key', …)` call's resolved English right after the call, so a raw
 * source read sees both the code (`i18nT('…')`, its interpolation args) AND the shipped
 * copy. Appending rather than replacing keeps the structural assertions — which match the
 * key string or the interpolation expression — working unchanged.
 *
 * The resolved copy is appended as a bare `⟦…⟧` marker, NOT a comment, so it survives
 * `rendered()` (which strips comments): several copy assertions run against `rendered()`.
 */
const expandI18n = (source: string): string =>
  source.replace(/i18nT\(\s*'([^']+)'/g, (whole, key: string) => {
    const value = catalogValue(key)
    return value === undefined ? whole : `${whole} ⟦${value}⟧`
  })

const readPanel = (name: string) =>
  expandI18n(readFileSync(resolve(__dirname, `../apps/ops-mission-control/${name}`), 'utf-8'))

/**
 * A panel's source with comments removed, for assertions that BAN a phrase.
 *
 * House style here is to record what a wrong behaviour cost, in a comment, next to the fix —
 * so a bare `not.toMatch` over the raw file forbids explaining the very thing it enforces,
 * and the choice becomes "keep the guard" or "keep the reason". This keeps both: the guard
 * reads only text that can reach the screen.
 *
 * Deliberately crude (it does not parse JSX or respect comment-like sequences inside string
 * literals) because the failure direction is safe: a stripped-too-much file can only make a
 * ban assertion pass on text it should have caught, and every such ban is paired with a
 * positive assertion on the replacement wording — which is checked against the raw source.
 */
const rendered = (source: string) =>
  source.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/^\s*\/\/.*$/gm, ' ')

/**
 * Ops Mission Control is a builtin page. Its route must be registered and must be
 * a single plain top-level segment — `BuiltinAppRoute` resolves the catch-all
 * `/:builtinApp` from ONE path parameter, so a multi-segment route would register
 * but never resolve (navigation silently redirects to chat).
 */
describe('ops-mission-control builtin registration', () => {
  const ROUTE = '/ops-mission-control'

  it('is registered in the builtin component registry', () => {
    expect(hasBuiltinComponent(ROUTE)).toBe(true)
  })

  it('resolves to a lazy component', () => {
    const component = getBuiltinComponent(ROUTE)
    expect(component).toBeDefined()
    expect(component).toHaveProperty('$$typeof')
  })

  it('uses a route shape BuiltinAppRoute can actually resolve', () => {
    // Single leading slash, one segment, no query/hash/whitespace.
    expect(ROUTE).toMatch(/^\/[A-Za-z0-9][A-Za-z0-9._~-]*$/)
  })

  it('matches the manifest route so the sidebar entry and page agree', () => {
    // app.json declares ui.pages[0].route as this exact value; a mismatch means
    // the nav item renders but the page never mounts.
    expect(ROUTE).toBe('/ops-mission-control')
  })

  it('the Playwright stat-card assertions match the labels the page renders', () => {
    // A renamed StatCard silently breaks the browser spec, and the browser spec only
    // runs in the opt-in E2E gate — so the mismatch survived until that gate ran.
    // This is the cheap check that fails in the default `npm run test` instead.
    // `readPanel` expands each `label={i18nT('key')}` into `… ⟦English⟧`, so the labels
    // read the shipped copy whether they are plain strings or catalog references.
    const page = readPanel('OpsMissionControlPage.tsx')
    const spec = readFileSync(
      resolve(__dirname, '../../playwright/ops-mission-control.spec.ts'),
      'utf-8',
    )
    const rendered = [
      ...[...page.matchAll(/<StatCard\s+label="([^"]+)"/g)].map((m) => m[1]),
      ...[...page.matchAll(/<StatCard\s+label=\{i18nT\('[^']+' ⟦([^⟧]+)⟧/g)].map((m) => m[1]),
    ]
    expect(rendered.length).toBeGreaterThan(0)

    const asserted = spec.match(/for \(const label of \[([^\]]+)\]\)/)
    expect(asserted, 'the stat-card loop must still exist in the spec').toBeTruthy()
    for (const label of rendered) {
      expect(
        asserted![1],
        `StatCard "${label}" is rendered but the Playwright spec does not assert it`,
      ).toContain(label)
    }
  })
})

/**
 * The ledger is the app's central premise: on a second occurrence the responder should
 * read what worked last time. The board used to render that as the number "2 matched",
 * which is the payoff reduced to a count — the actual pattern and fix were only visible
 * by opening the agent's chat transcript.
 */
describe('ops-mission-control renders the remembered fix, not just a count', () => {
  const page = readPanel('OpsMissionControlPage.tsx')

  it('resolves matched entry ids against the ledger it already fetches', () => {
    // Ids alone cannot be rendered; without this lookup the panel can only show a count.
    expect(page).toContain('ledgerById')
    expect(page).toMatch(/ledger_matches\.map\(/)
  })

  it('shows the pattern AND the fix', () => {
    // Matching the right lesson and not showing its remedy is a half-answer — the same
    // reason `ledger_index.entry_text` embeds pattern+fix together rather than pattern.
    expect(page).toMatch(/entry\.pattern/)
    expect(page).toMatch(/entry\.fix/)
  })

  it('shows trust, confidence and use count beside the fix', () => {
    // An unproven `observed/low` entry must not read like a verified one. `use_count` is
    // what decides the agent's fast path, so a human reviewing the same entry needs it.
    for (const field of ['entry.trust', 'entry.confidence', 'entry.use_count']) {
      expect(page, `${field} must be visible so an unproven entry cannot look proven`).toContain(
        field,
      )
    }
  })

  it('says so when a matched entry is no longer in the ledger', () => {
    // Hygiene prunes and decays. Rendering nothing for a missing id would read as "no
    // prior knowledge", which is the opposite of what happened.
    expect(page).toMatch(/no longer in the\s*\n?\s*ledger/)
  })

  it('does not use an emoji for the trust signal', () => {
    // Repo convention: lucide icons or text, never emoji in the UI.
    const block = page.slice(page.indexOf('ledger_matches.map('))
    expect(block.slice(0, 2000)).not.toMatch(/[\u{1F300}-\u{1FAFF}\u{2700}-\u{27BF}]/u)
  })
})

/**
 * The signature bug of this app is machinery that looks deliberate while doing nothing,
 * and the Signals row reintroduced it in the UI layer: it derived state from
 * `ProviderInfo.configured` (a pure config check) and printed the literal word 'ok'
 * whenever any /signals response existed. A source throttled into backoff — contributing
 * nothing, and known-bad to the backend — therefore rendered "ready / ok", and so did a
 * source that had never been polled at all. An operator reading that row trusted silence.
 */
describe('describeSourceHealth never reports a non-contributing source as ok', () => {
  const ok: SourcePollHealth = { ok: true, detail: '', at: '2026-08-01T00:00:00Z', signals: 2 }

  it('reports a source in backoff as backing off, not ok', () => {
    // The exact string registry.poll_all emits for a throttled skip. `configured` is true
    // and there is no poll_health failure, which is precisely the combination that used to
    // render green.
    const health = describeSourceHealth(
      'cloudwatch',
      {},
      { cloudwatch: 'backing off for another 47s after a prior failure' },
      true,
    )
    expect(health.state).toBe('backing_off')
    expect(health.variant).toBe('warn')
    expect(health.contributing).toBe(false)
    // The backend's own words, so a reworded message is not laundered through our copy.
    expect(health.detail).toContain('47s')
  })

  it('reports a configured but never-polled source as not polled, not ok', () => {
    // poll_all only records health for sources it actually attempted, so absence is a real
    // state. Reading it as healthy is how "we never looked" becomes "nothing is wrong".
    const health = describeSourceHealth('datadog', {}, {}, true)
    expect(health.state).toBe('not_polled')
    expect(health.contributing).toBe(false)
  })

  it('reports a failed poll with the provider reason and its age', () => {
    const health = describeSourceHealth(
      'pagerduty',
      { pagerduty: { ok: false, detail: 'HTTP 401', at: '2026-08-01T00:00:00Z' } },
      {},
      true,
    )
    expect(health.state).toBe('failed')
    expect(health.variant).toBe('err')
    expect(health.detail).toContain('401')
    expect(health.at).toBe('2026-08-01T00:00:00Z')
  })

  it('degrades a reworded backoff message to failed rather than to ok', () => {
    // The backoff branch is a string sniff on prose the backend owns. If that message ever
    // changes the state must fall back to something still-true and still-red; falling back
    // to `ok` would be the same lie in a new costume.
    const health = describeSourceHealth('cloudwatch', {}, { cloudwatch: 'throttled, retrying' }, true)
    expect(health.state).toBe('failed')
    expect(health.state).not.toBe('ok')
  })

  it('only reports ok when a poll actually succeeded', () => {
    const health = describeSourceHealth('cloudwatch', { cloudwatch: ok }, {}, true)
    expect(health.state).toBe('ok')
    expect(health.contributing).toBe(true)
  })

  it('distinguishes an unconfigured source from a broken one', () => {
    // Both are "not watching", but the fixes differ: one is a setup step, the other is a
    // credential or a rate limit.
    expect(describeSourceHealth('datadog', {}, {}, false).state).toBe('not_set_up')
  })

  it('does not let a drained push spool license an inference from absence', () => {
    // `ok` answers "did we look", not "did we see everything", and for the webhook source
    // those differ: its poll DRAINS the spool, so a signal it already delivered is absent
    // from every later cycle whether or not the fault is live. A row reading plain "ok"
    // said "all clear" about a source that structurally cannot report one.
    const spool = describeSourceHealth(
      'webhook',
      { webhook: { ok: true, detail: '', at: '2026-08-01T00:00:00Z', signals: 0, snapshot: false } },
      {},
      true,
    )
    // Still `ok` — nothing is WRONG with it, so it must not be painted as a fault.
    expect(spool.state).toBe('ok')
    expect(spool.contributing).toBe(true)
    // ...but absence from it proves nothing.
    expect(spool.absenceIsEvidence).toBe(false)
  })

  it('defaults a source that says nothing about snapshotting to trustworthy absence', () => {
    // Every polled provider API is a snapshot; only a queue-draining source opts out. An
    // older gateway sends no `snapshot` at all, and reading that as "absence proves nothing"
    // would make every source unverifiable — the fix overshooting into a new wrong answer.
    expect(describeSourceHealth('cloudwatch', { cloudwatch: ok }, {}, true).absenceIsEvidence).toBe(
      true,
    )
  })

  it('never claims absence is evidence for a source that did not answer', () => {
    // One boolean a caller can gate on, instead of remembering both reasons.
    for (const health of [
      describeSourceHealth('pd', { pd: { ok: false, detail: 'HTTP 401', at: '' } }, {}, true),
      describeSourceHealth('cw', {}, { cw: 'backing off for another 47s' }, true),
      describeSourceHealth('dd', {}, {}, true),
      describeSourceHealth('gh', {}, {}, false),
    ]) {
      expect(health.absenceIsEvidence).toBe(false)
    }
  })
})

/**
 * The "every configured source answered" banner is the line an operator reads before
 * trusting a quiet board, and unqualified it became an overstated claim the moment a push
 * source was configured: `all_sources_healthy` is `all(ok)`, and `ok` says we looked, not
 * that we saw everything.
 */
describe('the Signals panel does not overstate what a successful poll proves', () => {
  const panel = readPanel('SignalsPanel.tsx')

  it('qualifies the all-clear banner by naming the push sources it excludes', () => {
    expect(panel).toMatch(/pushSources/)
    // The copy now lives in the CATALOG, not the source: the qualifier used to interpolate an
    // English `verb` fragment ("it delivers"/"they deliver") into a translated sentence, which
    // rendered English mid-sentence in all nine non-English locales and which no catalog value
    // could repair. Both whole sentences are catalog plurals now, so the assertion follows the
    // copy there — and checks BOTH forms, since a singular-only fix would still read wrong for
    // two sources.
    expect(panel).toMatch(/signalsPanel\.push_source_absence_note/)
    expect(panel).not.toMatch(/verb:/)
    const signalsCopy = EN_CATALOG.apps.opsMissionControl.signalsPanel
    expect(signalsCopy.push_source_absence_note_one).toMatch(/EXCEPT for/)
    expect(signalsCopy.push_source_absence_note_other).toMatch(/EXCEPT for/)
  })

  it('derives that list from absenceIsEvidence rather than re-sniffing the source id', () => {
    // Hardcoding 'webhook' here would be the UI restating a rule the backend owns, and it
    // would silently miss a companion adapter that also drains a queue.
    expect(panel).toMatch(/absenceIsEvidence/)
    expect(rendered(panel)).not.toMatch(/id === 'webhook'/)
  })

  it('marks the source row itself, not only the banner', () => {
    // The per-source row is where an operator reads "0 signal(s) · answered", which is the
    // exact cell that read as an all-clear.
    expect(rendered(panel)).toMatch(/an empty poll means nothing/)
  })
})

describe('the Signals panel renders poll health rather than config', () => {
  const panel = readPanel('SignalsPanel.tsx')

  it('derives the source row from describeSourceHealth, not from `configured`', () => {
    expect(panel).toContain('describeSourceHealth')
    // The exact expressions that produced the misreport.
    expect(panel).not.toMatch(/p\.configured \? 'ready'/)
    expect(panel).not.toMatch(/p\.configured \? 'ok' : 'skipped'/)
  })

  it('counts the Firing column from the state-filtered list', () => {
    // The column headed "Firing" used to count `signals` — every signal the poll returned,
    // any state — so a provider reporting recoveries inflated the number an operator uses
    // to judge blast radius. dispatch claims from `firing`; this column must agree.
    expect(panel).toMatch(/firing\.filter\(\(s\) => s\.source === p\.id\)/)
    expect(panel).not.toMatch(/polled\.filter/)
  })

  it('surfaces all_sources_healthy with a separate no-sources-configured branch', () => {
    // all_sources_healthy is `bool(health) and all(ok)`, so it is FALSE on a fresh install.
    // A two-way branch would tell a brand-new user that one of their sources is failing.
    expect(panel).toContain('all_sources_healthy')
    expect(panel).toContain('anySignalSourceConfigured')
  })

  it('says that absence of a signal is not recovery while a source is unhealthy', () => {
    expect(panel).toMatch(/does NOT mean recovery/)
  })

  it('renders the cleared list, which is evidence rather than an absence', () => {
    expect(panel).toContain('cleared')
    expect(panel).toMatch(/Reported recovered/)
  })
})

/**
 * Provider-side suppression, the UI half.
 *
 * The backend can now say "a human parked this at the provider" (`STATE_SUPPRESSED` plus
 * `suppressed_by`/`suppressed_reason`), and a parked signal that silently vanishes from the
 * board is the same silent-failure bug one layer up: "the app ignored my alarm" and
 * "someone silenced it" look identical to the operator. So the panel must SHOW it — a
 * declared TypeScript field is not parity.
 */
describe('a parked signal is visibly parked, not silently gone', () => {
  const panel = readPanel('SignalsPanel.tsx')

  it('reads the dedicated suppressed bucket rather than sniffing the raw list', () => {
    // Deriving it from `signals` would reintroduce the miscount this bucket exists to fix.
    expect(panel).toMatch(/signalsQuery\.data\?\.suppressed/)
    expect(panel).toMatch(/Parked at the provider/)
  })

  it('never lets a parked signal inflate the Firing column', () => {
    // The two counts answer opposite questions, so they are separate expressions over
    // separate lists. A parked signal counted as firing is how "3 firing" ends up above an
    // empty queue with nothing to explain the contradiction.
    expect(panel).toMatch(/suppressed\.filter\(\(s\) => s\.source === p\.id\)/)
    expect(panel).toMatch(/⟦Parked⟧/)
  })

  it('renders the attribution, and admits when the provider published none', () => {
    // Implying we know who parked it would be the overstated-claim defect; an invented
    // owner is worse than a blank one.
    expect(panel).toContain('suppressed_by')
    expect(panel).toContain('suppressed_reason')
    expect(panel).toMatch(/no attribution/)
  })

  it('distinguishes a silence from an inhibition, because the next move differs', () => {
    // A silence is a person's decision to review or expire; an inhibition means the thing
    // to look at is the OTHER alert and this one is a symptom.
    expect(panel).toMatch(/Silenced by/)
    expect(panel).toMatch(/Inhibited by/)
  })

  it('offers NO action button on a parked signal', () => {
    // dispatch claims only `firing`, so a Claim control here would be the UI asserting an
    // authority the backend refuses — and a 403 after a click is worse than no button.
    const start = panel.indexOf('Parked at the provider')
    expect(start).toBeGreaterThan(-1)
    const card = panel.slice(start, panel.indexOf('</Card>', start))
    expect(card).toContain('suppressed_by')
    expect(card).not.toContain('<Btn')
  })

  it('stops the footer claiming a quiet estate when signals are merely muted', () => {
    // "0 firing" alone reads as all-clear. This is the exact state where that is a lie.
    expect(panel).toMatch(/parked at provider/)
    expect(panel).toMatch(/firing\.length === 0 && suppressed\.length > 0/)
  })

  it('uses a Lucide icon and no emoji', () => {
    expect(panel).toContain('BellOff')
    // eslint-disable-next-line no-control-regex
    expect(panel).not.toMatch(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u)
  })
})

describe('the api layer declares suppression without overstating it', () => {
  const api = readPanel('api.ts')

  it('widens SignalState instead of leaving suppressed untyped', () => {
    expect(api).toMatch(/'firing' \| 'ok' \| 'unknown' \| 'suppressed'/)
  })

  it('declares the two attribution fields with a stated reason', () => {
    expect(api).toContain('suppressed_by')
    expect(api).toContain('suppressed_reason')
  })

  it('declares the suppressed bucket on the /signals result', () => {
    expect(api).toMatch(/suppressed: Signal\[\]/)
  })

  it('declares the cycle count so the Board cannot report a smaller world', () => {
    expect(api).toMatch(/suppressed: number/)
  })

  it('stops understating ActionKind, which the backend has carried since the silence verb', () => {
    expect(api).toMatch(/'ack' \| 'resolve' \| 'comment' \| 'silence'/)
    // The route clamps and echoes the window actually applied; both halves must be typed or
    // a caller would display the window it asked for rather than the one it got.
    expect(api).toMatch(/duration_secs\?: number/)
    expect(api).toMatch(/duration_secs: number \| null/)
  })
})

describe('the Board names the parked count it saw', () => {
  const page = readPanel('OpsMissionControlPage.tsx')

  it('appends the suppressed count to the nothing-to-claim line', () => {
    // `polled` counts firing only, so this sentence alone reported a smaller world than the
    // cycle saw — on the one surface an operator lands on by default.
    expect(page).toMatch(/dispatchMutation\.data\.suppressed/)
    // The copy moved into the catalog when this sentence became a plural pair (it shipped a
    // draft "signal(s)"), so the assertion follows it there — and checks BOTH forms, since a
    // singular-only conversion would still read wrong for two signals.
    const pageCopy = EN_CATALOG.apps.opsMissionControl.opsMissionControlPage
    expect(pageCopy.polled_summary_with_parked_one).toMatch(/parked at the provider/)
    expect(pageCopy.polled_summary_with_parked_other).toMatch(/parked at the provider/)
  })
})

describe('the Board does not assert quiet it has not verified', () => {
  const page = readPanel('OpsMissionControlPage.tsx')

  it('reads the Signals tab cache instead of firing its own paid poll', () => {
    // Same key + enabled:false. A second key, or an enabled query, would make opening the
    // board poll every configured provider — the cost SignalsPanel deliberately avoids.
    expect(page).toContain('SIGNALS_QUERY_KEY')
    expect(page).toMatch(/queryKey: SIGNALS_QUERY_KEY,[\s\S]{0,120}enabled: false/)
  })

  it('does not claim "Nothing is firing" without a verified poll', () => {
    // The old empty state rendered that sentence whenever the incident list was empty, with
    // no reference to whether any source had answered — and a source failing every poll
    // produces exactly that empty list.
    expect(page).toMatch(/not been verified this session/)
    expect(page).toMatch(/all_sources_healthy/)
  })

  it('names the unhealthy sources rather than counting them', () => {
    expect(page).toContain('unhealthySources')
  })
})

describe('the Board states the ledger match basis one-directionally', () => {
  const page = readPanel('OpsMissionControlPage.tsx')

  it('renders provider_key so a shape match cannot pass as an exact one', () => {
    expect(page).toContain('inc.signal.provider_key')
    expect(page).toMatch(/Match basis/)
  })

  it('never re-derives exactness from the entry the incident matched', () => {
    // ledger.record_use BINDS the provider key on match, so from the second occurrence
    // onward `provider_key in entry.provider_keys` is true for every shape match too. The
    // per-lookup truth lives on the dispatch/claim response, which the board does not read.
    expect(page).not.toMatch(/provider_keys\.includes/)
    expect(page).not.toMatch(/exact_match_ids/)
  })
})

describe('the Board reports Slack reply reachability from the transition it made', () => {
  const page = readPanel('OpsMissionControlPage.tsx')

  it('renders slack_thread_replyable from the mutation result', () => {
    expect(page).toContain('slack_thread_replyable')
  })

  it('does not use the thread timestamp as a stand-in for replyability', () => {
    // A linked ts with no live investigation slot is the false positive test_slack_out.py
    // pins: it would promise the operator their Slack reply lands when it will not. Match
    // only READS of the field on an incident, so the comment explaining the rejection is
    // allowed to name it.
    expect(page).not.toMatch(/\binc\.slack_thread_ts\b/)
    expect(page).not.toMatch(/incident\.slack_thread_ts\b/)
  })
})

describe('the Handover coverage card distinguishes configured from working', () => {
  const panel = readPanel('HandoverPanel.tsx')

  it('marks a listed source that the last poll could not read', () => {
    // handover.coverage lists `watching` from `configured` alone, so a source whose every
    // poll 401s appears as coverage. The incoming responder inherits that blind spot.
    expect(panel).toContain('notAnswering')
    expect(panel).toMatch(/not answering/)
  })

  it('does not trigger a paid poll when a digest is opened', () => {
    expect(panel).toMatch(/queryKey: SIGNALS_QUERY_KEY,[\s\S]{0,120}enabled: false/)
  })

  it('says coverage is unverified when no poll has run', () => {
    expect(panel).toMatch(/Derived from configuration only/)
  })

  it('leaves the backend-rendered digest text alone', () => {
    // digest.text is what gets pasted into the handover thread. Editorialising it here
    // would let the paste and the screen word the same shift differently.
    expect(panel).not.toMatch(/digest\.text\s*[+.]\s*(replace|concat)/)
  })
})

describe('the ops panels use theme tokens that actually exist', () => {
  // tailwind.config.js defines ok / warn / danger; there is no `success` or `warning`
  // token and index.css defines no --success/--warning, so `text-success` and
  // `text-warning` silently rendered with NO color override — a warning that was not
  // yellow and a verified badge that was not green.
  for (const file of [
    'OpsMissionControlPage.tsx',
    'SignalsPanel.tsx',
    'HandoverPanel.tsx',
    'SettingsPanel.tsx',
  ]) {
    it(`${file} uses no phantom color class`, () => {
      const src = readPanel(file)
      expect(src).not.toMatch(/text-success\b/)
      expect(src).not.toMatch(/text-warning\b/)
      // Same failure mode, a different set of names: `bg-panel`, `border-line` and
      // `border-subtle` are NOT keys in tailwind.config.js (the palette has `card`,
      // `border`, `border-strong`), so those utilities emit nothing — an invisible border
      // or a background that never applied. The real tokens are `bg-card`/`bg-bg-elevated`
      // and `border-border`. Found in UX review.
      expect(src).not.toMatch(/\bbg-panel\b/)
      expect(src).not.toMatch(/\bborder-line\b/)
      expect(src).not.toMatch(/\bborder-subtle\b/)
    })
  }
})

/**
 * The team memory-exchange repo, the on-call schedule, and sync status.
 *
 * `PUT /settings` accepted `ledger_sync_remote` / `ledger_sync_branch` /
 * `ledger_sync_enabled` and NOTHING in the UI ever sent them, so the app's headline team
 * feature was settable only by hand-editing `data/config.json`. The owner tested against a
 * real repo and reported exactly that: "I do not see where we can specify memory exchange /
 * SOP / on-call schedule repository."
 */
describe('Settings can point this instance at the team repo', () => {
  const panel = readPanel('SettingsPanel.tsx')

  it('sends all three ledger_sync settings keys', () => {
    for (const key of ['ledger_sync_enabled', 'ledger_sync_remote', 'ledger_sync_branch']) {
      expect(panel).toContain(key)
    }
  })

  it('reads sync status from /state rather than inventing a second endpoint', () => {
    expect(panel).toMatch(/stateQuery\.data\?\.ledger_sync/)
  })

  it('never posts an empty branch, because the backend defaults only on an absent key', () => {
    // `ledger_sync.branch()` falls back to main when the key is MISSING; persisting "" would
    // store an empty branch that no default rescues.
    expect(panel).toMatch(/branch\.trim\(\) !== ''/)
  })

  it('surfaces a conflicted schedule as an error, not as a note', () => {
    // The one sync state that lies: `push` refuses outright while rotation.yaml holds
    // markers, so a card reading "syncing" would describe an indefinite publishing outage.
    expect(panel).toMatch(/schedule_conflict/)
    expect(panel).toMatch(/schedule conflict/)
    expect(panel).toMatch(/text-danger/)
  })

  it('ranks a schedule conflict above a ledger conflict', () => {
    // A ledger conflict is reconcilable (content-addressed ids union); a schedule conflict
    // blocks every push. Reversing them would let the recoverable state mask the fatal one.
    // The badge labels are catalog keys now, and `readPanel` inserts ⟦…⟧ after the key
    // literal — so the anchor stops at the closing quote, before that marker.
    const ledgerConflictAt = panel.indexOf("label: i18nT('apps.opsMissionControl.settingsPanel.ledger_conflict'")
    expect(ledgerConflictAt).toBeGreaterThan(-1)
    expect(panel.indexOf('schedule_conflict')).toBeLessThan(ledgerConflictAt)
  })

  it('renders the backend detail verbatim instead of re-wording the failure', () => {
    expect(panel).toMatch(/\{status\.detail\}/)
  })
})

/**
 * The branch pair, which is where the same backend-only-fix drift reappeared.
 *
 * `ledger_sync` published to the configured branch through explicit refspecs while the
 * local repo sat on git's own default (`master`), so `status()` claimed "on branch main"
 * about a repo that was not on it — and the operator's own `git pull` in that directory
 * failed with no upstream. The backend now reports `local_branch` / `branch_matches` /
 * `detached`; a card that declared them and painted nothing would be the same class of
 * miss as the SignalsPanel "ready / ok" row.
 */
describe('Settings shows when the local repo drifted off the configured branch', () => {
  const panel = readPanel('SettingsPanel.tsx')

  it('gates its branch warning on branch_matches, not on a comparison it re-derives', () => {
    // `branch_matches` is deliberately TRUE for an uninitialized repo — nothing yet to
    // disagree with. A panel comparing the two strings itself would warn on every fresh
    // install and train the operator to ignore the field.
    expect(panel).toMatch(/status\?\.ready && status\.initialized && !status\.branch_matches/)
    expect(panel).not.toMatch(/status\.local_branch !== status\.branch/)
  })

  it('names which branch the repo is actually on', () => {
    expect(panel).toMatch(/status\.local_branch/)
    expect(panel).toMatch(/This repo is on/)
  })

  it('distinguishes a detached HEAD from a plain mismatch, because the fixes differ', () => {
    // A mismatch the next sync repairs by itself; a detached HEAD is left alone on purpose,
    // so telling the operator to wait would leave them waiting forever.
    expect(panel).toMatch(/status\.detached/)
    expect(panel).toMatch(/detached HEAD/)
  })

  it('does not overstate the drift as an outage', () => {
    // Publishing genuinely still works — the refspecs are explicit. Painting this as an
    // error would be the overstated claim in the other direction; it is a warn, and the
    // copy says the exchange is still running.
    expect(panel).toMatch(/wrong local branch/)
    expect(panel).toMatch(/still being exchanged/)
    // Ranked BELOW both conflicts in the badge chain (a warn that does not mask a fatal
    // state) and ABOVE 'syncing', which is the label that hid this for the whole life of
    // the feature. Anchored on the branch guard rather than on the label text, because the
    // label itself is a detached/mismatch ternary.
    const drift = panel.indexOf(': branchDrifted')
    expect(drift).toBeGreaterThan(
      panel.indexOf("label: i18nT('apps.opsMissionControl.settingsPanel.ledger_conflict'"),
    )
    expect(drift).toBeLessThan(
      panel.indexOf("label: i18nT('apps.opsMissionControl.settingsPanel.syncing'"),
    )
  })
})

describe('the remote URL is displayed without a credential in it', () => {
  it('strips a userinfo component before painting a remote', async () => {
    const { displayRemote } = await import('../apps/ops-mission-control/SettingsPanel')
    // config.json is served UNAUTHENTICATED and `redact_tokens` has no pattern for a PAT
    // inside a URL, so this panel declines to be a second place the token is shown.
    expect(displayRemote('https://user:ghp_secret@github.com/org/repo.git')).toBe(
      'https://github.com/org/repo.git',
    )
  })

  it('leaves an scp-style SSH remote exactly as typed', () => {
    // `git@github.com:org/repo.git` has no userinfo to strip; a naive split on '@' would
    // mangle the recommended remote into `github.com:org/repo.git`.
    return import('../apps/ops-mission-control/SettingsPanel').then(({ displayRemote }) => {
      expect(displayRemote('git@github.com:org/repo.git')).toBe('git@github.com:org/repo.git')
    })
  })

  it('does not mangle a plain https remote or a path containing @', () => {
    return import('../apps/ops-mission-control/SettingsPanel').then(({ displayRemote }) => {
      expect(displayRemote('https://github.com/org/repo.git')).toBe(
        'https://github.com/org/repo.git',
      )
      expect(displayRemote('https://host/org/re@po.git')).toBe('https://host/org/re@po.git')
    })
  })

  it('claims no more than it does', () => {
    // An overstated claim is a defect here: the panel must say auth is the operator's own
    // git config, not that anything was sanitised on the way in.
    const panel = readPanel('SettingsPanel.tsx')
    expect(panel).toMatch(/No credential to enter/)
    expect(panel).not.toMatch(/we (strip|remove|redact)/i)
  })
})

describe('Settings explains where the on-call schedule lives', () => {
  const panel = readPanel('SettingsPanel.tsx')

  it('names rotation.yaml, which was documented only in Python and the module spec', () => {
    expect(panel).toContain('rotation.yaml')
  })

  it('shows the shape, including every key the parser reads', () => {
    for (const key of ['leader:', 'timezone:', 'shifts:', 'from:', 'to:', 'who:']) {
      expect(panel).toContain(key)
    }
  })

  it('states the date-only `to` rule, the likeliest misreading of the format', () => {
    expect(panel).toMatch(/THROUGH that whole day/)
  })

  it('says the location is fixed rather than offering a path field', () => {
    expect(panel).toMatch(/no path to pick/i)
  })

  it('writes the on-call login through the authenticated settings route, not provider config', () => {
    // The login moved onto the keystone floor: it is an input to the authorization decision
    // (`_definitely_off_shift`), and provider config is agent-writable and served
    // unauthenticated, so a login stored there could be forged to defeat the off-shift
    // refusal. `PUT /settings` is its only writer now, and the panel must use it.
    expect(panel).toMatch(/putSettings\(\{ schedule_github_login/)
    expect(panel).not.toMatch(/putProviderConfig\('schedule-file'/)
  })

  it('offers strict gating here, because fencing it removed the provider-config path', () => {
    // Same fence, same reason: turning strict gating off restores fail-open gating, so an
    // unreadable schedule reports "on shift" and the refusal stops firing. Fencing it took
    // away the old provider-config control, so the panel has to supply the new one or the
    // setting becomes unreachable.
    expect(panel).toMatch(/putSettings\(\{ schedule_strict_gating/)
  })

  it('explains the two idle-instance setup mistakes where the operator can fix them', () => {
    expect(panel).toMatch(/me_on_roster/)
    expect(panel).toMatch(/never pick up work/)
    expect(panel).toMatch(/cannot tell whether it is/)
  })
})

describe('a provider row cannot paint a control the backend will reject', () => {
  const panel = readPanel('SettingsPanel.tsx')

  it('renders the enable toggle only for adapters that declare an enabled field', () => {
    // `_handle_put_provider_config` 400s any undeclared key, and the rotation adapters
    // declare none — schedule-file now declares NOTHING either, since both of its fields
    // moved onto the keystone floor. Its toggle could never latch, so its config fields
    // were unreachable.
    expect(panel).toMatch(/config_fields\.includes\('enabled'\)/)
  })

  it('shows config fields for an adapter that has no enable flag', () => {
    expect(panel).toMatch(/enabled \|\| !hasEnableFlag|fieldsVisible/)
  })

  it('reports a rejected write outside the block the toggle gates', () => {
    // The error <p> used to live inside `enabled ? …`, so the click that failed most often
    // failed in complete silence.
    const errorAt = panel.indexOf('writeError ? (')
    const blockEnd = panel.indexOf('OUTSIDE the block the enable toggle gates')
    expect(blockEnd).toBeGreaterThan(0)
    expect(errorAt).toBeGreaterThan(blockEnd)
  })
})

/**
 * The postmortem the app writes when an incident closes.
 *
 * `store.write_log` rendered the complete artifact for the whole life of the app and had
 * exactly one reference — its own definition — so `incidents/<id>.md` was documented on-disk
 * state that could not exist. `read_log` was nonetheless SERVED at `/incident` and TYPED in
 * api.ts, and no component called it: the exact declared-but-dead shape this suite exists to
 * catch. These tests pin the reader, because the writer was never the missing half.
 */
describe('the Board renders the artifact a colleague gets handed', () => {
  const page = readPanel('OpsMissionControlPage.tsx')

  it('calls the incident route that carries the postmortem', () => {
    // The assertion that would have failed before this shipped.
    expect(page).toMatch(/opsApi\.incident\(/)
  })

  it('lists closed incidents, which /state deliberately omits', () => {
    // `/state` returns store.open_incidents() only, so a resolved incident leaves every
    // surface the instant it resolves. `/incidents` is the only way back to it.
    expect(page).toMatch(/opsApi\.incidents\(\)/)
    expect(page).toMatch(/CLOSED_STATUSES/)
  })

  it('derives closed from the two terminal statuses, not from a single filter', () => {
    // The route's `status` filter takes ONE value and terminal is two, so filtering
    // server-side would silently drop every escalation.
    expect(page).toMatch(/'resolved', 'escalated'/)
  })

  it('refreshes closed history from the transition that closes an incident', () => {
    // Without this the row simply vanishes: it drops off /state and the closed list has no
    // polling of its own, so the postmortem the close just wrote would not appear until a
    // manual reload.
    expect(page).toMatch(/invalidateQueries\(\{ queryKey: CLOSED_QUERY_KEY \}\)/)
  })

  it('renders the artifact verbatim rather than through the Markdown renderer', () => {
    // The operator is looking at the exact bytes a colleague will receive, and a rendered
    // view makes a redaction marker easy to miss. Matched as an ELEMENT and an import, so
    // the comment that records the rejection is still allowed to name it.
    expect(page).toMatch(/<pre className="font-mono/)
    expect(page).not.toMatch(/<MarkdownRenderer/)
    expect(page).not.toMatch(/import .*MarkdownRenderer/)
  })

  it('offers a copy control for the postmortem text', () => {
    expect(page).toMatch(/Copy postmortem/)
    expect(page).toMatch(/navigator\.clipboard\.writeText\(log\)/)
  })

  it('distinguishes "no artifact" from an empty one', () => {
    // Empty means either still open or closed before the writer existed — never "the
    // investigation found nothing", which is what a blank <pre> would imply.
    expect(page).toMatch(/No postmortem was written/)
  })

  it('never synthesizes the on-disk path', () => {
    // KIROCREW_HOME moves the data directory, so a path assembled in the UI would assert a
    // file the backend does not have. It is rendered only when the backend supplies one.
    expect(page).toMatch(/log_path/)
    expect(page).toMatch(/logPath \?/)
    // No template literal assembling a path: the two shapes an assembled one would take.
    expect(page).not.toMatch(/`[^`]*incidents\/\$\{/)
    expect(page).not.toMatch(/\$\{[^}]*incident_id[^}]*\}\.md/)
  })

  it('uses a Lucide icon for the section, never an emoji', () => {
    expect(page).toMatch(/FileText className="lucide-inline"/)
    // eslint-disable-next-line no-control-regex
    expect(page).not.toMatch(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u)
  })

  it('says when the capped list is not the whole history', () => {
    // /incidents caps at 200 across ALL statuses, so a long history clips the closed list
    // too — and a clipped list that claims to be complete is how someone concludes an
    // incident vanished.
    expect(page).toMatch(/query\.data\?\.truncated/)
  })
})

describe('the api layer declares the postmortem without overstating it', () => {
  const api = readPanel('api.ts')

  it('declares both fields on the incident envelope', () => {
    expect(api).toMatch(/incident: Incident; log: string; log_path: string/)
  })

  it('documents that an empty log has two different meanings', () => {
    expect(api).toMatch(/EMPTY in two different situations/)
  })

  it('forbids guessing the path in the UI', () => {
    expect(api).toMatch(/KIROCREW_HOME/)
  })

  it('names the closed-history section as the incidents route caller', () => {
    // The route was declared, correct about truncated/total, and called by nothing.
    expect(api).toMatch(/Closed — postmortems/)
  })
})

/**
 * Local desktop notifications (§5.6).
 *
 * `app.json` declared the `notification` event permission from the app's first commit and
 * the app never produced one, so the ONE push channel Kiro Crew offers that needs no
 * credential and no inbound URL was inert. Wiring the backend alone would have repeated the
 * failure this run exists to stop: an operator has to be able to SEE that channels exist
 * and turn them off, or the feature is machinery that looks deliberate while doing nothing.
 */
describe('Settings surfaces the notification channels and their on/off', () => {
  const panel = readPanel('SettingsPanel.tsx')

  it('writes the app-level toggle through the settings route', () => {
    expect(panel).toMatch(/notify_enabled/)
  })

  it('renders the channel list the backend declares', () => {
    // The DECLARATION, not the bus registry: registration is lazy, so the central rail
    // shows nothing for a fresh install until a notification fires.
    expect(panel).toMatch(/status\?\.channels/)
    expect(panel).toMatch(/CHANNEL_WHEN/)
  })

  it('reads readiness from the backend and never asserts active on the toggle alone', () => {
    // `enabled` true in a process with no bus delivers nothing; painting that as active
    // would be the overstated claim. The label is gated on `status.ready` and resolves to
    // "active" (extracted to a catalog key; `readPanel` folds the English in as ⟦…⟧ right
    // after the key literal).
    expect(panel).toMatch(/status\.ready\s*\n?\s*\?\s*i18nT\('[^']*' ⟦active⟧/)
  })

  it('renders the backend detail sentence rather than re-deriving the fix', () => {
    expect(panel).toMatch(/status && !status\.ready && enabled/)
  })

  it('points at the central rail instead of duplicating per-channel mute', () => {
    // Kiro Crew stores per-channel mute centrally; a second control here would be two
    // controls that can disagree about one stored setting.
    expect(panel).toMatch(/Settings → Notifications/)
  })

  it('describes the edge condition, not a recurring alert', () => {
    // The contract is one push per STATE CHANGE. Copy saying "while a source is down"
    // would promise a repeat the backend deliberately does not send.
    //
    // Read from the CATALOG, not the source: the copy moved out of an ALL-CAPS literal
    // table into `channel_when_*` keys (which is what put it back under the i18n gate).
    // `expandI18n` only resolves `i18nT('literal')` call sites, and these keys are values
    // in a map, so the catalog is where the wording now lives.
    //
    // The panel must still reference the map, or the copy could be correct and unrendered.
    expect(panel).toMatch(/CHANNEL_WHEN_KEY/)
    expect(catalogValue('apps.opsMissionControl.settingsPanel.channel_when_source_health'))
      .toMatch(/not again while it stays down/)
    expect(panel).toMatch(/never per heartbeat/)
  })

  it('uses Lucide icons for the card and every channel, never an emoji', () => {
    expect(panel).toMatch(/BellRing className="lucide-inline"/)
    expect(panel).toMatch(/UserCheck className="lucide-inline"/)
    // eslint-disable-next-line no-control-regex
    expect(panel).not.toMatch(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u)
  })

  it('falls back to an icon for a channel this map does not know', () => {
    // A channel the manifest gains later must still render — an unexplained channel beats
    // a hidden one.
    expect(panel).toMatch(/CHANNEL_ICONS\[ch\.icon\] \?\? <Bell/)
  })
})

describe('the api layer declares the notification status without overstating it', () => {
  const api = readPanel('api.ts')

  it('declares the status shape on the board payload', () => {
    expect(api).toMatch(/notify\?: NotifyOutStatus/)
    expect(api).toMatch(/export interface NotifyOutStatus/)
  })

  it('declares the channel shape the panel renders', () => {
    expect(api).toMatch(/export interface NotifyChannel/)
    expect(api).toMatch(/default_priority: string/)
  })

  it('names ready as the only gate, matching the Slack rule', () => {
    expect(api).toMatch(/`ready` is the only field to gate the happy path on/)
  })

  it('explains why bus_available is a different problem from disabled', () => {
    // A CLI or test process holds no DashboardState, so no toggle would help.
    expect(api).toMatch(/not the gateway process/)
  })

  it('explains why the channel list is declared here at all', () => {
    // The central rail lists REGISTERED channels and registration is lazy, so a fresh
    // install shows nothing there until the first push.
    expect(api).toMatch(/registration is lazy/)
  })

  it('names notify_enabled as an accepted settings key', () => {
    expect(api).toMatch(/`notify_enabled` turns local desktop notifications on/)
  })
})

/**
 * The ledger's TRACK RECORD, and what a remembered fix now means.
 *
 * `is_fast_path` used to be satisfied by any single `verified`+`high` match with no use
 * condition at all, and `POST /ledger` takes both values verbatim — so one hand-authored
 * entry unlocked "propose this fix directly" for a production failure having never been
 * applied to anything. The exact-identity layer made it worse rather than better:
 * `record_use` binds the provider key on the first match, so from occurrence two onward
 * that same single piece of evidence presents as an EXACT match.
 *
 * The bar now has four conditions and there is a mechanical downward path. Since that
 * changes what a match MEANS, the Board cannot keep presenting matches the way it did.
 */
describe('the Board shows a matched pattern track record, not just a use count', () => {
  const page = readPanel('OpsMissionControlPage.tsx')

  it('renders the miss count beside the use count', () => {
    // `use_count` increments at CLAIM time, before any outcome exists, so on its own it
    // means "was shown to somebody". An operator reading "used 4×" as corroboration is
    // reading the wrong number.
    expect(page).toMatch(/entry\.miss_count > 0/)
    // "failed" was extracted to a catalog key; `readPanel` folds it back as ⟦failed⟧
    // right after the key, and the count follows the call.
    expect(page).toMatch(/⟦failed⟧\)\}\s*\{entry\.miss_count\}/)
  })

  it('says whether the fast path is unlocked for each match', () => {
    // "How proven is this" is the question an operator trusting a remembered fix has, and
    // trust+confidence+uses individually do not answer it.
    expect(page).toMatch(/entryIsProven\(entry\)/)
    expect(page).toMatch(/agent may propose directly/)
    expect(page).toMatch(/agent must confirm first/)
  })

  it('asks the engine rather than re-deriving the bar in the panel', () => {
    // `handover.recurring_patterns` restated "verified and high" and went stale the moment
    // the bar gained a use floor. A panel that disagrees with the brief the agent was
    // handed leaves the operator no way to tell which of the two is lying.
    //
    // Asserted on the BADGE decision rather than as a blanket ban on reading trust and
    // confidence together: the panel legitimately inspects both to explain WHY an
    // otherwise-strong entry fell short of the use floor, which is a different job from
    // deciding the verdict. Every place the verdict is decided must call the shared
    // predicate, in this file and in the api layer that defines it.
    const api = readPanel('api.ts')
    expect(api).toMatch(/export function entryIsProven/)
    expect(api).toMatch(/entry\.use_count >= MIN_USES_FOR_FAST_PATH/)
    expect(api).toMatch(/entry\.miss_count === 0/)
    for (const verdict of [
      /variant=\{entryIsProven\(entry\) \? 'ok' : 'muted'\}/,
      // The badge label is decided by the shared predicate and resolves to the "proven"
      // copy (extracted to a catalog key; `readPanel` folds the English in as ⟦…⟧ right
      // after the key literal).
      /entryIsProven\(entry\)\s*\n?\s*\? i18nT\('[^']*' ⟦proven/,
    ]) {
      expect(page).toMatch(verdict)
    }
  })

  it('declares a demoted entry as demoted, in words and not only as a number', () => {
    // A refuted fix is worth strictly LESS than an untested one — the opposite of how a
    // high use count reads — so the number alone is not the fact.
    expect(page).toMatch(/Demoted: this fix was applied/)
  })

  it('explains a hypothesis whose only shortfall is the new use floor', () => {
    // Otherwise the badge is a verdict with no visible cause on an entry that reads
    // verified/high, which is exactly the confusing case the floor introduces.
    expect(page).toMatch(/MIN_USES_FOR_FAST_PATH/)
    expect(page).toMatch(/anyone can record those two/)
  })

  it('shows the failure column and the fast-path state in the ledger table', () => {
    const table = page.slice(page.indexOf('Knowledge ledger'))
    // The `Failed` and `Fast path` column headers were extracted to catalog keys;
    // `readPanel` folds their English back in as ⟦…⟧.
    expect(table).toMatch(/⟦Failed⟧/)
    expect(table).toMatch(/⟦Fast path⟧/)
    // An em dash for zero, not a column of noisy zeroes competing with the rows that matter.
    expect(table).toMatch(/entry\.miss_count > 0 \? `\$\{entry\.miss_count\}×` : '—'/)
  })

  it('states what the fast path actually gates', () => {
    // Before the floor landed an agent proposed a remembered fix on two hand-settable
    // fields and an operator reading the table had no way to know that.
    expect(page).toMatch(/needs all four of verified trust, high/)
  })

  it('separates patterns proven from patterns known', () => {
    // `total` counts guesses nobody has applied; `verified` and `high_confidence` are each
    // one HALF of the bar. None of the three answers what an agent would propose.
    expect(page).toMatch(/label=\{i18nT\('[^']*' ⟦Patterns proven⟧/)
    expect(page).toMatch(/state\?\.ledger\?\.proven/)
  })

  it('uses no emoji for any of it', () => {
    const block = page.slice(page.indexOf('ledger_matches.map('))
    expect(block.slice(0, 6000)).not.toMatch(/[\u{1F300}-\u{1FAFF}\u{2700}-\u{27BF}]/u)
  })
})

/**
 * Post-action verification. `ActionResult.ok` means "the provider returned 2xx" and
 * nothing more — Checkmk documents that gap explicitly for its asynchronous Livestatus
 * command dispatch, and Nagios's command pipe returns nothing at all. Nothing re-read the
 * signal, so the board could report an applied fix that never took effect.
 */
describe('the Board says whether anything checked that an action landed', () => {
  const page = readPanel('OpsMissionControlPage.tsx')
  const api = readPanel('api.ts')

  it('renders the verdict through the shared describer, not a local wording', () => {
    // Five states worded in one place, for the same reason `describeSourceHealth` lives in
    // api.ts: `unknown` in particular is easy to word as either success or failure.
    expect(page).toMatch(/describeVerification\(inc\)/)
    expect(api).toMatch(/export function describeVerification/)
  })

  it('renders nothing at all when no action was taken', () => {
    // Empty `verification` means "nothing was attempted", which is true of almost every
    // incident — a "not applicable" row on each would bury the ones that matter.
    expect(api).toMatch(/Returns `null` when there is nothing to say/)
    expect(page).toMatch(/if \(!view\) return null/)
  })

  it('never calls an unverifiable action verified', () => {
    // An ack leaves an alert firing BY DESIGN, so firing state proves nothing about it.
    expect(api).toMatch(/sent, not confirmed/)
    expect(api).toMatch(/leaves an alert firing by design/)
  })

  it('never reads a failed poll as the action having worked', () => {
    // The bug class the reconcile fix just closed. `unknown` is warn, not err and not ok:
    // the action may well have worked, we could not look.
    expect(api).toMatch(/could not check/)
    expect(api).toMatch(/proves nothing either way/)
    expect(api).not.toMatch(/case 'unknown':[\s\S]{0,200}variant: 'ok'/)
  })

  it('renders the backend detail verbatim, because it names which source failed', () => {
    expect(page).toMatch(/\{inc\.verification_detail\}/)
  })

  it('reports a still-firing verdict on the surface an operator lands on', () => {
    // This is the app retracting its own claim, which is why it interrupts — while
    // `cleared` deliberately says nothing, or the button would congratulate itself.
    expect(page).toMatch(/v === 'still_firing'/)
    expect(page).toMatch(/reported as applied/)
  })

  it('does not summarise the verification map as a success rate', () => {
    // `{}` means "nothing was due", not "everything worked".
    expect(api).toMatch(/means "nothing was due", NOT "every action worked"/)
  })

  it('declares every verification field the panel reads', () => {
    for (const field of [
      'last_action?:',
      'last_action_at?:',
      'verification?:',
      'verification_detail?:',
      'verify_after?:',
    ]) {
      expect(api).toContain(field)
    }
  })

  it('declares the fields on the action response too, so a caller stops saying "applied"', () => {
    expect(api).toMatch(/verification: '' \| 'pending' \| 'not_checkable'/)
    expect(api).toMatch(/\*\*`ok` means the provider returned 2xx and nothing more\*\*/)
  })
})

describe('the handover digest distinguishes a refuted fix from an unproven one', () => {
  const panel = readPanel('HandoverPanel.tsx')
  const api = readPanel('api.ts')

  it('renders the miss count on a demoted pattern', () => {
    // This list is ranked by how often the failure RECURS, so the entry most likely to be
    // reached for is at the top — which is exactly where "this has already failed" hides.
    expect(panel).toMatch(/pat\.demoted/)
    // "failed" is a catalog key now; `readPanel` folds it in as ⟦failed⟧ after the key,
    // and the miss count follows the call.
    expect(panel).toMatch(/⟦failed⟧\)\}\s*\{pat\.misses\}/)
  })

  it('says not to hand a refuted fix over as the answer', () => {
    expect(panel).toMatch(/Do not\s*\n?\s*hand it over as the answer/)
  })

  it('declares both fields on the digest type', () => {
    expect(api).toMatch(/misses: number/)
    expect(api).toMatch(/demoted: boolean/)
  })

  it('leaves digest.text alone', () => {
    // The backend renders the pasted form, including its own "failed N×" mark, so the
    // paste and the screen cannot word one shift differently.
    expect(panel).not.toMatch(/digest\.text\s*[+.]\s*(replace|concat)/)
  })
})

/**
 * The heartbeat-pacing knobs. `PUT /settings` accepted all three and no read path returned
 * any of them, so the values governing every install were invisible — the app's signature
 * failure (machinery that looks deliberate while telling you nothing) in the settings layer.
 */
describe('the sweep windows are visible to an operator', () => {
  const settings = readPanel('SettingsPanel.tsx')
  const api = readPanel('api.ts')

  it('declares the whole shape on the rotation response', () => {
    expect(api).toMatch(/interface SweepWindows/)
    expect(api).toMatch(/max_claims_per_cycle: number/)
    expect(api).toMatch(/stale_after_secs: number/)
    expect(api).toMatch(/needs_human_stale_after_secs: number/)
    expect(api).toMatch(/needs_human_derived: boolean/)
  })

  it('renders every one of the three knobs, not just the type', () => {
    // The failure this whole run is about: declaring a field and never painting it.
    expect(settings).toMatch(/HeartbeatCard/)
    expect(settings).toMatch(/stale_after_secs/)
    expect(settings).toMatch(/needs_human_stale_after_secs/)
    expect(settings).toMatch(/max_claims_per_cycle/)
  })

  it('says nothing rather than inventing defaults when the gateway did not report them', async () => {
    // An older gateway sends no `sweep`. Printing 2 h at an instance that might be running
    // 30 m would be an overstated claim, which this repo treats as a defect in itself.
    expect(settings).toMatch(/Not reported by this gateway/)
  })

  it('shows seconds in a unit a human tunes in', async () => {
    // Routed through `fmtUnit`, so the unit is translated and the digits localized. The
    // rendering is the CLDR narrow form for the active language — `2h` under `en`, not a
    // hand-glued `2 h`. Asserted against `en` because that is the test locale.
    const { humanizeSecs } = await import('../apps/ops-mission-control/SettingsPanel')
    expect(humanizeSecs(7200)).toBe('2h')
    // 12h — the derived needs_human default. Misread as minutes this is the difference
    // between half a day and a coffee break, which is why the card never prints raw secs.
    expect(humanizeSecs(43200)).toBe('12h')
    expect(humanizeSecs(600)).toBe('10m')
    expect(humanizeSecs(45)).toBe('45s')
  })

  it('renders an unreported window as a dash rather than as zero', async () => {
    // 0 must never read as "released immediately".
    const { humanizeSecs } = await import('../apps/ops-mission-control/SettingsPanel')
    expect(humanizeSecs(0)).toBe('—')
    expect(humanizeSecs(Number.NaN)).toBe('—')
  })

  it('distinguishes a derived window from a pinned one', () => {
    // A derived window MOVES when the working threshold changes and a pinned one does not,
    // so telling an operator which they have is the difference between the two settings.
    expect(settings).toMatch(/needs_human_derived/)
    expect(settings).toMatch(/derived from the window above/)
    expect(settings).toMatch(/Pinned at/)
  })

  it('never sends a value the backend would reject with a 400', () => {
    // The write path refuses non-integer and <= 0. Disabling the button beats making the
    // operator interpret a 400 they cannot attribute to anything.
    expect(settings).toMatch(/Number\.isInteger\(mins\) \|\| mins <= 0/)
  })
})

/**
 * UI/BACKEND PARITY, audited as its own property.
 *
 * The owner's standing instruction is that a change is not done until its operator-visible
 * effect is visible in the UI, and the pass before this one failed it measurably: of 10 new
 * response fields, 7 reached no panel and 3 were not even declared in `api.ts`. Types are not
 * parity — a field plumbed into `api.ts` and read by nothing is this app's signature failure
 * (machinery that looks deliberate while doing nothing) relocated one layer up.
 *
 * These tests pin the fields whose ONLY render is the one added for them, so deleting that
 * render is a test failure rather than a silent regression to invisible.
 */
describe('every operator-visible response field reaches a panel', () => {
  const signals = readPanel('SignalsPanel.tsx')
  const board = readPanel('OpsMissionControlPage.tsx')
  const settings = readPanel('SettingsPanel.tsx')
  const api = readPanel('api.ts')

  it('renders the inbound webhook queue depth, which a poll destroys', () => {
    // `WebhookSignalSource.poll` calls `drain`, so polling to look at the spool is what
    // empties it: between a sender's 200 and the next heartbeat this number is the only
    // evidence a delivery landed. `/state` reported it from the day the adapter shipped and
    // nothing read it, which made the one push-based adapter the hardest to confirm.
    expect(signals).toMatch(/webhook_queue/)
    // Catalog, not source: this became a plural pair for the same reason as above. The
    // singular says "it is not on the board yet", the plural "they are" — which is exactly
    // what a "(s)" could not express.
    const signalsCopy = EN_CATALOG.apps.opsMissionControl.signalsPanel
    expect(signalsCopy.inbound_webhook_queued_one).toMatch(/waiting for the next cycle/)
    expect(signalsCopy.inbound_webhook_queued_other).toMatch(/waiting for the next cycle/)
  })

  it('says the spool is full rather than showing a healthy-looking backlog', () => {
    // The spool is a bounded deque: at the cap the OLDEST delivery is discarded to make
    // room, silently. A bare "200 queued" reads as a backlog that will drain.
    expect(signals).toMatch(/WEBHOOK_QUEUE_LIMIT/)
    expect(api).toMatch(/export const WEBHOOK_QUEUE_LIMIT/)
    expect(signals).toMatch(/being discarded as new ones arrive/)
  })

  it('reports what a hand-claim found, on the only route that can say it', () => {
    // `exact_match_ids` and `fast_path` are properties of a LOOKUP, not of the stored
    // incident, so `/state` and `/incident` do not carry them and the Board is right to
    // refuse to re-derive them. This claim response is the only place either is observable,
    // which is exactly why both were declared and read by nothing.
    expect(signals).toMatch(/claimMutation\.data\.exact_match_ids/)
    expect(signals).toMatch(/claimMutation\.data\.fast_path/)
    expect(signals).toMatch(/exact match/)
  })

  it('distinguishes an exact provider match from a shape match in the claim result', () => {
    // Our fingerprint is a hash over rendered text with bare digits stripped, so a 4xx and
    // a 5xx alarm on one resource hash identically — a shape match can hand a responder a
    // fix learned from a different failure, and the operator has to be told which they got.
    expect(signals).toMatch(/merges alarms differing only in a number/)
  })

  it('reports the ledger-wide refuted count, which the top-25 table cannot', () => {
    // Entries sort by `-use_count`, which is exactly the order that pushes a demoted entry
    // DOWN, so "no red rows visible" is not "nothing has been refuted".
    expect(board).toMatch(/state\.ledger\.demoted/)
    expect(board).toMatch(/LEDGER_ROWS_SHOWN/)
    expect(board).toMatch(/been refuted/)
  })

  it('renders the shift end when the rotation source publishes one', () => {
    // A badge saying "on shift: octocat" leaves open how long that holds, which is what a
    // responder needs before starting anything long. Only the schedule-file provider sets
    // `until`, so it must degrade to the old label rather than print an empty clause.
    expect(board).toMatch(/rotation\?\.until/)
    expect(board).toMatch(/function shiftEnd/)
  })

  it('drops an unparseable shift end instead of printing it raw', () => {
    // `until` comes from a hand-edited rotation.yaml. "on shift: octocat until 2026-13-45"
    // is worse than saying nothing, because the rest of the badge is true and the reader
    // cannot tell which part broke.
    expect(board).toMatch(/if \(Number\.isNaN\(at\)\) return ''/)
  })

  it('names the notification bus as unavailable rather than as needing setup', () => {
    // The bus lives on the running gateway, so its absence is not something a toggle, a
    // field or a credential fixes. "needs setup" sends the operator looking for a control
    // that does not exist — advice that cannot work.
    expect(settings).toMatch(/status\.bus_available/)
    expect(settings).toMatch(/unavailable here/)
  })

  it('declares every route the backend registers, including the agent-facing ones', () => {
    // Omission from api.ts is what makes a route invisible to the next reader — and
    // `/ledger/contradictions` was registered, tested in Python, and undeclared here.
    // Declared does not mean rendered; the doc comment must say which and why.
    expect(api).toMatch(/ledgerContradictions:/)
    expect(api).toMatch(/tier_crons: Record<string, string\[\]>/)
    expect(api).toMatch(/export interface Evidence/)
  })

  it('never declares a field the response does not carry', () => {
    // `ClaimedIncident.to_dict` projects four evidence fields; the backend dataclass also
    // has a `url`. Declaring it would be this file asserting a link the payload lacks —
    // the inverse defect, and treated the same way here.
    const evidence = api.slice(api.indexOf('export interface Evidence'))
    expect(evidence.slice(0, evidence.indexOf('}'))).not.toMatch(/^\s*url:/m)
  })
})

/**
 * THE INVERSE DEFECT: a panel asserting more than the backend supports.
 *
 * Treated as a defect in its own right, because an operator acting on an overstated claim
 * wastes time on a fault they do not have — or worse, trusts a guarantee that is not there.
 * Both cases below shipped as confident sentences with no code behind them.
 */
describe('no panel claims more than the backend does', () => {
  const board = readPanel('OpsMissionControlPage.tsx')
  const settings = readPanel('SettingsPanel.tsx')

  it('does not offer turning off strict gating, which no route accepts', () => {
    // `strict_gating` is read through `config_value` but is NOT in
    // `ScheduleFileRotationSource.config_fields`, so `PUT /providers/schedule-file/config`
    // 400s it. There is no toggle in Settings or anywhere else, so the old "or turn off
    // strict gating" was a remedy the UI cannot deliver.
    //
    // Asserted against RENDERED text only, the same treatment the `slack_thread_ts` test
    // above uses: the comments explaining why the remedy was removed have to be free to
    // name it, or the explanation cannot be written down where the next reader will look.
    for (const panel of [board, settings]) {
      expect(rendered(panel)).not.toMatch(/turn off strict gating/)
    }
  })

  it('conditions the never-picks-up-work warning on strict gating being on', () => {
    // With strict gating OFF, `schedule_file._indeterminate` returns `on_shift=True`, so an
    // unnamed instance keeps working normally. Told unconditionally that its work had
    // stopped, an operator would hunt for a fault that does not exist.
    expect(board).toMatch(/!rotation\.roster\.me_on_roster &&\s*\n?\s*rotation\.roster\.strict_gating/)
    expect(settings).toMatch(/!roster\.me_on_roster && roster\.strict_gating/)
  })

  it('says the OTHER half too: gating off means this instance does not defer', () => {
    // Every unnamed instance arms, which is the duplicate-claim shape the shared schedule
    // exists to prevent — and it looks like a correctly-configured team from the board.
    expect(board).toMatch(/does not wait for\s*\n?\s*any teammate/)
    expect(settings).toMatch(/does not defer to whoever is on call/)
  })

  it('states the real ledger-sync cadence, not the one the docstrings aspire to', () => {
    // `POST /ledger/hygiene` is the ONLY caller of the git transport (`grep sync_safely
    // backend/` → routes.py twice, dispatch.py zero) and runs on the daily primary-tier
    // cron. "Pulled before every match and pushed after every lesson" is what
    // `ledger_sync`'s module docstring wants and what `sync_safely`'s docstring wrongly
    // claims. Believing it costs correctness, not just latency: rotation.yaml rides the
    // same repo, so a non-primary instance keeps arming off a schedule it may never fetch.
    //
    // Rendered text only, for the same reason as the strict-gating case: the comment that
    // records WHY the old wording was wrong has to be allowed to quote it.
    expect(rendered(settings)).not.toMatch(/pulled before every match/)
    expect(settings).toMatch(/nightly maintenance pass/)
    expect(settings).toMatch(/an instance with that off never pulls/)
  })
})

/**
 * The propose→approve loop, which the PR headlines and which had no UI surface at all.
 *
 * `propose` mode drafts an action and waits for a person. `proposed_action` was typed in
 * `api.ts` with ZERO readers and the frontend called neither `GET /proposals` nor
 * `POST /incident/proposal/decide` — so the only way to approve was the agent's chat
 * paraphrase or curl. That is the one path the digest binding exists to close: approving a
 * retelling of the terms is not approving the stored terms, and a draft written while the
 * operator was away expired leaving no trace on the board.
 */
describe('a drafted action can be approved from the board', () => {
  const page = readPanel('OpsMissionControlPage.tsx')
  const api = readPanel('api.ts')

  it('exposes the decide route through the API client', () => {
    // Typed `proposed_action` with no route to act on it was the whole defect.
    expect(api).toMatch(/decideProposal/)
    expect(api).toMatch(/'\/incident\/proposal\/decide'/)
    expect(api).toMatch(/'\/proposals'/)
  })

  it('types the proposal rather than leaving it an opaque record', () => {
    // `Record<string, unknown>` cannot tell a caller that `digest` exists, which is how a
    // UI ends up approving without echoing it.
    expect(api).toMatch(/export interface PendingProposal/)
    expect(api).toMatch(/proposed_action: PendingProposal \| null/)
  })

  it('echoes the digest it rendered on an approval', () => {
    // The binding: the approval carries the digest of the terms the operator READ, so the
    // route can refuse if the draft moved underneath. Sending no digest, or re-reading one
    // at click time, both mean "approve whatever is stored now".
    //
    // Anchored to the `approve: true` branch specifically. A bare `/digest: p\.digest/`
    // passes on the REJECT call site too, so it stayed green when the approve branch was
    // changed to send `digest: ''` — verified by making exactly that edit.
    expect(page).toMatch(/approve: true,\s*\n\s*digest: p\.digest,/)
    // The page only forwards a digest when APPROVING — a rejection authorizes nothing, so
    // binding it to terms would be meaningless.
    expect(page).toMatch(/approve \? digest : undefined/)
    // …and the client omits the key entirely rather than sending an empty one, which the
    // route would otherwise have to distinguish from "read the terms and they were blank".
    expect(api).toMatch(/\.\.\.\(digest \? \{ digest \} : \{\}\)/)
  })

  it('renders the outbound text verbatim rather than a summary', () => {
    // `note` IS the text that would go out. A re-worded rendering would make the operator
    // approve something they never saw.
    expect(page).toMatch(/\{p\.note\}/)
  })

  it('shows the card only while the proposal is still pending', () => {
    // A decided or expired draft is history; the row's status text already carries it, and
    // an Approve button on an expired proposal invites a click that cannot work.
    expect(page).toMatch(/proposed_action\.state === 'pending'/)
  })

  it('surfaces a refusal instead of silently doing nothing', () => {
    // A 403 is the autonomy gate and a 409 is a digest mismatch. Both must reach the
    // operator: a click that quietly changes nothing reads as success.
    expect(page).toMatch(/decideMutation\.isError/)
    expect(page).toMatch(/the_decision_was_refused|decideMutation\.data\.error/)
  })

  it('names the sink and window in one phrase, not as fragments', () => {
    // Two keys made the window half a bare `for {{duration}}` — unplaceable in a language
    // that orders "through X for Y" differently.
    expect(page).toMatch(/through_sink_for_duration/)
  })
})

/**
 * Provider-supplied URLs must never become executable links.
 *
 * `Signal.url` comes from a provider — including the HMAC-signed webhook, which accepts
 * anything able to POST JSON. It was rendered straight into `href={s.url}` on four surfaces,
 * so a signal carrying `javascript:alert(document.cookie)` produced a live script link in the
 * DASHBOARD'S OWN ORIGIN, on an element labelled "Provider" that an operator is invited to
 * click. Found in review.
 *
 * `lib/safeUrl.safeHttpUrl` already existed for exactly this and sibling apps (issue-radar,
 * ArtifactDeployPage) already routed through it — this app was the outlier, which is the part
 * worth guarding: the fix is one helper call, and the failure mode is that a NEW link forgets
 * it.
 */
describe('a provider-supplied URL cannot become an executable link', () => {
  const panels = ['SignalsPanel.tsx', 'OpsMissionControlPage.tsx'] as const

  it('routes every signal href through safeHttpUrl', () => {
    const offenders: string[] = []
    for (const name of panels) {
      const source = readPanel(name)
      source.split('\n').forEach((line, i) => {
        if (line.trim().startsWith('//') || line.trim().startsWith('*')) return
        // A raw `href={...url}` with no `safeHttpUrl` on the same line is the defect.
        if (/href=\{[^}]*\burl\b/.test(line) && !line.includes('safeHttpUrl')) {
          offenders.push(`${name}:${i + 1} ${line.trim()}`)
        }
      })
    }
    expect(offenders, `provider URLs must pass safeHttpUrl():\n  ${offenders.join('\n  ')}`).toEqual(
      [],
    )
  })

  it('gates the link on the VALIDATED url, not on the raw one', () => {
    // `{s.url ? <a href={safeHttpUrl(s.url)!}>` would render an anchor with href="null" for a
    // rejected URL — visibly broken rather than simply absent. The condition and the href must
    // both read the validated value.
    for (const name of panels) {
      const source = readPanel(name)
      const rawConditions = source
        .split('\n')
        .filter((l) => /\{(s|inc)\.(signal\.)?url \? \(/.test(l) && !l.includes('safeHttpUrl'))
        .map((l) => l.trim())
      expect(rawConditions, `${name}: gate on safeHttpUrl(...), not the raw url`).toEqual([])
    }
  })

  it('the helper it depends on really does reject the dangerous schemes', async () => {
    // Guards the premise: if `safeHttpUrl` ever loosened, the tests above would still pass
    // while the app became vulnerable again.
    const { safeHttpUrl } = await import('../lib/safeUrl')
    for (const hostile of [
      'javascript:alert(document.cookie)',
      'data:text/html,<script>alert(1)</script>',
      'vbscript:msgbox(1)',
      'https://token@evil.example/x', // userinfo — smuggles a credential to the host
    ]) {
      expect(safeHttpUrl(hostile), `${hostile} must be rejected`).toBeNull()
    }
    expect(safeHttpUrl('https://github.com/org/repo/issues/1')).toBeTruthy()
  })
})

/**
 * The `act` autonomy tier — the app's headline capability — had no authoring path at all.
 *
 * Exactly the same class as the team-repo gap above, and worse in consequence:
 * `policy_store.set_rules` had ZERO callers, `/rotation` returned only a rule COUNT, and the
 * panel rendered "No rules defined yet." with nothing to click. The shipped manual told
 * operators to edit `data/config.json` — which the keystone store ignores once the policy file
 * exists. So every act-mode adopter silently got Propose behavior with no error anywhere.
 */
describe('Settings can author the act-rules that authorize a write', () => {
  const panel = readPanel('SettingsPanel.tsx')

  it('sends autonomy_rules through the settings mutation', () => {
    expect(panel).toContain('autonomy_rules')
  })

  it('renders the rules themselves, not just the count', () => {
    // A count cannot be reviewed or revoked. `rules_detail` is the list.
    expect(panel).toContain('rules_detail')
  })

  it('offers a revoke affordance, so authority can be taken back', () => {
    expect(panel).toMatch(/rules\.filter\(/)
  })

  it('requires a resource pattern before the grant button enables', () => {
    // The backend refuses a blanket act-grant with a 400; the form must refuse it first so
    // the operator sees the constraint instead of a server error.
    expect(panel).toMatch(/glob\.trim\(\) !== ''/)
  })

  it('only offers configured signal sources', () => {
    // A rule naming an unconfigured provider can never match, so offering it would invite
    // the operator to author something inert.
    expect(panel).toMatch(/p\.configured && p\.roles\.includes\('signal'\)/)
  })

  it('no longer dead-ends on the un-actionable empty state', () => {
    // The old copy named the gap without offering any way to close it.
    expect(expandI18n(panel)).not.toMatch(/No rules defined yet\./)
  })
})
