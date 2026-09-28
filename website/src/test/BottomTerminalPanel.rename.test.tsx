import { useEffect, useRef } from 'react'
import { act, fireEvent, renderHook, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from './helpers'
import { TerminalTabsView } from '../components/BottomTerminalPanel'
import {
  __resetBottomTerminal, addTab, removeTab, renameTab, setActiveTab, setTabsOrder, useBottomTerminal,
  closeBottomTerminal, openBottomTerminal, MAX_TERMINAL_NAME_LENGTH, bottomTerminalPrefsSnapshot,
} from '../hooks/useBottomTerminal'

const terminal = vi.hoisted(() => ({ title: 'workspace', autofocus: false, dispose: vi.fn(), del: vi.fn() }))
vi.mock('../components/CliPanel', () => ({
  default: function MockCliPanel({ sessionId, visible }: { sessionId: string; visible: boolean }) {
    const ref = useRef<HTMLTextAreaElement>(null)
    useEffect(() => { if (visible && terminal.autofocus) ref.current?.focus() }, [visible])
    return <textarea ref={ref} tabIndex={-1} aria-label={`Terminal input ${sessionId}`} data-testid={`cli-${sessionId}`} />
  },
  disposeTerminalSession: terminal.dispose,
  useDeleteTerminalSession: () => ({ mutate: terminal.del }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalTitle: () => terminal.title,
  disposeTerminalConnection: vi.fn(),
}))
vi.mock('../utils/terminalPopout', () => ({
  openPopout: vi.fn(), isPopoutOpen: vi.fn(() => true), focusPopout: vi.fn(),
  bringBack: vi.fn(), returnSelfToMain: vi.fn(),
}))

beforeEach(() => { __resetBottomTerminal(); terminal.title = 'workspace'; terminal.autofocus = false; vi.clearAllMocks() })
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); __resetBottomTerminal() })

function mount(variant: 'dock' | 'popout' = 'dock') {
  const id = addTab('/project')!
  const view = renderWithProviders(<TerminalTabsView variant={variant} />)
  return { id, ...view }
}

function editor() { return screen.getByRole('textbox', { name: 'Terminal name' }) }
function hint() { return screen.queryByTestId('terminal-rename-hint') }
const EDIT_HINT = 'Enter to save, Escape to cancel. Leave empty to use the automatic name.'
function snapshot() { return renderHook(() => useBottomTerminal()).result.current }

