/**
 * AgentSelector popup layering — the #6358 regression, unit-testable half.
 *
 * The Schedule job form (JobForm — the one dialog-hosted consumer; Channels,
 * Projects and Webhooks render this selector on plain pages and stay
 * non-modal) renders AgentSelector inside a Radix MODAL dialog. The old implementation portaled
 * its dropdown to `document.body` with a bare `createPortal`, OUTSIDE the
 * dialog's layer stack: react-remove-scroll's `pointer-events: none` on the
 * body swallowed clicks on the options, and the dialog's FocusScope kept the
 * filter input from ever taking focus (dead keyboard). Rebuilt on Radix
 * Popover, the popup joins the dialog's focus/dismiss layer stack.
 *
 * The interaction itself (open + select INSIDE a modal dialog) cannot be
 * exercised faithfully under happy-dom: Radix commits its layer interplay via
 * `ReactDOM.flushSync` dispatches that land inside Testing Library's event
 * batch (see the header of scripts/verify-crews-dialog-select.mjs, which
 * exists for exactly this reason for Radix Select). The end-to-end proof for
 * this component lives in scripts/verify-agent-selector-dialog.mjs — a real
 * browser against the built SPA. What is pinned HERE is the structure that
 * makes the fix work, so a regression back to a bare body portal fails fast
 * in unit tests too:
 *
 *  1. the popup renders through Radix's popper layer (not a bare portal), and
 *  2. the component no longer reaches for `createPortal` at all.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { render, screen, fireEvent, act } from '@testing-library/react'
import AgentSelector from '../components/AgentSelector'
import type { KiroCrewAgent } from '../components/AgentSelector'

const agents: KiroCrewAgent[] = [
  { name: 'coding', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', description: 'Coding agent', source: 'kirocrew' },
  { name: 'oncall', kiro_agent: 'oncall-agent', workspace: 'oncall', memory_store: 'oncall-kb', description: 'Oncall agent', source: 'kirocrew' },
]

describe('AgentSelector popup layering (#6358)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    // The touch test ASSIGNS window.matchMedia; restoreAllMocks does not
    // undo plain assignments, and a leaked coarse-pointer mock silently
    // flips later tests into touch mode.
    delete (window as unknown as { matchMedia?: typeof window.matchMedia }).matchMedia
  })

  it('renders the listbox inside a Radix popper layer, not a bare body portal', () => {
    render(<AgentSelector agents={agents} defaultAgent="coding" value="coding" onChange={() => {}} />)
    fireEvent.click(screen.getByLabelText('Switch agent'))

    const listbox = screen.getByRole('listbox')
    // Radix Popover mounts its content inside a popper wrapper it owns. That
    // wrapper is what enrols the popup in the surrounding dialog's
    // focus/dismiss layer stack — the property the old `createPortal(...,
    // document.body)` implementation lacked, which is what made options
    // unclickable (body pointer-events: none) and the keyboard dead
    // (FocusScope reclaim) inside modal dialogs.
    expect(listbox.closest('[data-radix-popper-content-wrapper]')).not.toBeNull()
  })

  it('keeps the filter input inside the same popper layer as the listbox', () => {
    render(<AgentSelector agents={agents} defaultAgent="coding" value="coding" onChange={() => {}} />)
    fireEvent.click(screen.getByLabelText('Switch agent'))

    const input = screen.getByPlaceholderText('Type to filter…')
    const listbox = screen.getByRole('listbox')
    expect(input.closest('[data-radix-popper-content-wrapper]'))
      .toBe(listbox.closest('[data-radix-popper-content-wrapper]'))
  })

  it('does not use a bare createPortal for the popup', () => {
    // Source-level pin, same style as filterInputFont.test.tsx: a future
    // refactor that swaps the Radix Popover back for `createPortal(...,
    // document.body)` reintroduces the modal-dialog click-through silently —
    // no unit test can catch the interaction itself under happy-dom, so the
    // call is the cheapest reliable tripwire. Only the CALL is matched: the
    // component's doc comment legitimately names createPortal, and react-dom
    // has legitimate other imports (flushSync).
    const src = readFileSync(join(__dirname, '..', 'components', 'AgentSelector.tsx'), 'utf8')
    expect(src).not.toContain('createPortal(')
    expect(src).toContain("from './ui/popover'")
  })

  it('touch open parks focus on the popup container and ArrowDown reaches the options', async () => {
    // On touch the filter input must NOT autofocus (it pops the on-screen
    // keyboard), but focus has to enter the popup: left on the trigger, a
    // hardware keyboard on a touch device never reaches onListKeyDown, and in
    // modal mode the trigger is aria-hidden by hideOthers(). The container
    // needs an explicit tabIndex for its focus() to be more than a no-op.
    window.matchMedia = vi.fn().mockImplementation((q: string) => ({
      matches: /pointer:\s*coarse|hover:\s*none/.test(q), media: q, onchange: null,
      addListener: vi.fn(), removeListener: vi.fn(),
      addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
    }))
    render(<AgentSelector agents={agents} defaultAgent="coding" value="coding" onChange={() => {}} />)
    fireEvent.click(screen.getByLabelText('Switch agent'))
    await act(async () => { await new Promise(r => setTimeout(r, 5)) })

    const listbox = screen.getByRole('listbox')
    const content = listbox.closest('[data-radix-popper-content-wrapper]')!
      .querySelector('[tabindex="-1"]') as HTMLElement
    expect(content).toBeTruthy()
    expect(content.contains(document.activeElement)).toBe(true)

    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown' })
    expect(document.activeElement?.getAttribute('role')).toBe('option')
  })

  it('a composition-cancel Escape keeps the popup open and the IME action unprevented', async () => {
    // The window-capture handler must stopPropagation (so no dismissal layer
    // sees the key) WITHOUT preventDefault (so the IME keeps its native
    // cancel). Either half regressing breaks a CJK user typing in the filter.
    render(<AgentSelector agents={agents} defaultAgent="coding" value="coding" onChange={() => {}} />)
    fireEvent.click(screen.getByLabelText('Switch agent'))
    const input = screen.getByPlaceholderText('Type to filter…')

    const composingEsc = new KeyboardEvent('keydown', {
      key: 'Escape', bubbles: true, cancelable: true, composed: true, isComposing: true,
    })
    input.dispatchEvent(composingEsc)
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    expect(composingEsc.defaultPrevented).toBe(false)

    // WebKit ordering: the cancel keydown arrives AFTER compositionend with
    // isComposing already false. The tracked latch (live for 50ms past
    // compositionend) must still decline it — a raw-flag guard reads this as
    // a plain Escape and closes the popup mid-composition-cancel.
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input)
    const postCompositionEsc = new KeyboardEvent('keydown', {
      key: 'Escape', bubbles: true, cancelable: true, composed: true,
    })
    input.dispatchEvent(postCompositionEsc)
    expect(screen.getByRole('listbox')).toBeInTheDocument()

    // Once the post-composition window lapses, a plain Escape closes it —
    // the latch is a window, not a permanent state.
    await act(async () => { await new Promise(r => setTimeout(r, 60)) })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('clears the filter on every close path, not only Radix-initiated ones', async () => {
    // Select / Escape / Tab close through the component's own setOpen(false),
    // which does NOT fire Radix's onOpenChange — a reset living only there
    // leaks the typed filter into the next open, showing a narrowed (or empty
    // "No matches") list the user did not filter.
    const onChange = vi.fn()
    render(<AgentSelector agents={agents} defaultAgent="coding" value="coding" onChange={onChange} />)
    fireEvent.click(screen.getByLabelText('Switch agent'))
    const input = screen.getByPlaceholderText('Type to filter…')
    fireEvent.change(input, { target: { value: 'onc' } })
    // Enter on the sole match: an owned close path (never routes onOpenChange).
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('oncall')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('Switch agent'))
    expect((screen.getByPlaceholderText('Type to filter…') as HTMLInputElement).value).toBe('')
    expect(screen.getAllByRole('option')).toHaveLength(agents.length)
  })

  it('resolves exactly one copy of each Radix layer-state internal', () => {
    // The fix only holds while every Radix package shares ONE module instance
    // of the layer-state internals — separate copies mean separate layer
    // stacks, which is the click-through and the both-surfaces-close-on-
    // Escape defect itself. The internals are pinned as exact direct
    // dependencies so everything dedupes onto the root; this guard fails when
    // a future bump of ANY host (dialog, popover, select, the menu family)
    // re-nests a different internal version under itself — a direct dep
    // cannot prevent that — so the regression is caught at test time instead
    // of shipping. If this test reds after a host bump, re-pin the three
    // internals to the versions the bumped host requires (and bump the other
    // hosts onto the same release train).
    const lock = JSON.parse(readFileSync(join(__dirname, '..', '..', 'package-lock.json'), 'utf8'))
    const internals = [
      '@radix-ui/react-dismissable-layer',
      '@radix-ui/react-focus-scope',
      '@radix-ui/react-focus-guards',
    ]
    // The menu family (react-menu under dropdown-menu / context-menu) is the
    // ONE deliberate exception: it stays on its own older internals because
    // forcing it onto the shared versions changes Radix submenu dismiss
    // behaviour (pinned by ChatSidebar.recencyUnit.test.tsx). Menus already
    // lived on a separate layer stack on main; this preserves, not worsens,
    // that. Everything else must dedupe onto the root.
    const nested = Object.keys(lock.packages ?? {}).filter(p =>
      internals.some(i => p.endsWith(i) && p !== `node_modules/${i}`)
      && !p.includes('@radix-ui/react-menu/'))
    expect(nested).toEqual([])
    for (const i of internals) {
      expect(lock.packages?.[`node_modules/${i}`], `${i} must resolve at the root`).toBeTruthy()
    }
  })
})
