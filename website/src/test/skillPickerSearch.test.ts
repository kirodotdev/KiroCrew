import { describe, it, expect } from 'vitest'
import { rankSkills, leafOf, dollarToken, loaderKey } from '../lib/skillPickerSearch'

// The five skills from #42694: nested group folders carry the short leaf, the
// SKILL.md `name:` carries the full name. The old picker matched the leaf only.
const NESTED = [
  { key: 'finance/reports/quarterly-close', name: 'finance-reports-quarterly-close' },
  { key: 'ops/oncall/handoff-summary', name: 'ops-oncall-handoff-summary' },
  { key: 'data/warehouse/query-lineage', name: 'data-warehouse-query-lineage' },
  { key: 'data/warehouse/freshness-check', name: 'data-warehouse-freshness-check' },
  { key: 'support/triage/escalation', name: 'support-triage-escalation' },
]

const keys = (rows: { key: string }[]) => rows.map(r => r.key)

describe('rankSkills — #42694 examples', () => {
  it('leafOf is the last path segment', () => {
    expect(leafOf('ops/oncall/handoff-summary')).toBe('handoff-summary')
    expect(leafOf('grill')).toBe('grill')
  })

  it('$ops finds the skill under ops/ by its name prefix and path segment', () => {
    expect(keys(rankSkills(NESTED, 'ops'))).toEqual(['ops/oncall/handoff-summary'])
  })

  it('$oncall finds the skill whose name and path contain it', () => {
    expect(keys(rankSkills(NESTED, 'oncall'))).toEqual(['ops/oncall/handoff-summary'])
  })

  it('$data-warehouse finds both skills whose names start with it, in catalog order', () => {
    expect(keys(rankSkills(NESTED, 'data-warehouse'))).toEqual([
      'data/warehouse/query-lineage',
      'data/warehouse/freshness-check',
    ])
  })

  it('$escalation still finds the skill by its last folder', () => {
    expect(keys(rankSkills(NESTED, 'escalation'))).toEqual(['support/triage/escalation'])
  })

  it('matches the full key as one string ($ops/oncall)', () => {
    expect(keys(rankSkills(NESTED, 'ops/oncall'))).toEqual(['ops/oncall/handoff-summary'])
  })

  it('is case-insensitive on name, segments and key', () => {
    expect(keys(rankSkills(NESTED, 'DATA-Warehouse'))).toHaveLength(2)
    expect(keys(rankSkills(NESTED, 'OnCall'))).toEqual(['ops/oncall/handoff-summary'])
    expect(keys(rankSkills([{ key: 'Group/OnCall', name: 'OnCall' }], 'oncall'))).toEqual(['Group/OnCall'])
  })

  it('an empty query lists every skill in catalog order', () => {
    expect(keys(rankSkills(NESTED, ''))).toEqual(keys(NESTED))
  })

  it('a word nowhere in name, path or key finds nothing', () => {
    expect(rankSkills(NESTED, 'hello')).toEqual([])
  })
})

