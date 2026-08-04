import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import {
  BUCKETS,
  BUCKET_MEANING,
  LATENT_SIGNATURES,
  LEDGER_COMMENT,
  addedFindings,
  bucketOf,
  decide,
  fmtDelta,
  identityIsExact,
  tally,
  totals,
} from '../../scripts/lib/render-verdict.mjs'

/**
 * The render gate's verdict.
 *
 * The property under test is the one the gate got wrong: a DIFF fails, a TOTAL is
 * reported. It is tested here as a pure function because the alternative — reaching
 * this branch by building two bundles and driving a browser — is a ten-minute round
 * trip, which is why the original defect shipped untested in the first place.
 */

const SURFACES = ['projects', 'chat', 'apps']
const at = (surface, bucket, n) => ({
  ...Object.fromEntries(SURFACES.map(s => [s, { text: 0, layout: 0, latent: 0 }])),
  [surface]: { text: 0, layout: 0, latent: 0, [bucket]: n },
})

describe('bucketOf', () => {
  it('routes the min-w-0 signature to latent, other layout findings to layout', () => {
    expect(bucketOf({ kind: 'layout', signature: 'ellipsis-with-flex-parent' })).toBe('latent')
    expect(bucketOf({ kind: 'layout', signature: 'clipped-without-title' })).toBe('layout')
    expect(bucketOf({ kind: 'layout', signature: 'unbreakable-token' })).toBe('layout')
  })

  it('routes every non-layout finding to text', () => {
    expect(bucketOf({ kind: 'text', signature: 'untranslated-text' })).toBe('text')
    expect(bucketOf({ kind: 'text', signature: 'untranslated-attribute' })).toBe('text')
    expect(bucketOf({ kind: 'fragment', signature: 'multi-unit' })).toBe('text')
  })
})

describe('tally', () => {
  it('counts per surface and splits DNT out of the buckets', () => {
    const { counts, dnt } = tally([
      { surface: 'chat', locale: 'en-XA', finding: { kind: 'text', signature: 'untranslated-text' } },
      { surface: 'chat', locale: 'de', finding: { kind: 'layout', signature: 'clipped-without-title' } },
      { surface: 'projects', locale: 'de', finding: { kind: 'layout', signature: 'ellipsis-with-flex-parent' } },
      { surface: 'chat', locale: 'zh-CN', finding: { kind: 'dnt', detail: 'KiroCrew -> 基罗组' } },
    ], SURFACES)

    expect(counts.chat).toEqual({ text: 1, layout: 1, latent: 0 })
    expect(counts.projects).toEqual({ text: 0, layout: 0, latent: 1 })
    expect(counts.apps).toEqual({ text: 0, layout: 0, latent: 0 })
    expect(dnt).toHaveLength(1)
    expect(totals(counts)).toEqual({ text: 1, layout: 1, latent: 1 })
  })

  it('seeds every known surface so an untouched one reads 0 rather than undefined', () => {
    const { counts } = tally([], SURFACES)
    for (const id of SURFACES) expect(counts[id]).toEqual({ text: 0, layout: 0, latent: 0 })
  })
})

