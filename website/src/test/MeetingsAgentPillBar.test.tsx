// The agent pill bar — ported from the upstream app's own test file.
//
// What it pins: a pill reflects and toggles its agent's enabled state, the live
// dot only appears while the meeting is running, the preset picker round-trips,
// and the attachment menu's icon-only controls carry accessible labels (the
// blocking `icon-buttons-need-labels` rule).

import { useState } from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'

import AgentPillBar from '../apps/meetings/components/AgentPillBar'
import { menuItemsOf } from '../hooks/useMenuKeyboard'
import type { AgentDef, Attachment, MeetingStatus, Preset } from '../apps/meetings/api'

const AGENTS: AgentDef[] = [
  { id: 'note-taker', name: 'Note Taker', widget_type: 'markdown' },
  { id: 'sketch-artist', name: 'Sketch Artist', widget_type: 'html' },
]

const PRESETS: Record<string, Preset> = {
  standup: { enabled_agents: ['note-taker'] },
  design: { enabled_agents: ['note-taker', 'sketch-artist'] },
}

function mount(overrides: Partial<React.ComponentProps<typeof AgentPillBar>> = {}) {
  const props: React.ComponentProps<typeof AgentPillBar> = {
    agents: AGENTS,
    enabledIds: ['note-taker'],
    mutedAgents: [],
    presets: PRESETS,
    defaultPreset: 'standup',
    selectedPreset: 'standup',
    status: 'idle' as MeetingStatus,
    attachments: [],
    attachMenuOpen: false,
    onPresetChange: vi.fn(),
    onToggleAgent: vi.fn(),
    onOpenSettings: vi.fn(),
    onToggleAttachMenu: vi.fn(),
    onAddAttachment: vi.fn(),
    onRemoveAttachment: vi.fn(),
    ...overrides,
  }
  return { props, ...render(<AgentPillBar {...props} />) }
}

afterEach(cleanup)

describe('AgentPillBar', () => {
  it('renders a pill per agent', () => {
    mount()
    expect(screen.getByText('Note Taker')).toBeTruthy()
    expect(screen.getByText('Sketch Artist')).toBeTruthy()
  })

  it('toggles an agent ON when a disabled pill is clicked', () => {
    const onToggleAgent = vi.fn()
    mount({ onToggleAgent })
    fireEvent.click(screen.getByText('Sketch Artist').closest('button')!)
    // The second argument is the DESIRED state, so an off pill must ask for true.
    expect(onToggleAgent).toHaveBeenCalledWith('sketch-artist', true)
  })

  it('toggles an agent OFF when an enabled pill is clicked', () => {
    const onToggleAgent = vi.fn()
    mount({ onToggleAgent })
    fireEvent.click(screen.getByText('Note Taker').closest('button')!)
    expect(onToggleAgent).toHaveBeenCalledWith('note-taker', false)
  })

  it('shows a live dot for an enabled, unmuted agent during an active meeting', () => {
    mount({ status: 'active' })
    const pill = screen.getByText('Note Taker').closest('button')!
    expect(pill.querySelector('.animate-pulse')).toBeTruthy()
  })

  it('shows no live dot while the meeting is idle', () => {
    mount({ status: 'idle' })
    expect(
      screen.getByText('Note Taker').closest('button')!.querySelector('.animate-pulse'),
    ).toBeNull()
  })

  it('shows no live dot for a muted agent even during an active meeting', () => {
    mount({ status: 'active', mutedAgents: ['note-taker'] })
    expect(
      screen.getByText('Note Taker').closest('button')!.querySelector('.animate-pulse'),
    ).toBeNull()
  })

  it('renders the preset picker with the active preset selected', () => {
    mount()
    // The picker is a Radix Select now, so the selection lives in the trigger's
    // text, not a `.value` — and 'standup' is the default preset, so it renders
    // decorated.
    expect(screen.getByRole('combobox', { name: 'Agent preset' })).toHaveTextContent(
      'standup (default)',
    )
  })

  it('reports a preset change', async () => {
    const onPresetChange = vi.fn()
    mount({ onPresetChange })
    // A `change` event on the trigger does nothing — open it, then click the option.
    fireEvent.click(screen.getByRole('combobox', { name: 'Agent preset' }))
    fireEvent.click(await screen.findByRole('option', { name: 'design' }))
    expect(onPresetChange).toHaveBeenCalledWith('design')
  })

  it('clears the preset back to empty from the "no preset" row', async () => {
    // The old `<option value="">` is now SimpleSelect's `clearLabel`, which routes
    // through an internal sentinel. This pins that '' still reaches the callback
    // rather than the sentinel leaking out.
    const onPresetChange = vi.fn()
    mount({ onPresetChange })
    fireEvent.click(screen.getByRole('combobox', { name: 'Agent preset' }))
    fireEvent.click(await screen.findByRole('option', { name: 'No preset' }))
    expect(onPresetChange).toHaveBeenCalledWith('')
  })

  it('offers to create one when there are no presets', () => {
    mount({ presets: {} })
    expect(screen.queryByRole('combobox')).toBeNull()
  })

  it('shows the attachment count', () => {
    const attachments: Attachment[] = [
      { type: 'url', url: 'https://example.test/a', label: 'A' },
      { type: 'url', url: 'https://example.test/b', label: 'B' },
    ]
    mount({ attachments })
    expect(screen.getByText('2')).toBeTruthy()
  })

  it('lists attachments and reports a removal by index', () => {
    const onRemoveAttachment = vi.fn()
    mount({
      attachMenuOpen: true,
      onRemoveAttachment,
      attachments: [{ type: 'url', url: 'https://example.test/doc', label: 'Design Doc' }],
    })
    expect(screen.getByText('Design Doc')).toBeTruthy()
    // Icon-only control: it must be reachable by its accessible name.
    fireEvent.click(screen.getByLabelText('Remove Design Doc'))
    expect(onRemoveAttachment).toHaveBeenCalledWith(0)
  })

  it('every icon-only control has an accessible name', () => {
    mount({ attachMenuOpen: true })
    for (const button of screen.getAllByRole('button')) {
      const named = button.textContent?.trim() || button.getAttribute('aria-label')
      expect(named, `unlabelled control: ${button.outerHTML.slice(0, 80)}`).toBeTruthy()
    }
  })
})