describe('rankSkills — ranking', () => {
  it('exact name/leaf, then prefix, then path segment, then substring, then fuzzy', () => {
    const rows = [
      { key: 'tools/report-ops', name: 'report-ops' },          // substring of name
      { key: 'ops/handoff', name: 'ops-handoff' },              // prefix of name
      { key: 'group/ops', name: 'group-ops' },                  // exact leaf
      { key: 'infra/ops-main', name: 'ops' },                   // exact name
      { key: 'ops-legacy/cleanup', name: 'cleanup' },           // segment prefix only
      { key: 'other/pull-sync', name: 'open-pull-sync' },       // fuzzy: o-p-s word starts
    ]
    expect(keys(rankSkills(rows, 'ops'))).toEqual([
      'group/ops',
      'infra/ops-main',
      'ops/handoff',
      'ops-legacy/cleanup',
      'tools/report-ops',
      'other/pull-sync',
    ])
  })

  it('keeps catalog order inside a tier and sorts the fuzzy tier best score first', () => {
    const rows = [
      { key: 'git-review', name: 'git-review' },
      { key: 'grill', name: 'grill' },
      { key: 'agree', name: 'agree' },
    ]
    // grill: prefix; agree: substring; git-review: fuzzy (g, r word starts).
    expect(keys(rankSkills(rows, 'gr'))).toEqual(['grill', 'agree', 'git-review'])
  })

  it('bounds fuzzy work on an oversized name: substring still reads the whole name, fuzzy only its head', () => {
    // A 100k-char name with a word-start alignment only near its end. The
    // fuzzy tier is quadratic in the candidate, so it reads a bounded head of
    // the name: `qz` (aligning only past the bound) must NOT match, which is
    // the observable proof the work is bounded; no wall-clock assertion, per
    // the determinism contract.
    const huge = 'p-s-'.repeat(25_000) + 'quiet-zone'
    const rows = [{ key: 'tools/check', name: huge }]
    expect(keys(rankSkills(rows, 'qz'))).toEqual([])          // fuzzy, beyond the bound
    expect(keys(rankSkills(rows, 'ps'))).toEqual(['tools/check'])  // fuzzy, within the bound
    expect(keys(rankSkills(rows, 'quiet-zone'))).toEqual(['tools/check'])  // substring sees it all
  })

  it('a name match is found even when the leaf and key share nothing with the query', () => {
    const rows = [{ key: 'alpha/beta', name: 'release-notes' }]
    expect(keys(rankSkills(rows, 'release'))).toEqual(['alpha/beta'])
  })

  it('lists both rows that share a leaf but collapses the same key listed twice', () => {
    const rows = [
      { key: 'ops/oncall/escalation', name: 'ops-oncall-escalation' },
      { key: 'support/triage/escalation', name: 'support-triage-escalation' },
      { key: 'ops/oncall/escalation', name: 'ops-oncall-escalation' },
    ]
    expect(keys(rankSkills(rows, 'escalation'))).toEqual(['ops/oncall/escalation', 'support/triage/escalation'])
    expect(keys(rankSkills(rows, ''))).toEqual(['ops/oncall/escalation', 'support/triage/escalation'])
  })

  it('a row with no key falls back to its name', () => {
    expect(keys(rankSkills([{ key: '', name: 'grill' }], 'gri'))).toEqual([''])
  })
})