describe('decide — a total is a report, never a failure', () => {
  const ledger = { surfaces: { projects: { text: 0, layout: 0, latent: 16 } } }

  it('does NOT fail when the count is above the debt record but the diff is clean', () => {
    // The exact shape that turned main red: #1107 recorded 16 measured on its own
    // base, #985 independently added the 8 (one site x 4 locales x 2 viewports), and
    // after both merged every branch inherited a failure its own diff did not cause.
    const counts = at('projects', 'latent', 24)
    const verdict = decide({
      counts, baseCounts: counts, ledger, surfaceIds: SURFACES, dnt: [],
    })

    expect(verdict.failed).toBe(false)
    expect(verdict.grew).toEqual([])
    expect(verdict.above).toEqual([
      { surface: 'projects', bucket: 'latent', was: 16, now: 24 },
    ])
  })

  it('fails when the diff grows a surface, even while below the debt record', () => {
    const verdict = decide({
      counts: at('projects', 'latent', 12),
      baseCounts: at('projects', 'latent', 4),
      ledger,
      surfaceIds: SURFACES,
    })

    expect(verdict.failed).toBe(true)
    expect(verdict.grew).toEqual([
      { surface: 'projects', bucket: 'latent', was: 4, now: 12 },
    ])
    // Below 16, so the debt record has nothing to say — and could not have caught it.
    expect(verdict.above).toEqual([])
    expect(verdict.below).toEqual([
      { surface: 'projects', bucket: 'latent', was: 16, now: 12 },
    ])
  })

  it('reports a fix without requiring the debt record to be rewritten', () => {
    const verdict = decide({
      counts: at('projects', 'latent', 8),
      baseCounts: at('projects', 'latent', 16),
      ledger,
      surfaceIds: SURFACES,
    })

    expect(verdict.failed).toBe(false)
    expect(verdict.shrank).toEqual([
      { surface: 'projects', bucket: 'latent', was: 16, now: 8 },
    ])
  })

  it('fails a DNT violation with no counts involved at all', () => {
    const verdict = decide({
      counts: at('chat', 'text', 0),
      baseCounts: at('chat', 'text', 0),
      ledger: null,
      surfaceIds: SURFACES,
      dnt: [{ surface: 'chat', locale: 'zh-CN', finding: { kind: 'dnt', detail: 'KiroCrew' } }],
    })

    expect(verdict.failed).toBe(true)
  })

  it('grades every bucket, not just the one the caller happened to break', () => {
    const counts = {
      projects: { text: 3, layout: 0, latent: 0 },
      chat: { text: 0, layout: 2, latent: 0 },
      apps: { text: 0, layout: 0, latent: 5 },
    }
    const base = {
      projects: { text: 1, layout: 0, latent: 0 },
      chat: { text: 0, layout: 1, latent: 0 },
      apps: { text: 0, layout: 0, latent: 5 },
    }
    const verdict = decide({ counts, baseCounts: base, surfaceIds: SURFACES })

    expect(verdict.failed).toBe(true)
    expect(verdict.grew.map(fmtDelta)).toEqual([
      'projects.text: 1 -> 3 (+2)',
      'chat.layout: 1 -> 2 (+1)',
    ])
  })
})

describe('decide — with no base, whether the total decides depends on WHY', () => {
  const noBase = (extra = {}) => decide({
    counts: at('projects', 'latent', 999),
    baseCounts: null,
    ledger: { surfaces: { projects: { latent: 0 } } },
    surfaceIds: SURFACES,
    ...extra,
  })

  it('reports without failing when the caller ASKED not to diff', () => {
    // `--no-vs-base`, `--surface`, `--update`: all documented as report-only, and a
    // flag that starts failing is a flag whose contract changed under its users.
    const verdict = noBase()

    expect(verdict.enforced).toBe(false)
    expect(verdict.failed).toBe(false)
    expect(verdict.above).toHaveLength(1)
  })

  it('fails on the total when no base was AVAILABLE, rather than checking nothing', () => {
    // The whole point of demoting the total is that a diff is the better question.
    // Where there is no diff at all, the worse question still beats exiting 0 having
    // asked none — that is how a gate stops guarding without anyone noticing.
    const verdict = noBase({ totalIsFallback: true })

    expect(verdict.enforced).toBe(false)
    expect(verdict.failed).toBe(true)
    expect(verdict.above.map(fmtDelta)).toEqual(['projects.latent: 0 -> 999 (+999)'])
  })

  it('still passes a fallback run that is at or below the record', () => {
    expect(decide({
      counts: at('projects', 'latent', 0),
      baseCounts: null,
      ledger: { surfaces: { projects: { latent: 16 } } },
      surfaceIds: SURFACES,
      totalIsFallback: true,
    }).failed).toBe(false)
  })

  it('never lets the fallback fire once a diff exists, however high the total', () => {
    // The #1107/#985 shape: way above the record, but this tree added nothing.
    const counts = at('projects', 'latent', 999)
    expect(decide({
      counts,
      baseCounts: counts,
      ledger: { surfaces: { projects: { latent: 0 } } },
      surfaceIds: SURFACES,
      totalIsFallback: true,
    }).failed).toBe(false)
  })

  it('is enforced as soon as a base is supplied', () => {
    const counts = at('projects', 'latent', 4)
    expect(decide({ counts, baseCounts: counts, surfaceIds: SURFACES }).enforced).toBe(true)
  })
})

describe('the debt record describes the rule the code follows', () => {
  const ledger = JSON.parse(
    readFileSync(resolve(__dirname, '../i18n/render-baseline.json'), 'utf-8'),
  )

  it('has the _comment `--update` writes, so a regen is a no-op', () => {
    // These drifted apart once already: the writer's wording and the checked-in
    // wording diverged, and the next `--update` churned a paragraph nobody edited.
    expect(ledger._comment).toBe(LEDGER_COMMENT)
  })

  it('tells the reader it is not the gate, and names the one case where it is', () => {
    expect(ledger._comment).toContain('DEBT RECORD, not the gate')
    expect(ledger._comment).toContain('NO base commit to diff')
  })
})

