/**
 * Every catalog key the source asks for must exist — the gate, and the defect it closes.
 *
 * ## The defect, which is historical rather than hypothetical
 *
 * Phase 3 item 1a collapsed a shared count-badge fragment into a single
 * `show_all_count` key and updated ONE of that key's two render sites in
 * `ActivityViewer.tsx`. The second site kept referencing the deleted
 * `pages.chat.activityViewer.show_all`. i18next returns a missing key as its own
 * fallback instead of throwing, so the button rendered the literal string
 * `pages.chat.activityViewer.show_all` to the user.
 *
 * That state was reproduced on this tree before the gate landed, and every existing
 * i18n gate passed on it: `npm run i18n:check` (5 gates) exited 0, and
 * `npx vitest run src/i18n/` (30 files) exited 0 — `catalogParity` included, because a
 * key absent from `en` is absent from all 9 targets too, which is perfect parity. The
 * only thing that failed was `src/test/ActivityViewer.test.tsx`, and only by accident:
 * it happens to assert the rendered text `/Show all \(5\)/`. A defect caught by
 * coincidence is a defect that ships the next time the coincidence is absent.
 *
 * The first test below is the standing version of that reproduction.
 *
 * ## Why it spawns the real script against a fixture tree
 *
 * The same reason `codemodTransGuard.test.ts` does: `scripts/check-i18n-keys.mjs` is a
 * top-level script that walks the real `src/` and exits with a status. Re-implementing
 * its resolver here would test a copy of the logic rather than the gate CI runs, and the
 * exit code IS the contract. A fixture tree also lets the catalog be wrong on purpose,
 * which is the whole point and cannot be done to the real tree.
 *
 * ## Why the fixtures are padded to thousands of references
 *
 * The script refuses to report a pass on a corpus that is implausibly small — a walker
 * that silently matched nothing would otherwise turn every check below into a no-op.
 * Rather than give the script a test-only escape hatch (an env var that could be set in
 * CI and quietly disable the guard), the fixtures satisfy the guard honestly by
 * generating padding call sites. `refuses to pass on an implausibly small corpus` then
 * covers the guard itself.
 */

import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { describe, it, expect, beforeAll, afterAll } from 'vitest'

/** `website/`, two levels up from `src/i18n/`. */
const WEBSITE = path.resolve(__dirname, '../..')
const GATE = path.join(WEBSITE, 'scripts/check-i18n-keys.mjs')

/**
 * Padding call sites, enough to clear the script's corpus-plausibility floor of 3000.
 *
 * These are plain literals in a separate file, so they add coverage to the static count
 * without interacting with any shape under test.
 */
const PADDING = 3100
const paddingKeys = Array.from({ length: PADDING }, (_, i) => `probe.padding.k${i}`)
const PADDING_SOURCE = `import { i18nT } from '../i18n/t'\n\nexport function Padding() {\n  return [\n${
  paddingKeys.map((k) => `    i18nT('${k}'),`).join('\n')
}\n  ]\n}\n`

interface Run {
  status: number | null
  stdout: string
  stderr: string
}

interface Tree {
  /** Source files to write, keyed by path relative to the fake `website/`. */
  files: Record<string, string>
  /** Leaf keys for `en.manual.json`, on top of the padding keys in `en.json`. */
  keys: string[]
  /** Per-file dynamic-site baseline. Omitted entries mean zero. */
  baseline?: Record<string, number>
  pluralKeys?: string[]
}

const roots: string[] = []

/** Expand dotted leaf keys into the nested object shape i18next resolves against. */
function nest(keys: string[]): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const key of keys) {
    const parts = key.split('.')
    let node = out
    for (const part of parts.slice(0, -1)) {
      if (typeof node[part] !== 'object' || node[part] === null) node[part] = {}
      node = node[part] as Record<string, unknown>
    }
    node[parts[parts.length - 1]] = `probe value for ${key}`
  }
  return out
}

/**
 * A minimal `website/`-shaped tree: the copied gate, fixture sources, and catalogs.
 *
 * `node_modules` is linked so the copied script resolves `typescript` by the normal
 * walk-up from its own path; the script is COPIED rather than symlinked for the same
 * reason `codemodTransGuard.test.ts` copies the codemod — a symlinked entry point
 * resolves to its realpath and the gate would run against the real `src/`.
 *
 * The link type is platform-dependent and that matters: a plain directory symlink
 * needs `SeCreateSymbolicLinkPrivilege` on Windows, i.e. Developer Mode or an elevated
 * shell, so `'dir'` raises `EPERM` on an ordinary Windows checkout and would take the
 * whole i18n suite down with it. A junction is the unprivileged equivalent and is what
 * npm itself uses for local directory dependencies on Windows.
 */