describe('terminal pointer selection', () => {
  function inactive() {
    terminal.autofocus = true
    const view = mount()
    let activeId = ''
    act(() => { activeId = addTab('/other')! })
    return { ...view, tab: screen.getAllByRole('tab')[0], activeId }
  }

  it('selects and focuses a single pointer click immediately, accepting the first typed character', async () => {
    const { id, tab } = inactive()
    fireEvent.click(tab, { detail: 1 })
    expect(snapshot().activeId).toBe(id)
    expect(tab).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId(`cli-${id}`)).toBeVisible()
    expect(screen.getByTestId(`cli-${id}`)).toHaveFocus()
    await userEvent.keyboard('pwd')
    expect(screen.getByTestId(`cli-${id}`)).toHaveValue('pwd')
  })

  it.each(['Enter', ' ', 'assistive click'])('selects and focuses an inactive terminal immediately via %s', (key) => {
    const { id, tab } = inactive()
    // Independently assert keyboard/assistive activation from an inactive tab.
    if (key === 'assistive click') fireEvent.click(tab, { detail: 0 })
    else fireEvent.keyDown(tab, { key })
    expect(snapshot().activeId).toBe(id)
    expect(screen.getByTestId(`cli-${id}`)).toHaveFocus()
  })

  it('restores the prior terminal before a double-click editor mounts, with no later selection', () => {
    const { id, tab, activeId } = inactive()
    const shell = screen.getByTestId(`cli-${id}`)
    const priorShell = screen.getByTestId(`cli-${activeId}`)
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    fireEvent.click(tab, { detail: 1 })
    expect(snapshot().activeId).toBe(id)
    expect(shell).toBeVisible()
    expect(shell).toHaveFocus()
    fireEvent.pointerDown(tab, { button: 0, pointerType: 'mouse', pointerId: 1 })
    fireEvent.pointerUp(tab, { button: 0, pointerType: 'mouse', pointerId: 1 })
    fireEvent.click(tab, { detail: 2 })
    const focusOrder: { target: EventTarget | null; editorMounted: boolean; display: string; activeId: string }[] = []
    const observeFocus = (event: FocusEvent) => {
      focusOrder.push({
        target: event.target,
        editorMounted: screen.queryByRole('textbox', { name: 'Terminal name' }) !== null,
        display: shell.parentElement!.style.display,
        activeId: JSON.parse(localStorage.getItem('mc-bottom-terminal')!).activeId,
      })
    }
    document.addEventListener('focusin', observeFocus)
    try {
      fireEvent.doubleClick(tab, { detail: 2 })
      expect(editor()).toHaveFocus()
      expect(focusOrder).toEqual([
        { target: priorShell, editorMounted: false, display: 'none', activeId },
        { target: editor(), editorMounted: true, display: 'none', activeId },
      ])
    } finally {
      document.removeEventListener('focusin', observeFocus)
    }
    expect(tab).toHaveAttribute('aria-selected', 'false')
    act(() => { vi.runOnlyPendingTimers() })
    expect(editor()).toHaveFocus()
    expect(hint()).toBeVisible()
    expect(snapshot().activeId).toBe(activeId)
    fireEvent.change(editor(), { target: { value: 'Background' } })
    fireEvent.keyDown(editor(), { key: 'Enter' })
    act(() => { vi.runOnlyPendingTimers() })
    expect(hint()).toBeNull()
    const saved = JSON.parse(localStorage.getItem('mc-bottom-terminal')!)
    expect(saved.activeId).toBe(activeId)
    expect(snapshot().tabs.find(item => item.id === id)?.name).toBe('Background')
    expect(localStorage.getItem(`mc-terminal-name:${id}`)).toBe('Background')
    expect(snapshot().activeId).toBe(activeId)
    expect(screen.getByTestId(`cli-${id}`)).toBe(shell)
    expect(shell).not.toBeVisible()
    expect(tab).toHaveFocus()
    expect(terminal.dispose).not.toHaveBeenCalled()
    expect(terminal.del).not.toHaveBeenCalled()
  })

  it.each(['new first click', 'Enter', ' ', 'assistive click', 'pointer cancel', 'native drag', 'reorder', 'F2', 'context menu', 'another selection and back'])(
    'does not restore a stale prior selection after %s', (action) => {
      const { id, tab, activeId } = inactive()
      fireEvent.click(tab, { detail: 1 })
      if (action === 'new first click') fireEvent.click(tab, { detail: 1 })
      else if (action === 'assistive click') fireEvent.click(tab, { detail: 0 })
      else if (action === 'pointer cancel') fireEvent.pointerCancel(tab)
      else if (action === 'native drag') fireEvent.dragStart(tab)
      else if (action === 'reorder') {
        const tabs = snapshot().tabs
        act(() => setTabsOrder([...tabs].reverse()))
      } else if (action === 'context menu') {
        fireEvent.contextMenu(tab)
        fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' })
      } else if (action === 'another selection and back') {
        act(() => setActiveTab(activeId))
        act(() => setActiveTab(id))
      } else {
        fireEvent.keyDown(tab, { key: action })
        if (action === 'F2') fireEvent.keyDown(editor(), { key: 'Escape' })
      }
      fireEvent.click(tab, { detail: 2 })
      fireEvent.doubleClick(tab, { detail: 2 })
      expect(editor()).toHaveFocus()
      expect(snapshot().activeId).toBe(id)
    },
  )

  it.each(['removed prior tab', 'another active tab'])('does not restore after %s', (change) => {
    const { id, tab, activeId } = inactive()
    fireEvent.click(tab, { detail: 1 })
    let expectedId = id
    act(() => {
      if (change === 'removed prior tab') removeTab(activeId)
      else expectedId = addTab('/next')!
    })
    fireEvent.click(tab, { detail: 2 })
    fireEvent.doubleClick(tab, { detail: 2 })
    expect(editor()).toHaveFocus()
    fireEvent.keyDown(editor(), { key: 'Escape' })
    expect(snapshot().activeId).toBe(expectedId)
  })

  it.each(['button', 'middle click'])('closes the selected tab via %s without restoring it', (method) => {
    const { id, tab, activeId } = inactive()
    fireEvent.click(tab, { detail: 1 })
    if (method === 'button') fireEvent.click(tab.querySelector('button')!)
    else fireEvent(tab, new MouseEvent('auxclick', { bubbles: true, button: 1 }))
    expect(snapshot().activeId).toBe(activeId)
    expect(snapshot().tabs.some(t => t.id === id)).toBe(false)
    expect(terminal.del).toHaveBeenCalledWith(id)
  })
})