// The attachment menu is `role="menu"`, which promises the WAI-ARIA menu
// keyboard contract (#6231): arrows move real focus between the ACTIONABLE items
// and wrap, Home/End jump to the boundaries, Tab is contained inside the open
// menu, and focus enters the menu on open / returns to the trigger on Escape.
// The items are the controls (each row's remove button, the footer action), not
// the layout rows — see `expectedOrder` for why.
describe('AgentPillBar attachment menu keyboard contract', () => {
  const TWO: Attachment[] = [
    { type: 'url', url: 'https://example.test/doc', label: 'Design Doc' },
    { type: 'url', url: 'https://example.test/spec', label: 'Spec' },
  ]

  /**
   * Mount closed, then flip `attachMenuOpen` the way the host does when the
   * paperclip is clicked — so the hook observes `enabled` transitioning false →
   * true, which is what arms focus entry. Mounting straight into the open state
   * would exercise a different (first-render) path than a real open.
   */
  function openMenu(overrides: Partial<React.ComponentProps<typeof AgentPillBar>> = {}) {
    const utils = mount({ attachments: TWO, attachMenuOpen: false, ...overrides })
    utils.rerender(<AgentPillBar {...utils.props} attachMenuOpen />)
    return utils
  }

  /**
   * The expected traversal order, spelled out positionally rather than queried
   * from the DOM so the test pins the ORDER too: one stop per ACTIONABLE item —
   * each row's remove button, then the footer action. Every stop is looked up
   * BY `role="menuitem"`, because `role="menu"` only permits `menuitem` /
   * `menuitemradio` / `menuitemcheckbox` / `group` / `separator` as owned
   * children — a stop wearing any other role (the footer's old `role="button"`)
   * is an invalid child that assistive tech announces as a loose button inside
   * a menu rather than an item OF it. The attachment ROWS are deliberately NOT
   * stops: a row is a layout container with no activation handler, and
   * `role="menuitem"` subclasses `command`, so announcing a row as a menu item
   * would promise an Enter/Space activation that does nothing (the same
   * promise-not-kept defect class as #6231 itself). Menu semantics belong on
   * the controls, not the row.
   */
  function expectedOrder(): HTMLElement[] {
    return [
      screen.getByLabelText('Remove Design Doc'),
      screen.getByLabelText('Remove Spec'),
      screen.getByRole('menuitem', { name: 'Add a link' }),
    ]
  }

  const menu = () => screen.getByRole('menu')
  const trigger = () => screen.getByLabelText('Manage attachments')

  it('moves focus onto the first remove button when the menu opens', () => {
    openMenu()
    expect(expectedOrder()[0]).toHaveFocus()
  })

  // The hook's own discovery is the thing that decides the traversal, so assert
  // it directly: any non-actionable row that crept back into the item set would
  // show up here as an extra stop even if an ordering test happened to pass.
  it('discovers exactly the actionable controls as menu items — no inert rows', () => {
    openMenu()
    expect(menuItemsOf(menu())).toEqual(expectedOrder())
  })

  // ARIA-owned-children pin: `role="menu"` permits only menuitem /
  // menuitemradio / menuitemcheckbox / group / separator as its owned children.
  // The remove controls already carried `menuitem`; the footer action wore
  // `role="button"` (Clickable hardcodes it), so a screen reader announced the
  // menu as having TWO items with a stray button loose inside it. Assert all
  // three stops announce as menuitems, and that the menu owns nothing else.
  it('announces all three stops as menuitems — role="menu" owns no invalid child', () => {
    openMenu()
    expect(screen.getAllByRole('menuitem')).toEqual(expectedOrder())
    // Nothing inside the menu claims a role `menu` may not own.
    expect(menu().querySelectorAll('[role="button"]')).toHaveLength(0)
  })

  it('walks ArrowDown through the remove buttons and the footer in document order, wrapping past the footer', () => {
    openMenu()
    const items = expectedOrder()
    for (let i = 1; i < items.length; i++) {
      fireEvent.keyDown(menu(), { key: 'ArrowDown' })
      expect(items[i], `ArrowDown step ${i}`).toHaveFocus()
    }
    // Past the last item (the footer action) it wraps back to the first.
    fireEvent.keyDown(menu(), { key: 'ArrowDown' })
    expect(items[0]).toHaveFocus()
  })

  it('walks ArrowUp the other way and wraps from the first item to the footer', () => {
    openMenu()
    const items = expectedOrder()
    fireEvent.keyDown(menu(), { key: 'ArrowUp' })
    expect(items[items.length - 1]).toHaveFocus()
    fireEvent.keyDown(menu(), { key: 'ArrowUp' })
    expect(items[items.length - 2]).toHaveFocus()
  })

  it('jumps to the boundary items with End and Home', () => {
    openMenu()
    const items = expectedOrder()
    fireEvent.keyDown(menu(), { key: 'End' })
    expect(items[items.length - 1]).toHaveFocus()
    fireEvent.keyDown(menu(), { key: 'Home' })
    expect(items[0]).toHaveFocus()
  })

  it('contains Tab inside the open menu, wrapping at both ends', () => {
    openMenu()
    const items = expectedOrder()
    fireEvent.keyDown(menu(), { key: 'End' })
    fireEvent.keyDown(menu(), { key: 'Tab' })
    expect(items[0], 'Tab off the last item wraps to the first').toHaveFocus()
    fireEvent.keyDown(menu(), { key: 'Tab', shiftKey: true })
    expect(items[items.length - 1], 'Shift-Tab off the first item wraps to the last').toHaveFocus()
  })

  // The point of the traversal is reaching something you can then ACTIVATE.
  // `role="menuitem"` subclasses `command`, so every stop the arrows land on
  // owes an activation — a focusable stop that does nothing when pressed is the
  // defect this surface previously shipped on the row <div>.
  it('activates the focused remove button, reporting the right attachment index', () => {
    const onRemoveAttachment = vi.fn()
    openMenu({ onRemoveAttachment })
    const items = expectedOrder()
    // Focus entry already put us on the first remove button; activate it there.
    expect(items[0]).toHaveFocus()
    fireEvent.click(document.activeElement as HTMLElement)
    expect(onRemoveAttachment).toHaveBeenCalledWith(0)

    // Arrow to the second row's remove button and activate that one — the index
    // has to follow focus, not stay pinned to the first row.
    fireEvent.keyDown(menu(), { key: 'ArrowDown' })
    expect(items[1]).toHaveFocus()
    fireEvent.click(document.activeElement as HTMLElement)
    expect(onRemoveAttachment).toHaveBeenCalledWith(1)
  })

  it('activates every discovered menu item — no stop is inert', () => {
    const onRemoveAttachment = vi.fn()
    const onAddAttachment = vi.fn()
    openMenu({ onRemoveAttachment, onAddAttachment })
    // Walk the hook's OWN item list and activate each stop where the arrows put
    // focus, then assert the callback each one promises actually fired. An inert
    // stop (a layout row wearing role="menuitem") lands here as a missing call.
    const items = menuItemsOf(menu())
    expect(items).toHaveLength(3)
    fireEvent.keyDown(menu(), { key: 'Home' })
    for (const item of items) {
      expect(document.activeElement).toBe(item)
      fireEvent.click(document.activeElement as HTMLElement)
      fireEvent.keyDown(menu(), { key: 'ArrowDown' })
    }
    expect(onRemoveAttachment.mock.calls).toEqual([[0], [1]])
    expect(onAddAttachment).toHaveBeenCalledTimes(1)
  })

  // Regression PIN for the native activation path: the remove control is a real
  // <button>, so the browser turns Enter into a click for free — but only if the
  // menu contract leaves the key alone. This pins that Enter is NOT consumed
  // (fireEvent returns false when a handler called preventDefault). Asserted via
  // the event rather than a callback because jsdom/happy-dom do not synthesise
  // the native Enter→click on a focused button.
  it('leaves Enter on a focused remove button to the button\'s native activation', () => {
    openMenu()
    const focused = document.activeElement as HTMLElement
    expect(focused).toBe(expectedOrder()[0])
    expect(fireEvent.keyDown(focused, { key: 'Enter' })).toBe(true)
  })

  // Regression PIN: Escape already asked the host to close before this wiring.
  it('asks the host to close the menu on Escape', () => {
    const onToggleAttachMenu = vi.fn()
    openMenu({ onToggleAttachMenu })
    fireEvent.keyDown(menu(), { key: 'Escape' })
    expect(onToggleAttachMenu).toHaveBeenCalled()
  })

  it('returns focus to the paperclip trigger on Escape', () => {
    openMenu()
    // Focus now sits INSIDE the menu (focus entry on open), so a close that did
    // not restore would orphan focus on a control that is about to unmount.
    fireEvent.keyDown(menu(), { key: 'Escape' })
    expect(trigger()).toHaveFocus()
  })
})