function makeTree(tree: Tree): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'i18n-keyref-'))
  roots.push(root)
  fs.mkdirSync(path.join(root, 'scripts'), { recursive: true })
  fs.mkdirSync(path.join(root, 'src/i18n/locales'), { recursive: true })
  try {
    fs.symlinkSync(
      path.join(WEBSITE, 'node_modules'),
      path.join(root, 'node_modules'),
      process.platform === 'win32' ? 'junction' : 'dir',
    )
  } catch (err) {
    throw new Error(
      `could not link node_modules into the fixture tree (${(err as Error).message}). `
      + 'This test needs it so the copied gate can resolve \'typescript\'. On a platform '
      + 'without unprivileged links, build the fixture tree under website/ instead so '
      + 'resolution walks up to the real node_modules.',
    )
  }
  fs.copyFileSync(GATE, path.join(root, 'scripts/check-i18n-keys.mjs'))

  for (const [rel, source] of Object.entries(tree.files)) {
    fs.mkdirSync(path.join(root, path.dirname(rel)), { recursive: true })
    fs.writeFileSync(path.join(root, rel), source)
  }

  const generated = nest(paddingKeys)
  fs.writeFileSync(path.join(root, 'src/i18n/locales/en.json'), JSON.stringify(generated, null, 2))
  fs.writeFileSync(
    path.join(root, 'src/i18n/locales/en.manual.json'),
    JSON.stringify(nest(tree.keys), null, 2),
  )
  fs.writeFileSync(
    path.join(root, 'src/i18n/pluralKeys.json'),
    JSON.stringify(tree.pluralKeys ?? [], null, 2),
  )
  fs.writeFileSync(
    path.join(root, 'src/i18n/dynamic-keys-baseline.json'),
    JSON.stringify({ _total: 0, files: tree.baseline ?? {} }, null, 2),
  )
  return root
}

function run(tree: Tree, args: string[] = []): Run {
  const root = makeTree(tree)
  const r = spawnSync(
    process.execPath,
    [path.join(root, 'scripts/check-i18n-keys.mjs'), ...args],
    { encoding: 'utf-8' },
  )
  return { status: r.status, stdout: r.stdout ?? '', stderr: r.stderr ?? '' }
}

const withPadding = (files: Record<string, string>) => ({
  'src/probe/Padding.tsx': PADDING_SOURCE,
  ...files,
})

afterAll(() => {
  for (const root of roots) fs.rmSync(root, { recursive: true, force: true })
})

// ------------------------------------------------------------------ the historical defect

/**
 * `ActivityViewer.tsx`'s two render sites, reduced to the shape that mattered: both
 * reach for the same key, and item 1a renamed it in the catalog while updating only one.
 */
const ACTIVITY_VIEWER = (libraryKey: string) => `import { i18nT } from '../i18n/t'

export function ActivityViewer({ cappedLibrary, ids }: { cappedLibrary: number; ids: string[] }) {
  return (
    <>
      <button type="button">
        {i18nT('pages.chat.activityViewer.${libraryKey}', { count: cappedLibrary })}
      </button>
      <button type="button" data-testid="show-all-subagents">
        {i18nT('pages.chat.activityViewer.show_all_count', { count: ids.length })}
      </button>
    </>
  )
}
`

const CATALOG_AFTER_ITEM_1A = ['pages.chat.activityViewer.show_all_count']

describe('the Phase 3 item 1a defect', () => {
  let broken: Run
  let fixed: Run

  beforeAll(() => {
    // Exactly the committed state item 1a left behind: the catalog holds only the new
    // `show_all_count`, and one render site still asks for the deleted `show_all`.
    broken = run({
      files: withPadding({ 'src/pages/chat/ActivityViewer.tsx': ACTIVITY_VIEWER('show_all') }),
      keys: CATALOG_AFTER_ITEM_1A,
    })
    fixed = run({
      files: withPadding({ 'src/pages/chat/ActivityViewer.tsx': ACTIVITY_VIEWER('show_all_count') }),
      keys: CATALOG_AFTER_ITEM_1A,
    })
  }, 120_000)

  it('fails on the render site that still references the deleted key', () => {
    expect(broken.status, `stdout:\n${broken.stdout}\nstderr:\n${broken.stderr}`).toBe(1)
    expect(broken.stderr).toContain('pages.chat.activityViewer.show_all')
    // The location, not just the key: a gate that reports "something is wrong" without
    // saying where costs the reader the search that the AST already did.
    expect(broken.stderr).toMatch(/pages\/chat\/ActivityViewer\.tsx:\d+/)
  })

  it('passes once both sites point at the key that exists', () => {
    expect(fixed.status, `stdout:\n${fixed.stdout}\nstderr:\n${fixed.stderr}`).toBe(0)
  })
})