describe('terminal inline rename', () => {
  it.each(['dock', 'popout'] as const)('renames in %s without replacing the shell', async (variant) => {
    const { id } = mount(variant)
    const shell = screen.getByTestId(`cli-${id}`)
    await userEvent.dblClick(screen.getByRole('tab', { name: 'workspace' }))
    expect(editor()).toHaveFocus()
    await userEvent.clear(editor())
    await userEvent.type(editor(), 'Build logs{Enter}')
    expect(screen.getByRole('tab', { name: 'Build logs' })).toHaveFocus()
    expect(screen.queryByRole('textbox', { name: 'Terminal name' })).not.toBeInTheDocument()
    expect(screen.getByTestId(`cli-${id}`)).toBe(shell)
    expect(snapshot().tabs).toEqual([{ id, cwd: '/project', name: 'Build logs' }])
    expect(terminal.dispose).not.toHaveBeenCalled()
    expect(terminal.del).not.toHaveBeenCalled()
  })

  it('F2 edits, Escape cancels without blur saving, and focus returns to the tab', async () => {
    mount()
    const tab = screen.getByRole('tab')
    tab.focus()
    await userEvent.keyboard('{F2}')
    await userEvent.clear(editor())
    await userEvent.type(editor(), 'Discard me{Escape}')
    expect(tab).toHaveFocus()
    expect(tab).toHaveAccessibleName('workspace')
    expect(snapshot().tabs[0].name).toBeUndefined()
  })

  it.each(['F2', 'context menu'])('renames an inactive tab via %s without activating its terminal', async (method) => {
    const { id } = mount()
    let activeId: string
    act(() => { activeId = addTab('/other')! })
    const inactiveTab = screen.getAllByRole('tab')[0]
    expect(inactiveTab).toHaveAttribute('aria-selected', 'false')
    if (method === 'F2') {
      inactiveTab.focus()
      await userEvent.keyboard('{F2}')
    } else {
      fireEvent.contextMenu(inactiveTab)
      await userEvent.click(await screen.findByRole('menuitem', { name: 'Rename', exact: true }))
    }
    await waitFor(() => expect(editor()).toHaveFocus())
    expect(snapshot().activeId).toBe(activeId!)
    await userEvent.clear(editor())
    await userEvent.type(editor(), 'Background logs{Enter}')
    expect(snapshot().tabs.find(tab => tab.id === id)?.name).toBe('Background logs')
    expect(snapshot().activeId).toBe(activeId!)
    expect(inactiveTab).toHaveFocus()
  })

  it('opens the editor from the context menu without restoring focus over it', async () => {
    mount()
    fireEvent.contextMenu(screen.getByRole('tab'))
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Rename', exact: true }))
    await waitFor(() => expect(editor()).toHaveFocus())
    await userEvent.clear(editor())
    await userEvent.type(editor(), 'Review{Enter}')
    expect(screen.getByRole('tab')).toHaveAccessibleName('Review')
  })

  it('shows F2 beside Rename in the context menu without adding it to the item name', async () => {
    mount()
    // The declaration on the chip itself stays, so F2 is announced there too.
    expect(screen.getByRole('tab')).toHaveAttribute('aria-keyshortcuts', 'F2')
    fireEvent.contextMenu(screen.getByRole('tab'))
    // Fetched BY ITS EXACT NAME: the visible badge is decoration and the
    // attribute is the declaration, so a badge that leaked into the name
    // ("Rename F2") makes this query itself the regression.
    const rename = await screen.findByRole('menuitem', { name: 'Rename', exact: true })
    expect(rename).toHaveAttribute('aria-keyshortcuts', 'F2')
    const badge = within(rename).getByTestId('terminal-rename-shortcut')
    expect(badge).toBeVisible()
    expect(badge).toHaveTextContent('F2')
    expect(badge).toHaveAttribute('aria-hidden', 'true')
    expect(badge).toHaveAttribute('data-i18n-opaque')
    expect(rename).toHaveAccessibleName('Rename')
  })

  it('keeps a custom name across live title changes and can restore automatic naming', async () => {
    const { id, rerender } = mount()
    act(() => renameTab(id, 'My shell'))
    terminal.title = 'npm'
    rerender(<TerminalTabsView variant="dock" />)
    expect(screen.getByRole('tab')).toHaveAccessibleName('My shell')
    fireEvent.contextMenu(screen.getByRole('tab'))
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Use automatic name' }))
    expect(screen.getByRole('tab')).toHaveAccessibleName('npm')
    expect(snapshot().tabs[0].name).toBeUndefined()
  })

  it('saving a blank name restores the automatic title', async () => {
    const { id } = mount()
    act(() => renameTab(id, 'Custom'))
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'F2' })
    await userEvent.clear(editor())
    await userEvent.type(editor(), '   {Enter}')
    expect(screen.getByRole('tab')).toHaveAccessibleName('workspace')
  })

  it('saves on blur without stealing focus from the next control', async () => {
    mount()
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'F2' })
    await userEvent.clear(editor())
    await userEvent.type(editor(), 'New label')
    const next = screen.getByRole('button', { name: 'New terminal' })
    await userEvent.click(next)
    expect(snapshot().tabs[0].name).toBe('New label')
    expect(next).toHaveFocus()
  })

  it('does not submit or cancel during IME composition', () => {
    mount()
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'F2' })
    fireEvent.change(editor(), { target: { value: '端末' } })
    fireEvent.keyDown(editor(), { key: 'Enter', isComposing: true })
    expect(editor()).toHaveValue('端末')
    fireEvent.keyDown(editor(), { key: 'Escape', keyCode: 229 })
    expect(editor()).toHaveValue('端末')
    fireEvent.keyDown(editor(), { key: 'Enter' })
    expect(screen.getByRole('tab')).toHaveAccessibleName('端末')
  })

  it('declines the committing Enter that lands just after compositionend', async () => {
    // WebKit reports isComposing=false on the keydown that commits a candidate;
    // only the shared latch (useImeGuard) knows that Enter still belongs to the IME.
    mount()
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'F2' })
    fireEvent.compositionStart(editor())
    fireEvent.change(editor(), { target: { value: '端末' } })
    fireEvent.compositionEnd(editor())
    fireEvent.keyDown(editor(), { key: 'Enter' })
    expect(editor()).toHaveValue('端末')
    expect(snapshot().tabs[0].name).toBeUndefined()
    await new Promise(r => setTimeout(r, 80))
    fireEvent.keyDown(editor(), { key: 'Enter' })
    expect(screen.getByRole('tab')).toHaveAccessibleName('端末')
  })

  it('withdraws the close control while the editor is open', async () => {
    const { id } = mount()
    expect(screen.getByRole('button', { name: 'Close terminal' })).toBeInTheDocument()
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'F2' })
    expect(screen.queryByRole('button', { name: 'Close terminal' })).not.toBeInTheDocument()
    await userEvent.clear(editor())
    await userEvent.type(editor(), 'Kept{Enter}')
    expect(screen.getByRole('button', { name: 'Close terminal' })).toBeInTheDocument()
    expect(screen.getByTestId(`cli-${id}`)).toBeInTheDocument()
    expect(terminal.del).not.toHaveBeenCalled()
  })

  it('carries no hover tooltip and does not reveal close on keyboard focus', () => {
    mount()
    act(() => { addTab('/other') })
    const inactiveTab = screen.getAllByRole('tab')[0]
    expect(inactiveTab).not.toHaveAttribute('title')
    const close = inactiveTab.querySelector('button')!
    expect(close.className).not.toMatch(/focus-within/)
    expect(close.className).toMatch(/opacity-0/)
  })

  it('does not leak editing keys or pointer-down into global shortcuts or reorder', () => {
    mount()
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'F2' })
    const key = vi.fn()
    const pointer = vi.fn()
    document.addEventListener('keydown', key)
    document.addEventListener('pointerdown', pointer)
    try {
      fireEvent.keyDown(editor(), { key: ' ' })
      fireEvent.pointerDown(editor())
      expect(key).not.toHaveBeenCalled()
      expect(pointer).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', key)
      document.removeEventListener('pointerdown', pointer)
    }
  })
})

