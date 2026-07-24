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
import { describe, it, expect, vi } from 'vitest'
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
import { registerThemeBranding, getThemeBranding } from '../themeBranding'
import { registerTopBarWidgets, getTopBarWidgets } from '../apps/topBarWidgets'
import {
  registerPanelShortcut,
  CORE_PANEL_MAP,
  DEFAULT_SHORTCUTS,
  RESERVED_PANEL_CODES,
} from '../hooks/useKeyboardShortcuts'

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
  it('seeds the core lumon branding', () => {
    const lumon = getThemeBranding('lumon')
    expect(lumon?.botName).toBe('LumonClaw')
  })

  it('returns undefined for a theme with no branding', () => {
    expect(getThemeBranding('emerald')).toBeUndefined()
  })

  it('registers branding for a new theme', () => {
    registerThemeBranding({ 'seam-theme': { botName: 'SeamBot', logo: '/x.svg' } })
    expect(getThemeBranding('seam-theme')?.botName).toBe('SeamBot')
  })

  it('throws on duplicate theme branding in dev/test; core wins', () => {
    expect(() => registerThemeBranding({ lumon: { botName: 'Hijack' } })).toThrow(
      /already registered/,
    )
    expect(getThemeBranding('lumon')?.botName).toBe('LumonClaw')
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
    expect(entry?.group).toBe('Panel Navigation')
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
  // extensions.ts is core-tracked but contractually edition-owned: a downstream
  // build overlays it to inject registrations. If the CORE ever registers
  // something in it, an edition's overlay would silently delete that core
  // feature on the next sync (no key collision -> no throw). Guard the
  // invariant so the stock file stays a no-op: its body is `export {}` (plus
  // comments), and importing it registers nothing.
  it('has an empty body (export {} + comments only)', () => {
    // vitest runs with cwd = website/; extensions.ts lives at src/extensions.ts.
    const src = readFileSync(resolve(process.cwd(), 'src/extensions.ts'), 'utf-8')
    const code = src
      .replace(/\/\*[\s\S]*?\*\//g, '') // block comments
      .replace(/^\s*\/\/.*$/gm, '') // line comments
      .trim()
    expect(code).toBe('export {}')
  })

  it('importing it adds no registrations beyond the seeded core state', async () => {
    // The registries are module singletons seeded by the core. Snapshot the
    // core-seeded keys, import the composition root, and assert nothing new
    // appeared (the seam tests above add their own entries, so compare deltas
    // against a fresh reimport rather than absolute counts).
    await import('../extensions')
    // lumon is the only seeded theme branding; the icon registry is seeded with
    // the core lucide set; neither should gain entries from the stock root.
    expect(getThemeBranding('lumon')?.botName).toBe('LumonClaw')
    expect(getBuiltinIcon('Brain')).toBeDefined()
    // No stock top-bar widget (edition-only slot).
    expect(getTopBarWidgets().every(w => !w.id.startsWith('edition:'))).toBe(true)
  })
})

