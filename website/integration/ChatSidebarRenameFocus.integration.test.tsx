/**
 * Regression for the board-card rename bug: right-click a card → Rename, and
 * the rename box appeared then instantly reverted, so you could never type in
 * it. Root cause: the rename menus are Radix (ContextMenu/DropdownMenu). On
 * close Radix restores focus to its trigger (the card) via onCloseAutoFocus,
 * and that restore fires AFTER the rename input mounts. It blurs the input,
 * whose onBlur cancels the edit (setRenamingSlot(null)) — so the field flickers
 * open and vanishes.
 *
 * History: the first form of this bug (Mesh-2628) only cost the caret — the
 * input stayed, you just had to click it before typing — and was fixed with a
 * useEffect that re-grabs focus + selects on the next rAF. The menu-unification
 * refactor (CR-286161833) changed the close/focus-restore timing so the restore
 * now lands after that rAF, escalating "caret not placed" into "edit cancelled".
 *
 * Fix: suppress Radix's trigger-focus-restore on the rename path only
 * (onCloseAutoFocus preventDefault, armed by the Rename item), so the restore
 * never blurs the input; the existing rAF then focuses + selects it.
 *
 * What this test guards: the edit SURVIVES the menu's close-restore — the input
 * mounts and stays mounted (not instantly cancelled) and typing commits. That
 * is the user-visible regression ("box flickers open and reverts") and jsdom
 * can verify it reliably. The mock reproduces Radix's close-restore so a broken
 * guard would blur the input → onBlur cancels → input unmounts → this test goes
 * red.
 *
 * What this test does NOT assert: that document.activeElement lands on the input
 * or that the text is selected. jsdom drops activeElement to <body> during the
 * React commit + Radix portal teardown (no focusout fires), so caret/selection
 * are not observable here. Those are verified by manual browser smoke, which is
 * the real source of truth for focus placement.
 *
 * Lives in the integration suite (MSW + renderWithProviders) because that
 * harness opens Radix menus reliably; the inline-mock src/test harness does not.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor, cleanup } from '@testing-library/react'
import { http, HttpResponse } from 'msw'

// Use the stateful Radix mocks. This is load-bearing for THIS test: the mock's
// Content faithfully reproduces Radix's close-time focus-restore (blur the
// active element, focus the trigger, one frame after the consumer's rAF), which
// is the exact behaviour the rename guard defends against. Against the real
// library the restore is a jsdom no-op (it focuses a non-focusable <div>
// trigger), so a broken rename path would pass — which is how CR-286161833
// slipped through. The right-click path uses ContextMenu; the ⋯ path uses
// DropdownMenu, so mock both.
vi.mock('@radix-ui/react-context-menu', () => import('./__mocks__/@radix-ui/react-context-menu'))
vi.mock('@radix-ui/react-dropdown-menu', () => import('./__mocks__/@radix-ui/react-dropdown-menu'))

import ChatSidebar from '../src/pages/ChatSidebar'
import { renderWithProviders } from './helpers'
import { server } from './mocks/server'
import { __resetAuthRecoveryStateForTests } from '../src/api/client'

// Render framer-motion elements as plain DOM (jsdom can't run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: any, ref: any) => {
      const clean: any = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: any) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../src/components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../src/pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const SLOT_KEY = 'slot-1'
const TITLE = 'My Session Title'
const slots = [{
  key: SLOT_KEY, title: TITLE, running: false, agent: 'kirocrew',
  created: '2026-04-08T01:00:00Z', last_ts: '2026-04-08T02:00:00Z', folder_id: '', tags: [],
}]
const props = {
  slots, activeSlot: SLOT_KEY, unreadSlots: [] as string[],
  history: [], historyHasMore: false, defaultAgent: 'kirocrew',
  installedAgents: [{ name: 'kirocrew', source: 'builtin' }],
}

beforeEach(() => {
  localStorage.clear()
  server.use(
    // Stub auth so api/client.ts never shows its session-expired banner, whose
    // token input grabs focus on a rAF and steals it from the rename box under
    // test (jsdom-only focus theft; see the same guard in ChatSidebar.integration).
    http.post('/api/auth/refresh', () => HttpResponse.json({ ok: true })),
    http.get('/api/auth/me', () => HttpResponse.json({ ok: true })),
    http.get('/api/chat/tags', () => HttpResponse.json([])),
    http.get('/api/chat/tag-columns', () => HttpResponse.json([])),
    http.get('/api/chat/folders', () => HttpResponse.json([])),
    http.get('/api/chat/slots', () => HttpResponse.json(slots)),
  )
  __resetAuthRecoveryStateForTests()
  ;(document.activeElement as HTMLElement | null)?.blur?.()
})

// Unmount + drop focus after each case so an open rename input (and whatever
// holds document.activeElement) doesn't survive into the next test FILE — jsdom
// shares one document across files, and focus-dependent tests elsewhere (e.g.
// the folder Escape-to-cancel) key off the active element. Scoped here rather
// than globally: a suite-wide afterEach(cleanup) unmounts other files' trees
// mid-async (HooksPage's in-flight refresh/animation) and breaks them.
afterEach(() => {
  cleanup()
  ;(document.activeElement as HTMLElement | null)?.blur?.()
})

function rowFor(container: HTMLElement): HTMLElement {
  const row = container.querySelector(`[data-slot-key="${SLOT_KEY}"] .session-row`)
  expect(row).toBeTruthy()
  return row as HTMLElement
}

function renameInput(): HTMLInputElement {
  const input = Array.from(document.querySelectorAll('input'))
    .find(i => (i as HTMLInputElement).value === TITLE) as HTMLInputElement
  expect(input).toBeTruthy()
  return input
}

function renameInputWithValue(value: string): HTMLInputElement | undefined {
  return Array.from(document.querySelectorAll('input'))
    .find(i => (i as HTMLInputElement).value === value) as HTMLInputElement | undefined
}

// Flush enough animation frames for both the component's single-rAF focus and
// the mock's double-rAF close-restore to have run (see the mock header for why
// the restore is a double rAF). Three frames leaves a margin.
async function flushFrames(n = 3) {
  for (let i = 0; i < n; i++) {
    await act(async () => { await new Promise(r => requestAnimationFrame(() => r(null))) })
  }
}

describe('board card rename focus', () => {
  it('right-click → Rename keeps the input open through the menu close (edit not cancelled)', async () => {
    const { container } = renderWithProviders(<ChatSidebar {...(props as any)} />)

    fireEvent.contextMenu(rowFor(container))
    const rename = await screen.findByRole('menuitem', { name: /Rename/ })
    await act(async () => { fireEvent.click(rename) })
    // Let the menu's close-focus-restore run (double rAF in the mock).
    await flushFrames()

    // The edit SURVIVES the menu's close-focus-restore. On broken code (no
    // onCloseAutoFocus guard) the restore blurs the just-mounted input, its
    // onBlur cancels rename, and the field unmounts. Its continued presence is
    // the regression guard. (Caret placement / text selection are not asserted:
    // jsdom drops activeElement to <body> across the Radix portal teardown — see
    // the file header — so those are browser-smoke verified, not here.)
    const input = renameInput()
    expect(input).toBeInTheDocument()

    // And the box is usable: typing replaces the title and it commits.
    await act(async () => { fireEvent.change(input, { target: { value: 'Renamed' } }) })
    await waitFor(() => expect(renameInputWithValue('Renamed')).toBeTruthy())
  })
})
