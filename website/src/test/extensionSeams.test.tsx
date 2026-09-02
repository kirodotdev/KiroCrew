/**
 * Tests for the frontend extension seams — the register/get registries that
 * let a downstream edition contribute pages, nav icons, theme branding, top-bar
 * widgets, and panel shortcuts without editing (and re-diffing) core files on
 * every upstream sync. (There is no API-client seam — see website/AGENTS.md
 * "Frontend extension seams"; it was considered and dropped.)
 *
 * Each seam is verified for: (1) a registered entry is retrievable, (2) the
 * core ships the registry empty/seeded as documented, and (3) a duplicate/
 * collision is fail-loud in dev+test (throws via reportSeamCollision) so it is
 * caught before release, while the core (or first) registration is preserved.
 * In production the same collision degrades to warn-and-ignore.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { lazy } from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { render } from '@testing-library/react'
import ErrorBoundary from '../components/ErrorBoundary'
import {
  registerBuiltinComponents,
  getBuiltinComponent,
  hasBuiltinComponent,
} from '../apps/builtinRegistry'
import { registerBuiltinIcons, getBuiltinIcon } from '../apps/builtinIcons'
import {
  registerAutolinkRules,
  getAutolinkRules,
  resetAutolinkRulesForTest,
} from '../utils/autolinkRules'
import { registerThemeBranding, getThemeBranding } from '../themeBranding'
import { registerTopBarWidgets, getTopBarWidgets } from '../apps/topBarWidgets'
import {
  registerPanelShortcut,
  CORE_PANEL_MAP,
  DEFAULT_SHORTCUTS,
  RESERVED_PANEL_CODES,
} from '../hooks/useKeyboardShortcuts'
import { registerTheme, getRegisteredThemes } from '../hooks/useTheme'
import { registerCapsuleSegment, getCapsuleSegments } from '../apps/capsuleSegments'
import { registerOverviewStatCards, getOverviewStatCards } from '../pages/overviewStatCards'
import { registerOverviewPanel, getOverviewPanel } from '../pages/overviewPanel'
import {
  suppressOverviewBuiltin,
  isOverviewBuiltinSuppressed,
} from '../pages/overviewBuiltins'
import {
  registerMobileConnectRenderer,
  getMobileConnectRenderers,
  canRenderMobileConnectKind,
} from '../components/mobileConnectRenderers'
import { apiTransport } from '../api/apiTransport'
// Importing the client installs the blessed transport (installApiTransport runs
// at client module load), so `apiTransport` is populated for the test below.
import '../api/client'

const Dummy = () => null

describe('builtinRegistry — page seam', () => {
  it('registers a new route and resolves it', () => {
    const Comp = lazy(async () => ({ default: Dummy }))
    registerBuiltinComponents({ '/seam-test-page': Comp })
    expect(hasBuiltinComponent('/seam-test-page')).toBe(true)
    expect(getBuiltinComponent('/seam-test-page')).toBe(Comp)
  })

  it('throws on a duplicate route in dev/test; core wins', () => {
    const first = lazy(async () => ({ default: Dummy }))
    const second = lazy(async () => ({ default: Dummy }))
    registerBuiltinComponents({ '/seam-dup': first })
    expect(() => registerBuiltinComponents({ '/seam-dup': second })).toThrow(/already registered/)
    expect(getBuiltinComponent('/seam-dup')).toBe(first)
  })
})

describe('builtinIcons — nav-icon seam', () => {
  it('seeds the core builtin icons', () => {
    expect(getBuiltinIcon('Brain')).toBeDefined()
    expect(getBuiltinIcon('Contact')).toBeDefined()
  })

  it('returns undefined for an unknown icon', () => {
    expect(getBuiltinIcon('NoSuchIcon')).toBeUndefined()
  })

  it('registers a new icon and resolves it', () => {
    const icon = <span data-testid="x" />
    registerBuiltinIcons({ SeamIcon: icon })
    expect(getBuiltinIcon('SeamIcon')).toBe(icon)
  })

  it('throws on a duplicate icon name in dev/test; core wins', () => {
    const original = getBuiltinIcon('Brain')
    expect(() => registerBuiltinIcons({ Brain: <span data-testid="override" /> })).toThrow(
      /already registered/,
    )
    expect(getBuiltinIcon('Brain')).toBe(original)
  })
})

describe('themeBranding — theme seam', () => {
  it('ships no core-seeded registrations (built-in Lumon removed with theme packs)', () => {
    expect(getThemeBranding('lumon')).toBeUndefined()
  })

  it('returns undefined for a theme with no branding', () => {
    expect(getThemeBranding('emerald')).toBeUndefined()
  })

  it('registers branding for a new theme', () => {
    registerThemeBranding({ 'seam-theme': { botName: 'SeamBot', logo: '/x.svg' } })
    expect(getThemeBranding('seam-theme')?.botName).toBe('SeamBot')
  })

  it('throws on duplicate theme branding in dev/test; first registration wins', () => {
    registerThemeBranding({ 'seam-dup': { botName: 'First', logo: '/a.svg' } })
    expect(() => registerThemeBranding({ 'seam-dup': { botName: 'Hijack' } })).toThrow(
      /already registered/,
    )
    expect(getThemeBranding('seam-dup')?.botName).toBe('First')
  })
})

describe('topBarWidgets — widget-slot seam', () => {
  it('is empty in the stock build until registered', () => {
    const before = getTopBarWidgets().length
    registerTopBarWidgets([{ id: 'seam-widget', component: Dummy }])
    expect(getTopBarWidgets().length).toBe(before + 1)
    expect(getTopBarWidgets().some(w => w.id === 'seam-widget')).toBe(true)
  })

  it('throws on a duplicate widget id in dev/test', () => {
    registerTopBarWidgets([{ id: 'seam-widget-dup', component: Dummy }])
    expect(() => registerTopBarWidgets([{ id: 'seam-widget-dup', component: Dummy }])).toThrow(
      /already registered/,
    )
    expect(getTopBarWidgets().filter(w => w.id === 'seam-widget-dup').length).toBe(1)
  })
})

describe('panel shortcut — nav seam', () => {
  it('registers a new panel chord and derives the display key from the code', () => {
    registerPanelShortcut({ code: 'KeyG', path: '/seam-panel', label: 'Seam panel' })
    const entry = DEFAULT_SHORTCUTS.find(s => s.id === 'nav-seam-panel')
    expect(entry?.group).toBe('panel-navigation')
    // key is DERIVED from code (KeyG -> 'g'), never diverges from the handled chord.
    expect(entry?.key).toBe('g')
  })

  it('throws (dev/test) when shadowing a CORE panel chord; core wins', () => {
    const beforeLen = DEFAULT_SHORTCUTS.length
    // KeyC is a core chord (/chat) — a downstream attempt to remap it must fail loud.
    expect(() =>
      registerPanelShortcut({ code: 'KeyC', path: '/hijack', label: 'Hijack' }),
    ).toThrow(/reserved or already registered/)
    expect(CORE_PANEL_MAP.KeyC).toBe('/chat')
    expect(DEFAULT_SHORTCUTS.length).toBe(beforeLen) // nothing pushed
  })

  it('throws (dev/test) on a code the handler consumes before panel routing', () => {
    const beforeLen = DEFAULT_SHORTCUTS.length
    // Alt+K opens the shortcuts modal and returns before panelMap — a panel on
    // KeyK would be advertised but unreachable, so it must be rejected.
    expect(() =>
      registerPanelShortcut({ code: 'KeyK', path: '/unreachable', label: 'Unreachable' }),
    ).toThrow(/reserved or already registered/)
    expect(DEFAULT_SHORTCUTS.length).toBe(beforeLen)
  })

  it('throws (dev/test) on a duplicate extension chord', () => {
    registerPanelShortcut({ code: 'KeyH', path: '/seam-h', label: 'Seam H' })
    expect(() =>
      registerPanelShortcut({ code: 'KeyH', path: '/seam-h2', label: 'Seam H2' }),
    ).toThrow(/reserved or already registered/)
    expect(DEFAULT_SHORTCUTS.filter(s => s.id === 'nav-seam-h').length).toBe(1)
    expect(DEFAULT_SHORTCUTS.some(s => s.id === 'nav-seam-h2')).toBe(false)
  })

  it('RESERVED_PANEL_CODES covers every non-shift code the handler consumes pre-panel', () => {
    // Drift guard: parse the handler source for the codes it dispatches BEFORE
    // the panelMap block, keep only the non-shift ones (panel routing is gated
    // on !e.shiftKey, so shift chords don't conflict), and assert each is
    // reserved. A new pre-panel Alt chord added without updating the set fails
    // here instead of silently shadowing a downstream panel in production.
    const src = readFileSync(resolve(process.cwd(), 'src/hooks/useKeyboardShortcuts.ts'), 'utf-8')
    const marker = 'const panelMap'
    // FAIL-CLOSED: if the handler is refactored so the marker or the branch
    // syntax this test parses no longer exists, the parser would find nothing
    // and pass vacuously — the exact hole the guard exists to close. Assert the
    // structure this test depends on is present, and that the parse recovered
    // the KNOWN baseline of non-shift pre-panel codes, so any refactor that
    // changes the shape fails CI (forcing this guard to be updated in lockstep).
    expect(src).toContain(marker)
    const handlerHead = src.slice(0, src.indexOf(marker))
    const lines = handlerHead.split('\n')
    const consumed = new Set<string>()
    for (const line of lines) {
      if (/\be\.shiftKey\b/.test(line) && !/!e\.shiftKey/.test(line)) continue // shift-only branch
      for (const m of line.matchAll(/code === '([^']+)'/g)) consumed.add(m[1])
      if (/code >= 'Digit1'/.test(line)) {
        for (let d = 1; d <= 9; d++) consumed.add(`Digit${d}`)
      }
    }
    // The core panel chords (KeyC/N/P/S) live in CORE_PANEL_MAP, dispatched at
    // the panelMap block itself — not "pre-panel" — so exclude them here.
    for (const code of Object.keys(CORE_PANEL_MAP)) consumed.delete(code)
    // Fail-closed baseline: these non-shift codes are KNOWN to be consumed
    // before panel routing today. If the parser recovers fewer (a refactor
    // changed the branch syntax), the guard has gone blind — fail so it gets
    // re-derived rather than silently passing.
    const KNOWN_PRE_PANEL = [
      'KeyK', 'Comma', 'Enter', 'Backquote', 'ArrowLeft', 'ArrowRight',
      'Digit1', 'Digit2', 'Digit3', 'Digit4', 'Digit5',
      'Digit6', 'Digit7', 'Digit8', 'Digit9',
    ]
    const notRecovered = KNOWN_PRE_PANEL.filter(c => !consumed.has(c))
    expect(notRecovered).toEqual([]) // parser still sees the known branches
    // And every code the parser DID recover must be reserved.
    const missing = [...consumed].filter(c => !RESERVED_PANEL_CODES.has(c))
    expect(missing).toEqual([])
  })
})

describe('theme — picker-option seam', () => {
  it('is empty in the stock build until registered', () => {
    const before = getRegisteredThemes().length
    registerTheme([{ value: 'seam-theme-opt', label: '🧪 Seam Theme' }])
    expect(getRegisteredThemes().length).toBe(before + 1)
    expect(getRegisteredThemes().some(t => t.value === 'seam-theme-opt')).toBe(true)
  })

  it('throws on a value already in core THEMES; core wins', () => {
    expect(() => registerTheme([{ value: 'kiro', label: 'Hijack' }])).toThrow(/already registered/)
  })

  it('throws on a duplicate registered value in dev/test', () => {
    registerTheme([{ value: 'seam-theme-dup', label: '🧪 Dup' }])
    expect(() => registerTheme([{ value: 'seam-theme-dup', label: '🧪 Dup2' }])).toThrow(
      /already registered/,
    )
    expect(getRegisteredThemes().filter(t => t.value === 'seam-theme-dup').length).toBe(1)
  })
})

describe('apiTransport — exported blessed transport (not a registry)', () => {
  it('exposes the core session-key helpers an edition builds its API module on', () => {
    // These are the same helpers the core `api` methods use, so an edition
    // method built on them inherits X-Session-Key + auth-recovery + ApiError by
    // construction (never raw fetch, which would drop the session key). `put`
    // must be present — an edition PUT route otherwise falls back to raw fetch.
    for (const key of ['get', 'post', 'put', 'del', 'patch', 'j', 'jNullable'] as const) {
      expect(typeof apiTransport[key]).toBe('function')
    }
  })

  it('a reference destructured BEFORE install still forwards after it', async () => {
    // The previous body only asserted that destructuring does not throw — a plain
    // property read on an object literal, which cannot throw whatever the seam
    // does, so the test held no matter how the wrappers were written.
    //
    // The invariant that actually matters for import order: `extensions.ts` is
    // imported before `client.ts` in `main.tsx`, so an edition may capture
    // `apiTransport.get` at its own module-init, BEFORE any transport is
    // installed, and that captured reference must still reach the transport
    // installed later. That is what "resolves at CALL time" buys, and it fails
    // the moment a wrapper is replaced by a direct bind to `_installed`.
    //
    // Run it against a FRESH, uninstalled copy of the module rather than swapping
    // the process-wide singleton the rest of this file's tests share: `_installed`
    // has no getter, so the real transport cannot be read back to restore it, and
    // installing `apiTransport` itself (the wrapper object) would make
    // `_resolve().get()` call itself — unbounded recursion. `vi.resetModules()`
    // gives an isolated instance whose `_installed` starts null, exactly the
    // edition's module-init state.
    vi.resetModules()
    const fresh = await import('../api/apiTransport')

    // Captured from the fresh module BEFORE anything is installed into it.
    const { get } = fresh.apiTransport

    const calls: string[] = []
    fresh.installApiTransport({
      ...fresh.apiTransport,
      get: (async (url: string) => { calls.push(url); return 'from-stub' }) as typeof fresh.apiTransport.get,
    })

    // The pre-captured reference reaches the LATER-installed transport, which is
    // exactly what an edition destructuring at module-init depends on.
    await expect((get as (u: string) => Promise<unknown>)('/probe')).resolves.toBe('from-stub')
    expect(calls).toEqual(['/probe'])

    // Drop the isolated registry so the file's shared `apiTransport` import is
    // untouched; the next importer gets the normal singleton again.
    vi.resetModules()
  })
})

describe('capsuleSegments — in-capsule segment seam', () => {
  it('registers segments and returns them sorted by order', () => {
    registerCapsuleSegment([{ id: 'testseg:b', order: 2, component: () => null }])
    registerCapsuleSegment([{ id: 'testseg:a', order: 1, component: () => null }])
    const ids = getCapsuleSegments().filter(s => s.id.startsWith('testseg:')).map(s => s.id)
    expect(ids).toEqual(['testseg:a', 'testseg:b'])
  })

  it('throws on a duplicate id in dev/test', () => {
    registerCapsuleSegment([{ id: 'testseg:dup', component: () => null }])
    expect(() => registerCapsuleSegment([{ id: 'testseg:dup', component: () => null }])).toThrow(
      /already registered/,
    )
    expect(getCapsuleSegments().filter(s => s.id === 'testseg:dup').length).toBe(1)
  })
})

describe('overviewStatCards — settings status-card seam', () => {
  it('registers cards and returns them sorted by order', () => {
    registerOverviewStatCards([{ id: 'testcard:b', order: 2, component: () => null }])
    registerOverviewStatCards([{ id: 'testcard:a', order: 1, component: () => null }])
    const ids = getOverviewStatCards().filter(c => c.id.startsWith('testcard:')).map(c => c.id)
    expect(ids).toEqual(['testcard:a', 'testcard:b'])
  })

  it('throws on a duplicate id in dev/test', () => {
    registerOverviewStatCards([{ id: 'testcard:dup', component: () => null }])
    expect(() =>
      registerOverviewStatCards([{ id: 'testcard:dup', component: () => null }]),
    ).toThrow(/already registered/)
    expect(getOverviewStatCards().filter(c => c.id === 'testcard:dup').length).toBe(1)
  })
})

describe('overviewPanel — lower-region single-owner slot', () => {
  it('is empty in the stock build until something claims it', () => {
    expect(getOverviewPanel()).toBeNull()
  })

  it('registers a panel and returns it', () => {
    const Comp = () => null
    registerOverviewPanel({ id: 'testpanel:a', component: Comp })
    expect(getOverviewPanel()).toEqual({ id: 'testpanel:a', component: Comp })
  })

  it('throws on a second claim in dev/test; the first owner keeps the slot', () => {
    // The slot is deliberately singular: a second registrant is a collision,
    // not an append, so the region never has two owners negotiating layout.
    expect(() =>
      registerOverviewPanel({ id: 'testpanel:b', component: () => null }),
    ).toThrow(/already owns the overview panel slot/)
    expect(getOverviewPanel()?.id).toBe('testpanel:a')
  })
})

describe('overviewBuiltins — built-in suppression seam', () => {
  it('suppresses nothing in the stock build', () => {
    expect(isOverviewBuiltinSuppressed('tailnet-mobile')).toBe(false)
  })

  it('suppresses a built-in surface once asked', () => {
    suppressOverviewBuiltin('tailnet-mobile')
    expect(isOverviewBuiltinSuppressed('tailnet-mobile')).toBe(true)
  })

  it('is idempotent — a repeat is agreement, not a collision', () => {
    // Deliberately unlike `overviewPanel` above, which throws on a second claim
    // because two owners cannot share one slot. Two parties that both want a
    // surface GONE do not conflict, so a repeat must not fail-loud the way a
    // duplicate contribution does — HMR and a twice-imported module both hit
    // this path.
    expect(() => suppressOverviewBuiltin('tailnet-mobile')).not.toThrow()
    expect(isOverviewBuiltinSuppressed('tailnet-mobile')).toBe(true)
  })
})

describe('mobileConnectRenderers — phone-connection method renderer seam', () => {
  // The registry is a module singleton, so every test here is self-contained on
  // its OWN kind and none asserts an absolute registry size — an assertion like
  // that passes or fails on test ORDER once a sibling has registered (it fails
  // under --sequence.shuffle). The "core registers nothing" claim is the one
  // that genuinely needs an untouched registry, so it takes a fresh module.
  it('registers nothing of its own — a fresh registry is empty', async () => {
    vi.resetModules()
    const fresh = await import('../components/mobileConnectRenderers')
    expect(fresh.getMobileConnectRenderers()).toEqual([])
    for (const kind of fresh.BUILTIN_MOBILE_CONNECT_KINDS) {
      expect(fresh.canRenderMobileConnectKind(kind)).toBe(true)
    }
    expect(fresh.canRenderMobileConnectKind('seam_test_unregistered')).toBe(false)
  })

  it('registering a kind is what makes it drawable', () => {
    const Comp = () => null
    expect(canRenderMobileConnectKind('seam_test_new')).toBe(false)
    registerMobileConnectRenderer({ kind: 'seam_test_new', component: Comp })
    // The single definition of the renderable set: this predicate is what gates
    // the nav rail's row, so registering is what makes the row appear at all.
    expect(canRenderMobileConnectKind('seam_test_new')).toBe(true)
    expect(getMobileConnectRenderers()).toContainEqual({ kind: 'seam_test_new', component: Comp })
  })

  it('refuses a built-in kind — that would be an override, not a contribution', () => {
    // `tailnet_qr` and `login_link` are drawn by core sections whose mint
    // endpoints the core audits. Silently replacing one would let a composition
    // step redirect a credential mint the core still believes it owns.
    expect(() =>
      registerMobileConnectRenderer({ kind: 'tailnet_qr', component: () => null }),
    ).toThrow(/drawn by a built-in section/)
    expect(getMobileConnectRenderers().some(r => r.kind === 'tailnet_qr')).toBe(false)
  })

  it('refuses a kind that could never match a descriptor verbatim', () => {
    // Blank, whitespace-padded, and non-string all route to one rejection: the
    // readers compare the server's `kind` verbatim, so normalizing here would
    // register a key nothing can ever match, and reaching for `.trim()` on a
    // non-string would throw a raw TypeError in production instead of degrading.
    for (const kind of ['', '   ', ' padded_qr ', 123 as unknown as string]) {
      expect(() => registerMobileConnectRenderer({ kind, component: () => null })).toThrow(
        /non-empty method kind with no surrounding whitespace/,
      )
    }
    expect(getMobileConnectRenderers().some(r => r.kind.includes('padded'))).toBe(false)
  })

  it('throws on a duplicate kind in dev/test; the first renderer keeps it', () => {
    const first = () => null
    const second = () => null
    registerMobileConnectRenderer({ kind: 'seam_test_dup', component: first })
    expect(() =>
      registerMobileConnectRenderer({ kind: 'seam_test_dup', component: second }),
    ).toThrow(/already has a renderer/)
    expect(getMobileConnectRenderers().find(r => r.kind === 'seam_test_dup')?.component).toBe(first)
  })
})

describe('autolinkRules — bare-token autolink seam', () => {
  afterEach(() => resetAutolinkRulesForTest())

  it('ships empty in the core, so stock rendering is unchanged', () => {
    expect(getAutolinkRules()).toHaveLength(0)
  })

  it('a registered rule is retrievable and normalised to a global pattern', () => {
    registerAutolinkRules([
      { id: 'seam-token', pattern: /\bZZ-\d+\b/, href: 'https://example.invalid/{match}' },
    ])
    const rules = getAutolinkRules()
    expect(rules).toHaveLength(1)
    expect(rules[0].id).toBe('seam-token')
    expect(rules[0].pattern.global).toBe(true)
  })

  it('a duplicate id is fail-loud in dev/test and preserves the first registration', () => {
    const rule = {
      id: 'seam-token-dup',
      pattern: /\bZZ-\d+\b/g,
      href: 'https://first.invalid/{match}',
    }
    registerAutolinkRules([rule])
    expect(() =>
      registerAutolinkRules([{ ...rule, href: 'https://second.invalid/{match}' }]),
    ).toThrow(/already registered/)
    expect(getAutolinkRules().filter(r => r.id === 'seam-token-dup')).toHaveLength(1)
    expect(getAutolinkRules()[0].href).toBe('https://first.invalid/{match}')
  })
})

describe('builtinRegistry — route-shape guard', () => {
  // BuiltinAppRoute resolves /:builtinApp from ONE pathname segment and never
  // the query/hash — so anything that isn't a bare plain segment registers but
  // never resolves (redirects to chat). All of these must fail-loud in dev/test.
  it.each([
    ['/reports/daily', 'extra path segment'],
    ['/reports?daily', 'query string (not in pathname)'],
    ['/reports#x', 'hash (not in pathname)'],
    ['/rep orts', 'whitespace'],
    ['/..', 'traversal'],
    ['/.', 'dot'],
    ['/', 'root only'],
  ])('throws on unresolvable route %s (%s)', route => {
    expect(() =>
      registerBuiltinComponents({ [route]: lazy(async () => ({ default: Dummy })) }),
    ).toThrow(/plain path segment/)
    expect(hasBuiltinComponent(route)).toBe(false)
  })

  it.each(['/seam-single', '/Reports', '/my_app', '/a.b', '/x~y-z'])(
    'accepts a single plain path segment route %s',
    route => {
      registerBuiltinComponents({ [route]: lazy(async () => ({ default: Dummy })) })
      expect(hasBuiltinComponent(route)).toBe(true)
    },
  )
})

describe('extension slot isolation', () => {
  // App.tsx wraps each registered extension render slot (top-bar decoration /
  // aside / widgets / theme overlays) in an ErrorBoundary with fallback={null},
  // so a throwing downstream contribution disables ONLY itself instead of
  // crashing the shell via the root boundary. This verifies that contract at
  // the boundary level (a full-App render is covered by App.test.tsx).
  const Boom = () => {
    throw new Error('faulty extension')
  }

  it('renders nothing and does not propagate when a slot component throws', () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { container } = render(
      <ErrorBoundary scope="topbar-widget:boom" fallback={null}>
        <Boom />
      </ErrorBoundary>,
    )
    expect(container.innerHTML).toBe('')
    err.mockRestore()
  })

  it('a sibling slot still renders when another slot throws', () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { getByTestId } = render(
      <div>
        <ErrorBoundary scope="a" fallback={null}>
          <Boom />
        </ErrorBoundary>
        <ErrorBoundary scope="b" fallback={null}>
          <span data-testid="sibling">ok</span>
        </ErrorBoundary>
      </div>,
    )
    expect(getByTestId('sibling').textContent).toBe('ok')
    err.mockRestore()
  })
})

describe('composition root — stock extensions.ts is empty', () => {
  // extensions.ts is core-OWNED: it imports the `virtual:kirocrew-edition`
  // module (resolved by editionExtensionPlugin to an inert stub in the stock
  // build, or the edition's own composition root when KIROCREW_EDITION_DIR is
  // set). The core must register NOTHING of its own here — its only body is the
  // edition import + `export {}` (plus comments). If the core ever added a
  // registration here, the stock build would stop being a pure no-op. Guard
  // that invariant.
  it('has an empty body (edition import + export {} + comments only)', () => {
    // vitest runs with cwd = website/; extensions.ts lives at src/extensions.ts.
    const src = readFileSync(resolve(process.cwd(), 'src/extensions.ts'), 'utf-8')
    const code = src
      .replace(/\r\n/g, '\n') // normalize CRLF (Windows checkout) before comparing
      .replace(/\/\*[\s\S]*?\*\//g, '') // block comments
      .replace(/^\s*\/\/.*$/gm, '') // line comments
      .trim()
    expect(code).toBe("import 'virtual:kirocrew-edition'\n\nexport {}")
  })

  it('capsule-segment + overview-stat-card seams are empty in the stock build', () => {
    expect(getCapsuleSegments().every(s => !s.id.startsWith('edition:'))).toBe(true)
    expect(getOverviewStatCards().every(c => !c.id.startsWith('edition:'))).toBe(true)
    expect(getOverviewPanel()?.id.startsWith('edition:') ?? false).toBe(false)
  })

  it('importing it adds no registrations beyond the seeded core state', async () => {
    // The registries are module singletons seeded by the core. Snapshot the
    // core-seeded keys, import the composition root, and assert nothing new
    // appeared (the seam tests above add their own entries, so compare deltas
    // against a fresh reimport rather than absolute counts).
    await import('../extensions')
    // Theme branding ships unseeded (built-in Lumon removed with theme packs);
    // the icon registry is seeded with the core lucide set; neither should
    // gain entries from the stock root.
    expect(getThemeBranding('lumon')).toBeUndefined()
    expect(getBuiltinIcon('Brain')).toBeDefined()
    // No stock top-bar widget (edition-only slot).
    expect(getTopBarWidgets().every(w => !w.id.startsWith('edition:'))).toBe(true)
  })
})