describe('the base render is forced by anything that can move a number', () => {
  const gate = readFileSync(
    resolve(__dirname, '../../scripts/check-i18n-render.mjs'),
    'utf-8',
  )
  // Extracted rather than duplicated, so the test cannot drift from the real filter.
  // Guarded rather than `!`-asserted: a non-null assertion here made an unrelated
  // reformat of that line kill the WHOLE file at collection time with
  // `Cannot read properties of null`, which tells a reader nothing about what broke.
  const found = gate.match(/f => \/\^website\\\/\((.+?)\)\/\.test\(f\)/)
  const renderable = found
    ? new RegExp(found[0].replace('f => /', '').replace(/\/\.test\(f\)$/, ''))
    : null

  it('exposes a renderable filter this test can read', () => {
    expect(
      renderable,
      'could not find the `renderable` filter in check-i18n-render.mjs — if it was '
      + 'renamed or reformatted, update the pattern in this test',
    ).not.toBeNull()
  })

  // Skipping the base render is now equivalent to skipping the gate: with the total
  // demoted, an exempt path gets no check at all where before it still had to clear
  // the ledger. So the filter has to cover the measurement, not just the markup.
  it.each([
    ['website/src/pages/ProjectsPage.tsx', 'app source'],
    ['website/src/i18n/locales/zh-CN.json', 'a catalog'],
    ['website/scripts/lib/render-scan.mjs', 'the scanner itself'],
    ['website/scripts/lib/i18n-surfaces.mjs', 'the surface list — new surfaces start at 0'],
    ['website/scripts/lib/stub-dashboard-api.mjs', 'fixture text that gets rendered'],
    ['website/public/logo.svg', 'an asset served into the page'],
    ['website/index.html', 'the shell'],
    ['website/vite.config.ts', 'the build'],
    ['website/tsconfig.json', 'the build'],
    ['website/package.json', 'dependencies'],
    ['website/package-lock.json', 'resolved dependencies'],
  ])('forces a base render for %s (%s)', file => {
    expect(renderable!.test(file)).toBe(true)
  })

  it.each([
    'AGENTS.md',
    'website/AGENTS.md',
    '.github/workflows/ci.yml',
    'src/kiro_crew/server.py',
  ])('does not force one for %s', file => {
    expect(renderable!.test(file)).toBe(false)
  })
})

describe('decide — a partial run cannot claim an improvement', () => {
  const ledger = {
    surfaces: {
      projects: { text: 0, layout: 0, latent: 16 },
      chat: { text: 40, layout: 0, latent: 0 },
    },
  }

  it('suppresses decreases it under-counted, and ignores other surfaces', () => {
    const verdict = decide({
      counts: at('projects', 'latent', 8),
      ledger,
      surfaceIds: SURFACES,
      partial: true,
      onlySurface: 'projects',
    })

    // 8 < 16 only because --surface rendered one surface: not a real decrease.
    expect(verdict.below).toEqual([])
    // chat's ledger of 40 vs a measured 0 is not evidence of anything either.
    expect(verdict.above).toEqual([])
  })

  it('still reports an increase, which a partial run cannot fake', () => {
    const verdict = decide({
      counts: at('projects', 'latent', 24),
      ledger,
      surfaceIds: SURFACES,
      partial: true,
      onlySurface: 'projects',
    })

    expect(verdict.above).toEqual([
      { surface: 'projects', bucket: 'latent', was: 16, now: 24 },
    ])
    expect(verdict.failed).toBe(false)
  })
})