describe('terminal editing treatment', () => {
  it.each(['dock', 'popout'] as const)('shows the save/cancel helper while editing in %s and describes the editor by it', async (variant) => {
    mount(variant)
    expect(hint()).toBeNull()
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'F2' })
    expect(hint()).toBeVisible()
    expect(hint()).toHaveTextContent(EDIT_HINT)
    expect(editor()).toHaveAccessibleDescription(EDIT_HINT)
    // Visible helper, not a hover tooltip nobody sees while typing.
    expect(editor()).not.toHaveAttribute('title')
    expect(editor()).toHaveFocus()
    // Outside the horizontal scroller, which would clip anything hung below a chip.
    expect(screen.getByRole('tablist')).not.toContainElement(hint())
    await userEvent.clear(editor())
    await userEvent.type(editor(), 'Named{Enter}')
    expect(hint()).toBeNull()
  })

  it.each(['Escape', 'blur'])('withdraws the helper after %s', async (how) => {
    mount()
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'F2' })
    expect(hint()).toBeVisible()
    if (how === 'Escape') fireEvent.keyDown(editor(), { key: 'Escape' })
    else await userEvent.click(screen.getByRole('button', { name: 'New terminal' }))
    expect(hint()).toBeNull()
  })

  it('withdraws the helper when the edited tab is closed mid-edit', () => {
    const { id } = mount()
    act(() => { addTab('/other') })
    const tab = screen.getAllByRole('tab')[0]
    fireEvent.keyDown(tab, { key: 'F2' })
    expect(hint()).toBeVisible()
    act(() => removeTab(id))
    expect(hint()).toBeNull()
    expect(screen.queryByRole('textbox', { name: 'Terminal name' })).not.toBeInTheDocument()
  })

  it('keeps the helper up while a second editor opens before the first blur-saves', async () => {
    mount()
    act(() => { addTab('/other') })
    const [first, second] = screen.getAllByRole('tab')
    fireEvent.keyDown(first, { key: 'F2' })
    expect(hint()).toBeVisible()
    // Opening the second editor blurs (and saves) the first in the same tick.
    fireEvent.keyDown(second, { key: 'F2' })
    await waitFor(() => expect(screen.getAllByRole('textbox', { name: 'Terminal name' })).toHaveLength(1))
    expect(hint()).toBeVisible()
    fireEvent.keyDown(editor(), { key: 'Escape' })
    expect(hint()).toBeNull()
  })

  it('renders the field as the only filled box: the chip sheds its pill while editing, and selection speaks in fill not accent text', () => {
    mount()
    act(() => { addTab('/other') })
    const [inactive, active] = screen.getAllByRole('tab')
    expect(active).toHaveAttribute('aria-selected', 'true')
    // Selected = elevated pill with strong text; never the accent that a focus ring paints in.
    expect(active.className).toMatch(/bg-bg-elevated/)
    expect(active.className).toMatch(/text-text-strong/)
    expect(active.className).not.toMatch(/text-accent/)
    expect(inactive.className).not.toMatch(/bg-bg-elevated|border-border|text-accent/)

    for (const tab of [active, inactive]) {
      fireEvent.keyDown(tab, { key: 'F2' })
      // The pill is gone — no elevated fill, border, shadow or selection text —
      // while the field carries its own visible fill and border.
      expect(tab.className).not.toMatch(/bg-bg-elevated|border-border|shadow-sm|text-accent|text-text-strong/)
      expect(tab.className).toMatch(/border-transparent/)
      expect(editor().className).toMatch(/bg-bg-hover/)
      expect(editor().className).not.toMatch(/bg-bg-elevated/)
      expect(editor().className).toMatch(/border-border/)
      expect(editor().className).toMatch(/focus-ring/)
      fireEvent.keyDown(editor(), { key: 'Escape' })
    }
    // The selected pill comes back untouched once editing ends.
    expect(active.className).toMatch(/bg-bg-elevated/)
    expect(active.className).toMatch(/text-text-strong/)
    expect(active).toHaveAttribute('aria-selected', 'true')
  })

  it('gives an inactive chip a neutral focus outline and leaves the selected pill on the global accent outline', async () => {
    mount()
    act(() => { addTab('/other') })
    const [inactive, active] = screen.getAllByRole('tab')
    expect(active).toHaveAttribute('aria-selected', 'true')
    // Selected: ring and pill are one element, so the global accent outline
    // stays — no focus utility of its own. Inactive: an accent ring beside the
    // pill reads as a second selection at any strength, so the cue must differ
    // in COLOUR — the neutral `--muted` token, same 2px/2px geometry.
    expect(active.className).not.toMatch(/focus-visible:|focus-ring/)
    expect(inactive.className).toMatch(/\bfocus-visible:outline-2\b/)
    expect(inactive.className).toMatch(/\bfocus-visible:outline-muted\b/)
    expect(inactive.className).toMatch(/\bfocus-visible:outline-offset-2\b/)
    // Not selection grammar: no accent, no `--ring` treatment, no strong text.
    expect(inactive.className).not.toMatch(/focus-ring|outline-accent|ring-accent|outline-text-strong|outline-\[/)
    // Neither branch suppresses the outline without a cue in its place.
    expect(inactive.className).not.toMatch(/outline-(none|hidden)/)
    expect(active.className).not.toMatch(/outline-(none|hidden)/)

    // The case the cue exists for: focus returns to the still-inactive chip
    // after saving its rename, and that chip must paint the neutral outline —
    // while the field is open the chip paints no ring of its own.
    inactive.focus()
    await userEvent.keyboard('{F2}')
    expect(inactive.className).not.toMatch(/focus-visible:/)
    await userEvent.clear(editor())
    await userEvent.type(editor(), 'Background logs{Enter}')
    expect(inactive).toHaveFocus()
    expect(inactive).toHaveAttribute('aria-selected', 'false')
    expect(inactive.className).toMatch(/\bfocus-visible:outline-muted\b/)
    expect(active.className).not.toMatch(/focus-visible:|focus-ring/)
  })

  it('overlays the helper without remounting, hiding or resizing any shell', () => {
    terminal.autofocus = true
    const { id } = mount()
    let otherId = ''
    act(() => { otherId = addTab('/other')! })
    const shell = screen.getByTestId(`cli-${id}`)
    const otherShell = screen.getByTestId(`cli-${otherId}`)
    const inactiveTab = screen.getAllByRole('tab')[0]
    fireEvent.click(inactiveTab, { detail: 1 })
    expect(shell).toHaveFocus()
    fireEvent.keyDown(inactiveTab, { key: 'F2' })
    expect(hint()).toBeVisible()
    expect(editor()).toHaveFocus()
    // Absolutely positioned over the body: the shells keep their nodes, their
    // visibility and their box, so xterm never refits and the PTY sees nothing.
    expect(hint()!.className).toMatch(/absolute/)
    expect(hint()!.parentElement).toBe(shell.parentElement!.parentElement)
    expect(screen.getByTestId(`cli-${id}`)).toBe(shell)
    expect(screen.getByTestId(`cli-${otherId}`)).toBe(otherShell)
    expect(shell.parentElement!.style.display).toBe('block')
    expect(otherShell.parentElement!.style.display).toBe('none')
    fireEvent.keyDown(editor(), { key: 'Enter' })
    expect(hint()).toBeNull()
    expect(screen.getByTestId(`cli-${id}`)).toBe(shell)
    expect(snapshot().activeId).toBe(id)
    expect(terminal.dispose).not.toHaveBeenCalled()
    expect(terminal.del).not.toHaveBeenCalled()
  })
})