// ------------------------------------------------------------------ resolvable shapes

/**
 * One fixture per shape the resolver claims to see through, each with a distinct key.
 *
 * The `as const` map and the indexed lookup are here because they are the repo's OWN
 * recommended fix for a dynamic key (`dynamicKeys.test.ts` points at `UPDATE_ERROR_KEYS`
 * and `STATUS_LABEL_KEY`). A gate blind to the recommended fix would push every author
 * back toward the shape the other gate bans.
 */
const SHAPES_SOURCE = `import { i18nT } from '../i18n/t'
import { Trans } from 'react-i18next'

const BOUND = 'probe.shape.bound'
const AS_CONST_MAP = {
  offline: 'probe.shape.map_offline',
  server: 'probe.shape.map_server',
} as const
const INDEXED = {
  active: 'probe.shape.indexed_active',
  disabled: 'probe.shape.indexed_disabled',
}
const ALIASED = BOUND

export const SURFACE = { navId: 'probe', label: 'Probe', labelKey: 'probe.shape.label' }

export function Shapes({ st, flag }: { st: 'active' | 'disabled'; flag: boolean }) {
  return (
    <>
      {i18nT('probe.shape.literal')}
      {i18nT(\`probe.shape.tagless\`)}
      {i18nT(BOUND)}
      {i18nT(ALIASED)}
      {i18nT(AS_CONST_MAP.offline)}
      {i18nT(AS_CONST_MAP['server'])}
      {i18nT(INDEXED[st])}
      {i18nT(flag ? 'probe.shape.cond_a' : 'probe.shape.cond_b')}
      {i18nT((flag ? 'probe.shape.paren' : 'probe.shape.cond_b') as string)}
      <Trans i18nKey="probe.shape.trans">Hello <strong>there</strong></Trans>
    </>
  )
}
`

/** Every key `SHAPES_SOURCE` can reach — the indexed lookup contributes BOTH entries. */
const SHAPE_KEYS = [
  'probe.shape.literal',
  'probe.shape.tagless',
  'probe.shape.bound',
  'probe.shape.map_offline',
  'probe.shape.map_server',
  'probe.shape.indexed_active',
  'probe.shape.indexed_disabled',
  'probe.shape.cond_a',
  'probe.shape.cond_b',
  'probe.shape.paren',
  'probe.shape.label',
  'probe.shape.trans',
]

describe('key resolution beyond the plain literal', () => {
  let complete: Run
  let empty: Run

  beforeAll(() => {
    complete = run({ files: withPadding({ 'src/probe/Shapes.tsx': SHAPES_SOURCE }), keys: SHAPE_KEYS })
    // Same source, none of the shape keys in the catalog. Every shape the resolver
    // really sees through must now be reported — one run, twelve assertions.
    empty = run({ files: withPadding({ 'src/probe/Shapes.tsx': SHAPES_SOURCE }), keys: [] })
  }, 120_000)

  it('accepts all of them when every key exists', () => {
    expect(complete.status, `stdout:\n${complete.stdout}\nstderr:\n${complete.stderr}`).toBe(0)
  })

  it.each(SHAPE_KEYS)('resolves %s well enough to report it missing', (key) => {
    expect(
      empty.stderr,
      'A shape the resolver cannot see through is silently exempt from the gate: the key is '
      + 'never checked, so deleting it from the catalog is invisible. That is worse than a '
      + 'false positive, because nothing surfaces it.\n' + empty.stderr,
    ).toContain(key)
  })

  it('reports no dynamic sites, since every shape here is resolvable', () => {
    expect(complete.stdout).toContain('0 dynamic site(s)')
  })
})

// ------------------------------------------------------------------ dynamic keys

/**
 * The three genuinely unresolvable shapes, plus the one the repo bans outright.
 *
 * `dynamicKeys.test.ts` forbids ASSEMBLING a key (`+` and `${…}`), so this gate must
 * never quietly resolve an assembled one: if it did, an author could satisfy this gate
 * with the shape the other gate rejects, and the two would be pulling in opposite
 * directions. Assembly therefore counts as dynamic here — visible in the ratchet — not
 * as a resolvable value.
 */