describe('CI supplies a base commit on both paths', () => {
  // The verdict above only fails on a diff, so a context with no base enforces
  // nothing. That makes the workflow wiring load-bearing: if a push to main stopped
  // passing `github.event.before`, merges would silently go unchecked and the unit
  // tests above would still pass.
  const ci = readFileSync(resolve(process.cwd(), '../.github/workflows/ci.yml'), 'utf-8')
  const wirings = ci.match(/^\s*I18N_BASE_REF: \$\{\{.*$/gm) || []

  it('wires every i18n gate step', () => {
    expect(wirings).toHaveLength(3)
  })

  it('diffs a PR against the commit its merge ref was built on, not the branch tip', () => {
    // `origin/<base.ref>` is a MOVING TARGET resolved at step time, while the checked-out
    // tree is a merge-ref snapshot from job start. Anything that lands on `main` in
    // between shows up only on the base side and is charged to every PR running in that
    // window — observed on PR #1257, whose run started 36s before #1223 merged and was
    // billed 58 render findings for `WelcomeView` translations its head did not have.
    // `base.sha` is the commit the merge ref was computed against, so the two sides are
    // consistent by construction.
    for (const line of wirings) {
      expect(line).toContain("github.event_name == 'pull_request'")
      expect(line).toContain('github.event.pull_request.base.sha')
      expect(line, 'the base branch TIP must not be used — it moves under the run')
        .not.toContain('base.ref')
      // A push to `main` has no PR base, so it diffs the commit it replaced.
      expect(line).toContain('github.event.before')
    }
  })

  it('routes each step through the shared resolver', () => {
    expect(ci.match(/resolve-i18n-base\.sh/g) || []).toHaveLength(3)
  })
})

describe('addedFindings — name the findings, not just the number that moved', () => {
  const rec = (surface: string, signature: string, source: string[], text = 'Chat') => ({
    surface,
    locale: 'en-XA',
    finding: { kind: signature.startsWith('ellipsis') ? 'layout' : 'latin-leak', signature, source, path: 'div>span', text },
  })
  const grewLatent = [{ surface: 'chat', bucket: 'latent', was: 0, now: 1 }]

  it('returns the finding present at head and absent at base', () => {
    const base = [rec('chat', 'ellipsis-with-flex-parent', ['a.tsx:1'])]
    const head = [...base, rec('chat', 'ellipsis-with-flex-parent', ['b.tsx:9'])]

    const added = addedFindings(head, base, grewLatent)
    expect(added).toHaveLength(1)
    expect(added[0].finding.source).toEqual(['b.tsx:9'])
  })

  it('counts duplicates as a multiset — 16 from one .map() is 16 findings', () => {
    const many = (n: number) => Array.from({ length: n }, () => rec('chat', 'ellipsis-with-flex-parent', ['row.tsx:12']))
    expect(addedFindings(many(16), many(4), grewLatent)).toHaveLength(12)
  })

  it('ignores surfaces and buckets that did not grow', () => {
    // `settings-chat` moved internally; only `chat.latent` is charged to this branch.
    const head = [
      rec('chat', 'ellipsis-with-flex-parent', ['b.tsx:9']),
      rec('settings-chat', 'untranslated-text', ['x.tsx:3']),
    ]
    const added = addedFindings(head, [], grewLatent)
    expect(added.map(a => a.surface)).toEqual(['chat'])
  })

  it('returns nothing when no surface grew, however many findings exist', () => {
    expect(addedFindings([rec('chat', 'untranslated-text', ['a.tsx:1'])], [], [])).toEqual([])
  })

  it('keys identity on SOURCE, so a DOM reshuffle is not a new finding', () => {
    // Same source line, different positional path: inserting a wrapper renumbers
    // siblings, and a path-keyed diff would report every finding below it as new.
    const base = [rec('chat', 'ellipsis-with-flex-parent', ['row.tsx:12'])]
    const head = [{ ...base[0], finding: { ...base[0].finding, path: 'div:nth(3)>div>span' } }]
    expect(addedFindings(head, base, grewLatent)).toEqual([])
  })

  it('falls back to the DOM path when no source is available, and says it is inexact', () => {
    const head = [rec('chat', 'ellipsis-with-flex-parent', [])]
    expect(addedFindings(head, [], grewLatent)).toHaveLength(1)
    expect(identityIsExact(head)).toBe(false)
    expect(identityIsExact([rec('chat', 'ellipsis-with-flex-parent', ['a.tsx:1'])])).toBe(true)
  })
})

describe('bucket metadata the log prints', () => {
  it('classifies latent by set membership, not by one hard-coded name', () => {
    expect(LATENT_SIGNATURES.has('ellipsis-with-flex-parent')).toBe(true)
    // Behaviour must be unchanged today: exactly one member, so no count moves.
    expect([...LATENT_SIGNATURES]).toEqual(['ellipsis-with-flex-parent'])
    expect(bucketOf({ kind: 'layout', signature: 'unbreakable-token' })).toBe('layout')
  })

  it('explains every bucket, so the log can reconcile signatures with counts', () => {
    for (const b of BUCKETS) expect(BUCKET_MEANING[b]).toBeTruthy()
  })
})

describe('INVARIANT — with a diff in hand, a total can never fail the run', () => {
  /**
   * The regression this whole change exists to prevent. #1107 recorded
   * `projects.latent: 16` on its own base, #985 independently added one
   * `min-w-0`-less `truncate`, and every unrelated PR inherited a red gate for a
   * number no diff produced. If a total can fail a run that HAS a diff, that is back.
   *
   * Exhaustive over the flags rather than spot-checked, because the guard is a
   * three-term conjunction and a future edit could drop any one of them.
   */
  const surfaceIds = ['chat', 'projects']
  const wayAbove = { chat: { text: 9999, layout: 9999, latent: 9999 }, projects: { text: 9999, layout: 9999, latent: 9999 } }
  const tightLedger = { surfaces: { chat: { text: 0, layout: 0, latent: 0 }, projects: { text: 0, layout: 0, latent: 0 } } }

  it.each([
    ['totalIsFallback false', false],
    // The interesting one: even if a caller wrongly passes the fallback flag WITH a
    // base, `!enforced` keeps the total out of the verdict.
    ['totalIsFallback true (flag passed in error)', true],
  ])('passes a clean diff that is massively above the record — %s', (_label, totalIsFallback) => {
    const verdict = decide({
      counts: wayAbove,
      baseCounts: wayAbove, // identical: this branch added nothing
      ledger: tightLedger,
      surfaceIds,
      totalIsFallback,
    })

    expect(verdict.enforced).toBe(true)
    expect(verdict.above.length).toBeGreaterThan(0) // the total IS exceeded
    expect(verdict.failed).toBe(false) // ...and it does not fail the run
  })

  it('treats an empty base render as a diff, not as a missing one', () => {
    // `sweep()` returns `[]` when the base tree produced no findings. `[]` is truthy,
    // so `enforced` must stay true — a zero-finding base is a measurement, not an
    // absence, and degrading it to the fallback would gate on the total instead.
    const { counts } = tally([], surfaceIds)
    expect(decide({
      counts, baseCounts: counts, ledger: tightLedger, surfaceIds, totalIsFallback: true,
    }).enforced).toBe(true)
  })

  it('still fails a diff that grew, and still fails DNT, with or without a base', () => {
    // The invariant must not have been bought by disabling enforcement generally.
    const grewVerdict = decide({
      counts: wayAbove,
      baseCounts: tally([], surfaceIds).counts,
      ledger: tightLedger,
      surfaceIds,
    })
    expect(grewVerdict.failed).toBe(true)

    const dntVerdict = decide({
      counts: tally([], surfaceIds).counts,
      baseCounts: tally([], surfaceIds).counts,
      surfaceIds,
      dnt: [{ surface: 'chat', locale: 'de', finding: { kind: 'dnt', detail: 'x' } }],
    })
    expect(dntVerdict.failed).toBe(true)
  })

  it('is structurally guarded too: the flag is set only where no base was rendered', () => {
    // Belt and braces. `decide()`'s `!enforced` is one barrier; the other is that the
    // gate assigns `totalIsFallback` inside the `else` of `if (scope.run)`, so the flag
    // and a populated `baseAll` are set in mutually exclusive branches. Asserted on the
    // source because that structure — not an expression — is what makes it impossible.
    const gate = readFileSync(resolve(__dirname, '../../scripts/check-i18n-render.mjs'), 'utf-8')
    // Only assignments that can make it TRUTHY matter; `= false` is the declaration
    // and the destructuring default, and both are the safe direction.
    const raising = (gate.match(/totalIsFallback = (?!false)[^\n]*/g) || [])
      .map(s => s.replace(/\s*\}\).*$/, '').trim())
    expect(raising).toEqual(['totalIsFallback = !!scope.fallback'])
    // ...and exactly one branch of resolveBaseScope may raise it.
    expect((gate.match(/fallback: true/g) || []).length).toBe(1)
    expect(gate).toMatch(/if \(!baseRef\) \{[\s\S]*?fallback: true/)
    // The default must be the safe one, so a new early return cannot raise it by omission.
    expect(gate).toMatch(/let totalIsFallback = false/)
  })
})

describe('a surface this branch added is measured against zero, not against a fallback page', () => {
  // Found by the server GPT reviewer, and it invalidated an assumption an earlier test
  // in this file had quietly encoded. Adding a surface means the BASE bundle has no
  // route for its URL, so the base sweep resolves that path through the SPA's router and
  // reports whatever page it lands on. If that fallback is busier than the new surface,
  // the new surface's real defects read as an IMPROVEMENT and `[vs-base]` passes.
  const SURFS = ['chat', 'newsurf']

  it('charges its findings as growth even when the base reported MORE for that id', () => {
    const verdict = decide({
      counts: { chat: { text: 0, layout: 0, latent: 0 }, newsurf: { text: 12, layout: 0, latent: 0 } },
      // The base sweep hit a fallback page with 92 findings under `newsurf`'s id.
      baseCounts: { chat: { text: 0, layout: 0, latent: 0 }, newsurf: { text: 92, layout: 0, latent: 0 } },
      surfaceIds: SURFS,
      newSurfaces: new Set(['newsurf']),
    })

    expect(verdict.failed).toBe(true)
    expect(verdict.grew.map(fmtDelta)).toEqual(['newsurf.text: 0 -> 12 (+12)'])
    // And it must NOT be reported as a fix — the branch did not improve anything.
    expect(verdict.shrank).toHaveLength(0)
  })

  it('without the flag, that same shape passes — which is the bug', () => {
    const verdict = decide({
      counts: { chat: { text: 0, layout: 0, latent: 0 }, newsurf: { text: 12, layout: 0, latent: 0 } },
      baseCounts: { chat: { text: 0, layout: 0, latent: 0 }, newsurf: { text: 92, layout: 0, latent: 0 } },
      surfaceIds: SURFS,
    })
    expect(verdict.failed).toBe(false)
    expect(verdict.shrank.map(fmtDelta)).toEqual(['newsurf.text: 92 -> 12'])
  })

  it('leaves pre-existing surfaces measured against their real base', () => {
    const verdict = decide({
      counts: { chat: { text: 4, layout: 0, latent: 0 }, newsurf: { text: 0, layout: 0, latent: 0 } },
      baseCounts: { chat: { text: 9, layout: 0, latent: 0 }, newsurf: { text: 0, layout: 0, latent: 0 } },
      surfaceIds: SURFS,
      newSurfaces: new Set(['newsurf']),
    })
    expect(verdict.failed).toBe(false)
    expect(verdict.shrank.map(fmtDelta)).toEqual(['chat.text: 9 -> 4'])
  })

  // The other half of the same distinction, and the one that decides whether the
  // gate's aperture can ever be widened. A surface REGISTERED for the first time
  // whose ROUTE already existed on the base is measurable there, and its findings
  // are pre-existing debt the registration merely revealed — not something the
  // branch added. `newSurfaces` must therefore mean "the base had no route for this
  // URL" (proven in `check-i18n-render.mjs` by the base sweep being redirected
  // away), never "this id is absent from the base registry".
  it('does NOT charge a newly registered surface whose route already existed', () => {
    const verdict = decide({
      counts: { chat: { text: 0, layout: 0, latent: 0 }, newsurf: { text: 140, layout: 0, latent: 0 } },
      // The base rendered the same route and found the same 140 — the surface was
      // simply never in the registry, so nobody had looked.
      baseCounts: { chat: { text: 0, layout: 0, latent: 0 }, newsurf: { text: 140, layout: 0, latent: 0 } },
      surfaceIds: SURFS,
      // Not in `newSurfaces`: the base sweep landed on the requested URL.
      newSurfaces: new Set(),
    })

    expect(verdict.failed).toBe(false)
    expect(verdict.grew).toHaveLength(0)
    expect(verdict.shrank).toHaveLength(0)
  })

  it('still catches a real regression on a newly registered existing route', () => {
    // Registering the surface must not buy an exemption either: once it is compared
    // against a real base, an increase there fails like anywhere else.
    const verdict = decide({
      counts: { chat: { text: 0, layout: 0, latent: 0 }, newsurf: { text: 146, layout: 0, latent: 0 } },
      baseCounts: { chat: { text: 0, layout: 0, latent: 0 }, newsurf: { text: 140, layout: 0, latent: 0 } },
      surfaceIds: SURFS,
      newSurfaces: new Set(),
    })

    expect(verdict.failed).toBe(true)
    expect(verdict.grew.map(fmtDelta)).toEqual(['newsurf.text: 140 -> 146 (+6)'])
  })
})

describe('BUCKETS', () => {  it('is the ledger key order the debt record is written with', () => {
    expect(BUCKETS).toEqual(['text', 'layout', 'latent'])
  })
})