const UNICODE_NAMES = [
  { label: '119 BMP characters and an emoji', value: 'x'.repeat(119) + '😀', expected: 'x'.repeat(119) + '😀' },
  { label: 'more than 120 emoji', value: '😀'.repeat(130), expected: '😀'.repeat(120) },
]

function expectIntactName(value: string, expected: string) {
  expect(value).toBe(expected)
  expect(Array.from(value)).toHaveLength(MAX_TERMINAL_NAME_LENGTH)
  expect(value).not.toContain('\uFFFD')
  // In Unicode mode, paired surrogates are matched as their full code point.
  expect(value).not.toMatch(/[\uD800-\uDFFF]/u)
}

describe('terminal Unicode name cap', () => {
  it.each(UNICODE_NAMES)('caps $label on change, save and restoration', ({ value, expected }) => {
    const { id } = mount()
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'F2' })
    expect(editor()).not.toHaveAttribute('maxlength')
    fireEvent.change(editor(), { target: { value } })
    expectIntactName((editor() as HTMLInputElement).value, expected)
    expect(snapshot().tabs[0].name).toBeUndefined()
    fireEvent.keyDown(editor(), { key: 'Enter' })
    const saved = bottomTerminalPrefsSnapshot(localStorage.getItem('mc-bottom-terminal')!)
    expectIntactName(localStorage.getItem(`mc-terminal-name:${id}`)!, expected)
    expectIntactName(JSON.parse(saved).tabs[0].name, expected)
    act(() => __resetBottomTerminal())
    localStorage.setItem('mc-bottom-terminal', saved)
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'mc-bottom-terminal' })))
    expectIntactName(snapshot().tabs[0].name!, expected)
    expect(screen.getByRole('tab')).toHaveAccessibleName(expected)
    expect(snapshot().tabs[0]).toEqual({ id, cwd: '/project', name: expected })
  })

  it.each(UNICODE_NAMES)('pastes $label intact in the popout', async ({ value, expected }) => {
    const user = userEvent.setup()
    mount('popout')
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'F2' })
    await user.clear(editor())
    await user.paste(value)
    expectIntactName((editor() as HTMLInputElement).value, expected)
    await user.keyboard('{Enter}')
    expectIntactName(snapshot().tabs[0].name!, expected)
    expect(terminal.dispose).not.toHaveBeenCalled()
    expect(terminal.del).not.toHaveBeenCalled()
  })

  it.each(UNICODE_NAMES)('normalizes $label from storage and trim-on-save', ({ value, expected }) => {
    const id = addTab('/project')!
    renameTab(id, `  ${value}  `)
    expectIntactName(snapshot().tabs[0].name!, expected)
    localStorage.setItem('mc-bottom-terminal', JSON.stringify({ tabs: [{ id, name: `  ${value}  ` }] }))
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'mc-bottom-terminal' })))
    expectIntactName(snapshot().tabs[0].name!, expected)
  })

  it('caps the initial live title without trimming the draft', () => {
    terminal.title = ' ' + '😀'.repeat(130)
    mount()
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'F2' })
    expectIntactName((editor() as HTMLInputElement).value, ' ' + '😀'.repeat(119))
    fireEvent.keyDown(editor(), { key: 'Escape' })
    expect(snapshot().tabs[0].name).toBeUndefined()
  })

  it('keeps Unicode composition and its committing Enter inside the editor', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    mount()
    fireEvent.keyDown(screen.getByRole('tab'), { key: 'F2' })
    fireEvent.compositionStart(editor())
    const expected = 'x'.repeat(119) + '😀'
    fireEvent.change(editor(), { target: { value: expected } })
    fireEvent.keyDown(editor(), { key: 'Enter', isComposing: true })
    fireEvent.keyDown(editor(), { key: 'Escape', keyCode: 229 })
    expectIntactName((editor() as HTMLInputElement).value, expected)
    fireEvent.compositionEnd(editor())
    fireEvent.keyDown(editor(), { key: 'Enter' })
    expect(snapshot().tabs[0].name).toBeUndefined()
    expect(editor()).toHaveFocus()
    act(() => vi.advanceTimersByTime(80))
    fireEvent.keyDown(editor(), { key: 'Enter' })
    expectIntactName(snapshot().tabs[0].name!, expected)
  })
})