const DYNAMIC_SOURCE = `import { i18nT } from '../i18n/t'

export function surfaceLabel(s: { label: string; labelKey?: string }): string {
  return s.labelKey ? i18nT(s.labelKey) : s.label
}

export function assembled(id: string, prefix: string) {
  return [
    i18nT(\`probe.dynamic.\${id}\`),
    i18nT(prefix + 'suffix'),
    i18nT(id),
  ]
}
`

/** `s.labelKey`, the template, the concatenation, and the bare parameter. */
const DYNAMIC_SITES = 4

describe('dynamic call sites are counted, never waved through', () => {
  it('holds when the baseline matches', () => {
    const r = run({
      files: withPadding({ 'src/probe/Dynamic.tsx': DYNAMIC_SOURCE }),
      keys: [],
      baseline: { 'probe/Dynamic.tsx': DYNAMIC_SITES },
    })
    expect(r.status, `stdout:\n${r.stdout}\nstderr:\n${r.stderr}`).toBe(0)
    expect(r.stdout).toContain(`${DYNAMIC_SITES} dynamic site(s)`)
  })

  it('fails when a file gains one', () => {
    const r = run({
      files: withPadding({ 'src/probe/Dynamic.tsx': DYNAMIC_SOURCE }),
      keys: [],
      baseline: { 'probe/Dynamic.tsx': DYNAMIC_SITES - 1 },
    })
    expect(r.status).toBe(1)
    expect(r.stderr).toContain('cannot be resolved statically')
  })

  it('fails when a file LOSES one, so the coverage gain cannot be given back', () => {
    // The bidirectional half, for the reason `check-i18n-strings.mjs` gives: a one-way
    // ratchet has no downward pressure, so the debt sits at N forever.
    const r = run({
      files: withPadding({ 'src/probe/Dynamic.tsx': DYNAMIC_SOURCE }),
      keys: [],
      baseline: { 'probe/Dynamic.tsx': DYNAMIC_SITES + 1 },
    })
    expect(r.status).toBe(1)
    expect(r.stderr).toContain('improved')
  })

  it('never resolves an assembled key, which dynamicKeys.test.ts bans', () => {
    // If `+` or `${…}` were resolved, these would be checked as keys and reported as
    // missing rather than counted as dynamic.
    const r = run({
      files: withPadding({ 'src/probe/Dynamic.tsx': DYNAMIC_SOURCE }),
      keys: [],
      baseline: { 'probe/Dynamic.tsx': DYNAMIC_SITES },
    })
    expect(r.stderr).not.toContain('probe.dynamic.')
  })
})

// ------------------------------------------------------------------ scope safety

/**
 * A file-scope `const` is followed; a name shadowed by a parameter or a nested binding
 * is NOT, because resolving it to the outer binding would check the wrong key.
 * Unresolvable is a counted, visible outcome; resolved-incorrectly is not.
 */
const SHADOW_SOURCE = `import { i18nT } from '../i18n/t'

const KEY = 'probe.shadow.outer'

export function Shadowed(KEY: string) {
  return i18nT(KEY)
}
`

describe('scope', () => {
  it('treats a shadowed name as dynamic rather than resolving the outer binding', () => {
    const clean = run({
      files: withPadding({ 'src/probe/Shadow.tsx': SHADOW_SOURCE }),
      keys: [],
      baseline: { 'probe/Shadow.tsx': 1 },
    })
    expect(clean.status, `stdout:\n${clean.stdout}\nstderr:\n${clean.stderr}`).toBe(0)
    // The outer key must NOT be reported: the call site cannot reach it, and asserting a
    // key the code never asks for is how a gate loses its reader's trust.
    expect(clean.stderr).not.toContain('probe.shadow.outer')
  })
})

// ------------------------------------------------------------------ plurals

/**
 * A plural key is CALLED by its base, which never exists in the catalog —
 * `Intl.PluralRules` appends the category at runtime. Treating the base as missing would
 * report every plural call site in the repo as dangling, so `pluralKeys.json` is
 * consulted. It is consulted INSTEAD OF a `_one` / `_other` suffix scan, which is the
 * property the second test pins.
 */
const PLURAL_SOURCE = `import { i18nT } from '../i18n/t'

export function Plurals({ n }: { n: number }) {
  return [
    i18nT('probe.plural.registered', { count: n }),
    i18nT('probe.plural.bare_count', { count: n }),
  ]
}
`

/**
 * A base that is NOT a plural at all: the catalog holds `…_one` because "one" is the
 * last word of the English sentence. Three real keys have this shape
 * (`pages.channelPage.click_new_to_create_one`, `…panel_to_add_one`,
 * `…add_column_after_this_one`), which is why the suffix scan cannot be used.
 */