describe('dollarToken — what goes after the $', () => {
  const SHARED = [
    { key: 'ops/oncall/escalation', name: 'ops-oncall-escalation' },
    { key: 'support/triage/escalation', name: 'support-triage-escalation' },
    { key: 'grill', name: 'grill' },
    { key: 'kirocrew/grill', name: 'grill' },
  ]

  it('is the last folder while that leaf is unique in the catalog', () => {
    expect(dollarToken(NESTED[1], NESTED)).toBe('handoff-summary')
    expect(dollarToken({ key: 'grill', name: 'grill' }, [{ key: 'grill', name: 'grill' }])).toBe('grill')
  })

  it('is the full key for a leaf another skill also ends in, so the backend resolves it exactly', () => {
    expect(dollarToken(SHARED[0], SHARED)).toBe('ops/oncall/escalation')
    expect(dollarToken(SHARED[1], SHARED)).toBe('support/triage/escalation')
    expect(dollarToken(SHARED[2], SHARED)).toBe('grill')
    expect(dollarToken(SHARED[3], SHARED)).toBe('kirocrew/grill')
  })

  it('compares leaves case-insensitively, as the backend does', () => {
    const rows = [{ key: 'a/Grill', name: 'Grill' }, { key: 'b/grill', name: 'grill' }]
    expect(dollarToken(rows[1], rows)).toBe('b/grill')
  })

  // /api/skills rekeys project rows to `kiro-workspace/<rel>` and never gives them
  // aliases; the loader holds them under `<rel>`, except behind a same-named
  // global skill, which it lists instead (catalog.py).
  it('a project row is held under its key minus kiro-workspace/, with no alias needed', () => {
    const rows = [
      { key: 'kiro-workspace/ops/escalation', name: 'ops-escalation' },
      { key: 'kiro-workspace/support/escalation', name: 'support-escalation' },
      { key: 'kiro-workspace/foo', name: 'foo' },
      { key: 'group/foo', name: 'foo' },
    ]
    expect(loaderKey(rows[0], rows)).toBe('ops/escalation')
    expect(dollarToken(rows[0], rows)).toBe('ops/escalation')
    expect(dollarToken(rows[1], rows)).toBe('support/escalation')
    expect(keys(rankSkills(rows, 'escalation'))).toEqual(['kiro-workspace/ops/escalation', 'kiro-workspace/support/escalation'])
    expect(dollarToken(rows[2], rows)).toBe('foo')
    expect(dollarToken(rows[3], rows)).toBe('group/foo')
  })

  it('a project row behind a same-named global skill is not loader-held and is hidden', () => {
    const rows = [
      { key: 'foo', name: 'foo' },
      { key: 'kiro-workspace/foo', name: 'foo' },
    ]
    expect(loaderKey(rows[1], rows)).toBeNull()
    expect(dollarToken(rows[1], rows)).toBeNull()
    // The global row's leaf is not made ambiguous by a row the loader never sees.
    expect(dollarToken(rows[0], rows)).toBe('foo')
    expect(keys(rankSkills(rows, 'foo'))).toEqual(['foo'])
  })

  it('a project row is hidden behind a global skill the listing kept under its kiro-user/ spelling', () => {
    // extra_paths names ~/.kiro/skills: the listing keeps the `kiro-user/foo` row
    // (alias `foo`) over the unowned kirocrew row, and the loader holds `foo` from
    // the extra path while skipping the project copy. `$foo` would load the global.
    const rows = [
      { key: 'kiro-user/foo', name: 'foo', aliases: ['foo'] },
      { key: 'kiro-workspace/foo', name: 'foo' },
    ]
    expect(loaderKey(rows[0], rows)).toBe('foo')
    expect(loaderKey(rows[1], rows)).toBeNull()
    expect(dollarToken(rows[1], rows)).toBeNull()
    expect(dollarToken(rows[0], rows)).toBe('foo')
    expect(keys(rankSkills(rows, 'foo'))).toEqual(['kiro-user/foo'])
  })

  it('an unaliased kiro-user/ row with a unique leaf keeps $leaf (an agent mapping can resolve it)', () => {
    // skill://~/.kiro/skills/*/SKILL.md in an agent spec puts the native skill in
    // that session's catalog; the picker lists it and `$foo` resolves by leaf.
    const rows = [
      { key: 'kiro-user/foo', name: 'foo' },
      { key: 'ticket-pull', name: 'ticket-pull' },
    ]
    expect(loaderKey(rows[0], rows)).toBeNull()
    expect(dollarToken(rows[0], rows)).toBe('foo')
    expect(keys(rankSkills(rows, 'foo'))).toEqual(['kiro-user/foo'])
  })

  // skills.extra_paths naming ~/.kiro/skills: the listing keeps the `kiro-user/foo`
  // row and carries the loader's `foo` in aliases; the loader knows only `foo`.
  it('a kiro-user/ row is loader-held only through its alias', () => {
    const rows = [
      { key: 'kiro-user/foo', name: 'foo', aliases: ['foo'] },
      { key: 'group/foo', name: 'foo' },
      { key: 'kiro-user/utils/tiny-url', name: 'tiny-url', aliases: ['utils/tiny-url'] },
      { key: 'other/tiny-url', name: 'tiny-url' },
      { key: 'kiro-user/bare', name: 'bare' },
      { key: 'x/bare', name: 'bare' },
    ]
    expect(dollarToken(rows[0], rows)).toBe('foo')
    expect(dollarToken(rows[2], rows)).toBe('utils/tiny-url')
    // No alias: the loader does not walk ~/.kiro/skills, a bare `$bare` would load
    // x/bare, so the native row is hidden and x/bare keeps its plain leaf token.
    expect(loaderKey(rows[4], rows)).toBeNull()
    expect(dollarToken(rows[4], rows)).toBeNull()
    expect(dollarToken(rows[5], rows)).toBe('bare')
    expect(keys(rankSkills(rows, 'bare'))).toEqual(['x/bare'])
    expect(loaderKey({ key: 'kiro-user/foo', name: 'foo', aliases: ['kiro-workspace/foo', 'foo'] }, [])).toBe('foo')
    // An unprefixed row ignores its aliases: its own key is the loader key.
    expect(loaderKey({ key: 'group/foo', name: 'foo', aliases: ['kiro-user/foo'] }, [])).toBe('group/foo')
  })

  it('a row the loader does not hold never makes another row\'s leaf ambiguous', () => {
    // Ops/foo cannot be spelled as a `$` token, but with only a bare native `foo`
    // beside it, `$foo` still resolves to Ops/foo by leaf: keep it listed.
    const rows = [
      { key: 'Ops/foo', name: 'foo' },
      { key: 'kiro-user/foo', name: 'foo' },
    ]
    expect(dollarToken(rows[0], rows)).toBe('foo')
    expect(dollarToken(rows[1], rows)).toBeNull()
    expect(keys(rankSkills(rows, 'foo'))).toEqual(['Ops/foo'])
  })

  it('treats a duplicate of the same key as the same skill, not a shared leaf', () => {
    const rows = [{ key: 'kirocrew/grill', name: 'grill' }, { key: 'kirocrew/grill', name: 'grill' }]
    expect(dollarToken(rows[0], rows)).toBe('grill')
  })

  it('hides a shared-leaf row whose key the $ grammar cannot carry, instead of inserting a leaf that loads the other skill', () => {
    // `$grill` would resolve exactly to a root `grill` or by leaf to kirocrew/grill,
    // never to AIMPkg/grill; the row is hidden rather than mis-routed.
    const rows = [{ key: 'AIMPkg/grill', name: 'grill' }, { key: 'kirocrew/grill', name: 'grill' }]
    expect(dollarToken(rows[0], rows)).toBeNull()
    expect(dollarToken(rows[1], rows)).toBe('kirocrew/grill')
    expect(keys(rankSkills(rows, 'grill'))).toEqual(['kirocrew/grill'])
    const upper = [{ key: 'foo', name: 'foo' }, { key: 'Ops/foo', name: 'foo' }]
    expect(dollarToken(upper[1], upper)).toBeNull()
    expect(keys(rankSkills(upper, 'ops'))).toEqual([])
    const qualified = [{ key: 'package/0123456789abcdef:shared', name: 'shared' }, { key: 'local/shared', name: 'shared' }]
    expect(dollarToken(qualified[0], qualified)).toBeNull()
    expect(keys(rankSkills(qualified, 'shared'))).toEqual(['local/shared'])
  })

  it('a package/ row is a display spelling too: held through its alias, never inserted as $package/…', () => {
    // An edition root in extra_paths: the listing keeps the `package/foo` row over
    // the unowned kirocrew row and carries the loader's `foo` as an alias.
    const rows = [
      { key: 'package/foo', name: 'foo', aliases: ['foo'] },
      { key: 'group/foo', name: 'foo' },
    ]
    expect(loaderKey(rows[0], rows)).toBe('foo')
    expect(dollarToken(rows[0], rows)).toBe('foo')
    expect(dollarToken(rows[1], rows)).toBe('group/foo')
    // Unaliased and alone under its leaf: `$foo` through an agent mapping, as for kiro-user/.
    const lone = [{ key: 'package/foo', name: 'foo' }, { key: 'grill', name: 'grill' }]
    expect(loaderKey(lone[0], lone)).toBeNull()
    expect(dollarToken(lone[0], lone)).toBe('foo')
    // Unaliased beside a catalog-held `group/foo`: hidden, never `$package/foo`.
    const beside = [{ key: 'package/foo', name: 'foo' }, { key: 'group/foo', name: 'foo' }]
    expect(dollarToken(beside[0], beside)).toBeNull()
    expect(keys(rankSkills(beside, 'foo'))).toEqual(['group/foo'])
  })

  it('two edition roots shipping the same path keep one row under the bare leaf, as the old dedupe did', () => {
    // fold_package_keys rekeys the copies to `package/<q>:shared`; neither fits
    // the `$` grammar, so neither has a qualified token. Hiding both would make
    // the skill vanish; the first keeps `$shared` and the agent's mapping picks.
    const rows = [
      { key: 'package/0123456789abcdef:shared', name: 'shared' },
      { key: 'package/fedcba9876543210:shared', name: 'shared' },
    ]
    expect(dollarToken(rows[0], rows)).toBe('shared')
    expect(dollarToken(rows[1], rows)).toBeNull()
    expect(keys(rankSkills(rows, 'shared'))).toEqual(['package/0123456789abcdef:shared'])
    // With a spellable sibling, the qualified copies hide behind it as before.
    const withLocal = [...rows, { key: 'local/shared', name: 'shared' }]
    expect(keys(rankSkills(withLocal, 'shared'))).toEqual(['local/shared'])
  })

  it('an untrusted project row is not in the catalog yet: it cannot hide a global skill that shares its leaf', () => {
    // Global `WorkforceEmploymentKnowledgeBase/oncall-handover` cannot be spelled
    // as a `$` token, but `$oncall-handover` resolves to it by leaf; an untrusted
    // project `oncall-handover` is listed for consent only, so the global stays.
    const rows = [
      { key: 'WorkforceEmploymentKnowledgeBase/oncall-handover', name: 'oncall-handover' },
      { key: 'kiro-workspace/oncall-handover', name: 'oncall-handover', trusted: false },
    ]
    expect(loaderKey(rows[1], rows)).toBeNull()
    expect(dollarToken(rows[0], rows)).toBe('oncall-handover')
    // The untrusted row still lists, so the consent step stays reachable; it
    // inserts the key consent will put in the catalog.
    expect(dollarToken(rows[1], rows)).toBe('oncall-handover')
    expect(keys(rankSkills(rows, 'oncall'))).toEqual(keys(rows))
    // Once trusted, the project row is held: the global's bare leaf is now
    // ambiguous and it has no spellable key, so it hides behind the project row.
    const trusted = [rows[0], { ...rows[1], trusted: true }]
    expect(loaderKey(trusted[1], trusted)).toBe('oncall-handover')
    expect(dollarToken(trusted[0], trusted)).toBeNull()
    expect(dollarToken(trusted[1], trusted)).toBe('oncall-handover')
  })

  it('two untrusted project groups sharing a leaf each insert the key consent will make resolvable', () => {
    const rows = [
      { key: 'kiro-workspace/ops/escalation', name: 'ops-escalation', trusted: false },
      { key: 'kiro-workspace/support/escalation', name: 'support-escalation', trusted: false },
    ]
    expect(dollarToken(rows[0], rows)).toBe('ops/escalation')
    expect(dollarToken(rows[1], rows)).toBe('support/escalation')
    expect(keys(rankSkills(rows, 'escalation'))).toEqual(keys(rows))
  })

  it('stays linear on a large catalog: ten thousand nested skills rank without rescanning per row', () => {
    // No wall-clock assertion (determinism contract); the shape is pinned by
    // completing on a catalog where an O(n^2) scan would be 1e8 comparisons.
    const rows = Array.from({ length: 10_000 }, (_, i) => ({ key: `g${i % 100}/skill-${i}`, name: `skill-${i}` }))
    expect(rankSkills(rows, '')).toHaveLength(10_000)
    expect(keys(rankSkills(rows, 'skill-9999'))).toEqual(['g99/skill-9999'])
  })

  it('still lists a lone row whose key the $ grammar cannot carry, under its leaf token', () => {
    const rows = [{ key: 'AIMPkg/grill', name: 'grill' }, { key: 'ticket-pull', name: 'ticket-pull' }]
    expect(dollarToken(rows[0], rows)).toBe('grill')
    expect(keys(rankSkills(rows, 'grill'))).toEqual(['AIMPkg/grill'])
  })
})