describe('terminal tab name persistence', () => {
  it('keeps names, ids and cwd across reorder, hide/reopen and storage restoration', () => {
    const first = addTab('/one')!
    const second = addTab('/two')!
    renameTab(first, '  Build  ')
    setTabsOrder([...snapshot().tabs].reverse())
    closeBottomTerminal()
    openBottomTerminal()
    const saved = localStorage.getItem('mc-bottom-terminal')!
    __resetBottomTerminal()
    localStorage.setItem('mc-bottom-terminal', saved)
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'mc-bottom-terminal' })))
    expect(snapshot().tabs).toEqual([
      { id: second, cwd: '/two', name: undefined },
      { id: first, cwd: '/one', name: 'Build' },
    ])
    expect(snapshot().activeId).toBe(second)
  })

  it('bounds names, ignores nonexistent tabs and tolerates malformed stored names', () => {
    const id = addTab()!
    renameTab(id, 'x'.repeat(MAX_TERMINAL_NAME_LENGTH + 10))
    expect(snapshot().tabs[0].name).toHaveLength(MAX_TERMINAL_NAME_LENGTH)
    renameTab('missing', 'No tab')
    expect(snapshot().tabs).toHaveLength(1)
    localStorage.removeItem(`mc-terminal-name:${id}`)
    localStorage.setItem('mc-bottom-terminal', JSON.stringify({ tabs: [{ id, name: { invalid: true } }] }))
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'mc-bottom-terminal' })))
    expect(snapshot().tabs[0].name).toBeUndefined()
  })
})

describe('terminal Unicode name cold reload', () => {
  it.each(UNICODE_NAMES)('reloads $label without splitting code points', async ({ value, expected }) => {
    const id = addTab('/project')!
    renameTab(id, value)
    const saved = localStorage.getItem('mc-bottom-terminal')!
    vi.resetModules()
    const reloaded = await import('../hooks/useBottomTerminal')
    const { result, unmount } = renderHook(() => reloaded.useBottomTerminal())
    try {
      expectIntactName(result.current.tabs[0].name!, expected)
      expect(result.current.tabs[0]).toEqual({ id, cwd: '/project', name: expected })
      expect(result.current.activeId).toBe(id)
      expect(localStorage.getItem('mc-bottom-terminal')).toBe(saved)
    } finally {
      unmount()
      reloaded.__resetBottomTerminal()
    }
  })
})