// Focus repair for the two HOST-driven paths that can strand focus on <body>.
// AgentPillBar is CONTROLLED — `attachments` and `attachMenuOpen` are props —
// so activating an item never changes the menu itself; the host does, on a
// later render:
//
//   (a) a removal keeps the menu OPEN and refreshes `attachments`, so the
//       index-keyed row holding focus unmounts from under the keyboard user.
//       Focus falls to <body>, from where Tab escapes the still-open menu and
//       the container's Escape handler is unreachable (Escape is bound to the
//       menu element, not the document).
//   (b) a successful 'Add a link' CLOSES the menu from the host while focus is
//       still inside it, orphaning focus on <body> instead of handing it back
//       to the paperclip the way Escape does.
//
// These tests drive AgentPillBar through a real controlled host, so the props
// move the way the app moves them rather than the way a rerender() spells them.
describe('AgentPillBar attachment menu focus repair (controlled host)', () => {
  const TWO: Attachment[] = [
    { type: 'url', url: 'https://example.test/doc', label: 'Design Doc' },
    { type: 'url', url: 'https://example.test/spec', label: 'Spec' },
  ]

  interface HostProps {
    initial?: Attachment[]
    onAdd?: () => void
  }

  function Host({ initial = TWO, onAdd }: HostProps) {
    const [attachments, setAttachments] = useState<Attachment[]>(initial)
    const [attachMenuOpen, setAttachMenuOpen] = useState(false)
    return (
      <>
        {/* Refreshes the `attachments` prop identity WITHOUT touching the menu's
            open state — path (a)'s host re-render, reachable from a test without
            performing a removal (used by the negative test below). */}
        <button
          type="button"
          data-testid="host-refresh"
          onClick={() => setAttachments(a => [...a])}
        >
          refresh
        </button>
        <AgentPillBar
          agents={AGENTS}
          enabledIds={['note-taker']}
          mutedAgents={[]}
          presets={PRESETS}
          defaultPreset="standup"
          selectedPreset="standup"
          status={'idle' as MeetingStatus}
          attachments={attachments}
          attachMenuOpen={attachMenuOpen}
          onPresetChange={vi.fn()}
          onToggleAgent={vi.fn()}
          onOpenSettings={vi.fn()}
          onToggleAttachMenu={() => setAttachMenuOpen(o => !o)}
          // The real handler opens a prompt and, on success, closes the menu.
          onAddAttachment={() => {
            onAdd?.()
            setAttachMenuOpen(false)
          }}
          // The real handler drops the entry and leaves the menu open.
          onRemoveAttachment={i => setAttachments(a => a.filter((_, n) => n !== i))}
        />
      </>
    )
  }

  const trigger = () => screen.getByLabelText('Manage attachments')
  const menu = () => screen.getByRole('menu')
  const footer = () => screen.getByRole('menuitem', { name: 'Add a link' })

  /** Click the paperclip the way a user does, so the hook sees closed → open. */
  function openMenu(props: HostProps = {}) {
    const utils = render(<Host {...props} />)
    fireEvent.click(trigger())
    return utils
  }

  // PIN, and the guard against over-reaching: the restore-to-trigger branch must
  // not fire on a first render that merely happens to find focus on <body>.
  it('does not steal focus on mount while the menu is closed', () => {
    render(<Host />)
    expect(document.body).toHaveFocus()
    expect(trigger()).not.toHaveFocus()
  })

  it('repairs focus onto the first surviving menu item when the focused row is removed', () => {
    openMenu()
    fireEvent.keyDown(menu(), { key: 'ArrowDown' })
    const second = screen.getByLabelText('Remove Spec')
    expect(second).toHaveFocus()
    fireEvent.click(second)
    // The host dropped 'Spec', so the control holding focus unmounted while the
    // menu stayed open — focus belongs on the menu's first surviving item.
    expect(screen.queryByText('Spec')).toBeNull()
    expect(document.body).not.toHaveFocus()
    expect(menuItemsOf(menu())[0]).toHaveFocus()
    expect(screen.getByLabelText('Remove Design Doc')).toHaveFocus()
  })

  it('repairs focus onto the footer action when the LAST attachment is removed', () => {
    openMenu({ initial: [TWO[0]] })
    const only = screen.getByLabelText('Remove Design Doc')
    expect(only).toHaveFocus()
    fireEvent.click(only)
    // No rows are left, so the menu's only surviving item is the footer action,
    // and that is what this implementation focuses (`menuItemsOf(...)[0]`). The
    // trigger is only the fallback for an open menu with NO items at all.
    expect(screen.getByText('No attachments')).toBeTruthy()
    expect(document.body).not.toHaveFocus()
    expect(footer()).toHaveFocus()
  })

  it('returns focus to the paperclip when the host closes the menu after "Add a link"', () => {
    const onAdd = vi.fn()
    openMenu({ onAdd })
    fireEvent.keyDown(menu(), { key: 'End' })
    expect(footer()).toHaveFocus()
    fireEvent.click(footer())
    expect(onAdd).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('menu')).toBeNull()
    // Focus was inside the menu the host just closed; orphaning it on <body> is
    // the defect. The paperclip is where Escape already puts it.
    expect(document.body).not.toHaveFocus()
    expect(trigger()).toHaveFocus()
  })

  // NEGATIVE guard: the repair only ever adopts focus that is genuinely LOST
  // (`document.activeElement === document.body`). A host re-render while focus
  // sits somewhere legitimate must leave it exactly there.
  it('does NOT steal focus when a host re-render arrives while focus sits on the trigger', () => {
    openMenu()
    // Park focus back on the paperclip with the menu still open — a legitimate
    // place for it (Shift-Tab reach, or a pointer click on the trigger itself).
    trigger().focus()
    fireEvent.click(screen.getByTestId('host-refresh'))
    // The `attachments` identity changed, so the effect re-ran; it must no-op.
    expect(trigger()).toHaveFocus()
  })
})