const FALSE_PLURAL_SOURCE = `import { i18nT } from '../i18n/t'

export function FalsePlural() {
  return i18nT('probe.plural.click_new_to_create')
}
`

describe('plural keys', () => {
  it('resolves a registered base whose only catalog entries are suffixed forms', () => {
    const r = run({
      files: withPadding({ 'src/probe/Plurals.tsx': PLURAL_SOURCE }),
      keys: [
        // The registered base has no bare entry — the registry IS its declaration, and
        // `catalogParity` builds the suffixed forms from it.
        'probe.plural.registered_one',
        'probe.plural.registered_other',
        // `{{count}}` with no plural siblings, which i18next 26 resolves cleanly.
        'probe.plural.bare_count',
      ],
      pluralKeys: ['probe.plural.registered'],
    })
    expect(r.status, `stdout:\n${r.stdout}\nstderr:\n${r.stderr}`).toBe(0)
  })

  it('reports an UNREGISTERED base even when a _one sibling exists', () => {
    // The regression this locks: resolving by suffix would silently accept
    // `…click_new_to_create` because `…click_new_to_create_one` is in the catalog as
    // ordinary English. The key the call site actually names exists nowhere, and the
    // registry is the only thing that can tell the two cases apart.
    const r = run({
      files: withPadding({ 'src/probe/FalsePlural.tsx': FALSE_PLURAL_SOURCE }),
      keys: ['probe.plural.click_new_to_create_one'],
      pluralKeys: [],
    })
    expect(r.status).toBe(1)
    expect(r.stderr).toContain('probe.plural.click_new_to_create')
  })

  it('still fails for a count-bearing key that exists in no form', () => {
    const r = run({
      files: withPadding({ 'src/probe/Plurals.tsx': PLURAL_SOURCE }),
      keys: ['probe.plural.bare_count'],
      pluralKeys: [],
    })
    expect(r.status).toBe(1)
    expect(r.stderr).toContain('probe.plural.registered')
  })
})

// ------------------------------------------------------------------ catalog shadowing

describe('en.json / en.manual.json shadowing', () => {
  it('fails when one key is defined in both, since the merge silently picks one', () => {
    const r = run({
      files: withPadding({}),
      keys: ['probe.padding.k0'],
    })
    expect(r.status).toBe(1)
    expect(r.stderr).toContain('exist in BOTH en.json and en.manual.json')
    expect(r.stderr).toContain('probe.padding.k0')
  })
})

// ------------------------------------------------------------------ the gate's own guards

describe('the gate refuses to report a pass it cannot justify', () => {
  it('exits 2 on an implausibly small corpus rather than passing vacuously', () => {
    // Without the padding file there are two references. A walker bug that matched
    // almost nothing would otherwise print OK and gate nothing at all — the failure mode
    // deadKeys.test.ts and dynamicKeys.test.ts both guard against explicitly.
    const r = run({
      files: { 'src/probe/Tiny.tsx': "import { i18nT } from '../i18n/t'\nexport const a = i18nT('probe.padding.k0')\n" },
      keys: [],
    })
    expect(r.status).toBe(2)
    expect(r.stderr).toContain('the scan is broken')
  })

  it('exits 2 on an unknown flag rather than silently gating nothing', () => {
    // `--check` is not a flag this script has; a typo'd invocation in CI must fail loudly
    // instead of falling through to a no-op, which is the property ci.yml already calls
    // out for the codemod.
    const r = run({ files: withPadding({}), keys: [] }, ['--check'])
    expect(r.status).toBe(2)
  })
})

// ------------------------------------------------------------------ wiring

describe('wiring', () => {
  it('is invoked by npm run i18n:check', () => {
    // The gate lives in a script, so nothing in the vitest suite would notice it being
    // dropped from the chain CI runs. `englishIdentity.test.ts` reads codemod source for
    // the same reason: assert the mechanism, not only the behaviour.
    const pkg = JSON.parse(fs.readFileSync(path.join(WEBSITE, 'package.json'), 'utf-8'))
    expect(pkg.scripts['i18n:check']).toContain('check-i18n-keys.mjs')
  })

  it('gates the real tree at zero dangling references', () => {
    // Not a ratchet, deliberately: a dangling reference renders a raw dotted key to a
    // user, and there is no coherent "acceptable number" of call sites doing that.
    const r = spawnSync(process.execPath, [GATE], { encoding: 'utf-8', cwd: WEBSITE })
    expect(r.status, `stdout:\n${r.stdout}\nstderr:\n${r.stderr}`).toBe(0)
  }, 60_000)
})
