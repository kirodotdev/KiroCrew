import { screen, fireEvent, cleanup } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../test/helpers'
import SessionActionsMenu from './SessionActionsMenu'
import { sseSlots, markSlotUnread } from '../store/dashboardSlice'
import { sseSubagentSpawn } from '../store/chatSlice'
import { api } from '../api/client'
import type { ChatSlot } from '../types'

/** happy-dom cannot drive Radix menus, so both families collapse to buttons.
 *  Hoisted, because the vi.mock factories below run before module init. */
const { Item, Separator } = vi.hoisted(() => ({
  Item: ({ children, onSelect, className, disabled }: {
    children?: React.ReactNode
    onSelect?: () => void
    className?: string
    disabled?: boolean
  }) => (
    <button type="button" className={className} disabled={disabled} onClick={() => onSelect?.()}>{children}</button>
  ),
  Separator: () => <hr data-testid="zzq-sep" />,
}))

vi.mock('./ui/dropdown-menu', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  DropdownMenuItem: Item,
  DropdownMenuSeparator: Separator,
}))
vi.mock('./ui/context-menu', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ContextMenuItem: Item,
  ContextMenuSeparator: Separator,
}))

// Child sections are covered by their own suites; stub them so this file's
// own branch logic is what is under test.
vi.mock('./FolderMoveSubmenu', () => ({
  default: ({ label }: { label: string }) => <div>zzq-folders:{label}</div>,
}))
vi.mock('./SendToInstanceSubmenu', () => ({ default: () => <div>zzq-send</div> }))
vi.mock('./SessionColorSwatches', () => ({ default: () => <div>zzq-colors</div> }))
vi.mock('./LinkedSurfacesSection', () => ({ default: () => <div>zzq-links</div> }))

const actions = vi.hoisted(() => ({
  toggleRead: vi.fn(),
  togglePin: vi.fn(),
  copyLink: vi.fn(),
  move: vi.fn(),
  reload: vi.fn(),
  close: vi.fn(),
}))
const popouts = vi.hoisted(() => ({
  isPoppedOut: vi.fn(() => false),
  isSelfPopout: vi.fn(() => false),
  open: vi.fn(),
  focus: vi.fn(),
  bringBack: vi.fn(),
  returnSelfToMain: vi.fn(),
}))
const openTagPopover = vi.hoisted(() => vi.fn())

vi.mock('../hooks/useSessionActions', () => ({ useSessionActions: () => actions }))
vi.mock('../hooks/useChatPopouts', () => ({ useChatPopouts: () => popouts }))
vi.mock('../hooks/useTagPopover', () => ({ useTagPopover: () => ({ open: openTagPopover }) }))
vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, chatFolders: vi.fn() } }
})

const chatFolders = vi.mocked(api.chatFolders)

function setup(
  props: Partial<React.ComponentProps<typeof SessionActionsMenu>> = {},
  slot: Partial<ChatSlot> = {},
  unread = false,
) {
  const store = createTestStore()
  store.dispatch(sseSlots([{ key: 'zzq-slot', messages: 0, running: false, ...slot } as ChatSlot]))
  if (unread) store.dispatch(markSlotUnread('zzq-slot'))
  return renderWithProviders(
    <SessionActionsMenu variant="dropdown" slotKey="zzq-slot" {...props} />,
    { store },
  )
}

const btn = (label: string | RegExp) => screen.getByRole('button', { name: label })

describe('SessionActionsMenu', () => {
  beforeEach(() => {
    Object.values(actions).forEach(fn => fn.mockReset())
    Object.values(popouts).forEach(fn => fn.mockReset())
    popouts.isPoppedOut.mockReturnValue(false)
    popouts.isSelfPopout.mockReturnValue(false)
    openTagPopover.mockReset()
    chatFolders.mockReset()
    chatFolders.mockResolvedValue([] as never)
  })

  it('wires the tab-modifier actions to the slot', () => {
    setup()
    fireEvent.click(btn('Mark as unread'))
    expect(actions.toggleRead).toHaveBeenCalledWith('zzq-slot')
    fireEvent.click(btn('Pin'))
    expect(actions.togglePin).toHaveBeenCalledWith('zzq-slot')
    fireEvent.click(btn('Tags…'))
    expect(openTagPopover).toHaveBeenCalledWith('zzq-slot')
    fireEvent.click(btn('Copy link'))
    expect(actions.copyLink).toHaveBeenCalledWith('zzq-slot')
    fireEvent.click(btn('Close session'))
    expect(actions.close).toHaveBeenCalledWith('zzq-slot')
  })

  it('wires Reload session to the slot and disables it while a turn runs', () => {
    const { unmount } = setup()
    fireEvent.click(btn('Reload session'))
    expect(actions.reload).toHaveBeenCalledWith('zzq-slot')
    unmount()

    // While the slot runs, the item is disabled: the backend would answer 409
    // anyway (killing an in-flight process orphans the streaming prompt) —
    // the disable makes that visible instead of a dead click.
    actions.reload.mockReset()
    setup({}, { running: true })
    const reloadBtn = screen.getByRole('button', { name: /Reload session/ })
    expect(reloadBtn).toBeDisabled()
    fireEvent.click(reloadBtn)
    expect(actions.reload).not.toHaveBeenCalled()
  })

  it('disables Reload session while sub-agent children are attached', () => {
    // A slot whose turn ended but whose children still run LOOKS idle; the
    // backend still 409s (the reset would tear down the children's shared
    // runtime), so the item must not offer the click.
    const store = createTestStore()
    store.dispatch(sseSlots([{ key: 'zzq-slot', messages: 0, running: false } as ChatSlot]))
    store.dispatch(sseSubagentSpawn({ slot: 'zzq-slot', id: 'sa-1', task: 't', agent: 'a' }))
    renderWithProviders(
      <SessionActionsMenu variant="dropdown" slotKey="zzq-slot" />,
      { store },
    )
    expect(screen.getByRole('button', { name: /Reload session/ })).toBeDisabled()
    // The reason renders inline: a disabled Radix item is pointer-events-none,
    // so a hover title can never explain the grey state.
    expect(screen.getByText('sub-agents working')).toBeInTheDocument()
  })

  it('says a press applies a stale config change, or what the reload waits on', () => {
    const { unmount } = setup({}, { config_stale: true })
    expect(screen.getByTestId('reload-stale-note')).toHaveTextContent(/^stale config · choose Reload session to apply$/)
    expect(screen.getByRole('button', { name: /Reload session/ })).toBeEnabled()
    unmount()

    const store = createTestStore()
    store.dispatch(sseSlots([{ key: 'zzq-slot', messages: 0, running: false, config_stale: true } as ChatSlot]))
    store.dispatch(sseSubagentSpawn({ slot: 'zzq-slot', id: 'sa-1', task: 't', agent: 'a' }))
    renderWithProviders(<SessionActionsMenu variant="dropdown" slotKey="zzq-slot" />, { store })
    const note = screen.getByTestId('reload-stale-note')
    // Two separate facts, no "·" that reads as one causing the other.
    expect(note).toHaveTextContent(/^stale config — reload when sub-agents finish$/)
    expect(note.parentElement).not.toHaveTextContent('·')
    cleanup()
    // Same while a turn runs.
    setup({}, { config_stale: true, running: true })
    expect(screen.getByTestId('reload-stale-note')).toHaveTextContent(/^stale config — reload when the turn ends$/)
  })

  it('names what changed on the stale note, for a touch user with no tooltip', () => {
    const { unmount } = setup({}, { config_stale: true, config_stale_inputs: '~/.kiro/agents/kirocrew.json' })
    expect(screen.getByTestId('reload-stale-note')).toHaveTextContent(
      /^stale config \(~\/\.kiro\/agents\/kirocrew\.json\) · choose Reload session to apply$/,
    )
    unmount()
    setup({}, { config_stale: true, running: true, config_stale_inputs: 'agent.acp_backend' })
    expect(screen.getByTestId('reload-stale-note')).toHaveTextContent(
      /^stale config \(agent\.acp_backend\) — reload when the turn ends$/,
    )
  })

  it('clamps only the changed-input list, never the remedy, and titles the whole sentence', () => {
    const inputs = '~/.kiro/agents/kirocrew.json, ~/.kiro/settings/mcp.json, agent.acp_backend'
    const { unmount } = setup({}, { config_stale: true, config_stale_inputs: inputs })
    const note = screen.getByTestId('reload-stale-note')
    expect(note).not.toHaveClass('truncate')
    expect(note).toHaveClass('min-w-0')
    expect(note).toHaveClass('max-w-[20rem]')
    expect(note).toHaveAttribute('title', `stale config (${inputs}) · choose Reload session to apply`)
    const list = screen.getByTestId('reload-stale-inputs')
    expect(list).toHaveClass('truncate')
    expect(list).toHaveClass('min-w-0')
    expect(list).toHaveTextContent(`stale config (${inputs})`)
    const clause = screen.getByTestId('reload-stale-clause')
    expect(clause).not.toHaveClass('truncate')
    expect(clause).toHaveClass('shrink-0')
    expect(clause).toHaveTextContent(/^· choose Reload session to apply$/)
    unmount()
    setup({}, { config_stale: true, running: true, config_stale_inputs: inputs })
    expect(screen.getByTestId('reload-stale-clause')).toHaveTextContent(/^— reload when the turn ends$/)
    expect(screen.getByTestId('reload-stale-note')).toHaveAttribute(
      'title',
      `stale config (${inputs}) — reload when the turn ends`,
    )
  })

  it('keeps the sub-agent wait clause whole beside a long input list', () => {
    const inputs = '~/.kiro/agents/kirocrew.json, ~/.kiro/settings/mcp.json'
    const store = createTestStore()
    store.dispatch(sseSlots([
      { key: 'zzq-slot', messages: 0, running: false, config_stale: true, config_stale_inputs: inputs } as ChatSlot,
    ]))
    store.dispatch(sseSubagentSpawn({ slot: 'zzq-slot', id: 'sa-1', task: 't', agent: 'a' }))
    renderWithProviders(<SessionActionsMenu variant="dropdown" slotKey="zzq-slot" />, { store })
    expect(screen.getByTestId('reload-stale-clause')).toHaveTextContent(/^— reload when sub-agents finish$/)
    expect(screen.getByTestId('reload-stale-note')).toHaveAttribute(
      'title',
      `stale config (${inputs}) — reload when sub-agents finish`,
    )
  })

  it('shows no stale note on a current config', () => {
    setup()
    expect(screen.queryByTestId('reload-stale-note')).toBeNull()
  })

  it('labels read/unread and pin/unpin from the live store state', () => {
    setup({}, { pinned: true }, true)
    expect(btn('Mark as read')).toBeInTheDocument()
    expect(btn('Unpin')).toBeInTheDocument()
  })

  it('offers no chat-mode switch, even for a legacy orchestrator slot', () => {
    setup({}, { mode: 'orchestrator' })
    expect(screen.queryByRole('button', { name: /Switch to (Chat|Autopilot)/ })).not.toBeInTheDocument()
  })

  it('omits Rename and Reveal unless the surface supplies them', () => {
    const { unmount } = setup()
    expect(screen.queryByRole('button', { name: 'Rename' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reveal in sidebar' })).not.toBeInTheDocument()
    unmount()

    const onRename = vi.fn()
    const onReveal = vi.fn()
    setup({ onRename, onReveal })
    fireEvent.click(btn('Rename'))
    expect(onRename).toHaveBeenCalledTimes(1)
    fireEvent.click(btn('Reveal in sidebar'))
    expect(onReveal).toHaveBeenCalledTimes(1)
  })

  it('offers Pop out for a session that is not out yet', () => {
    setup({}, { title: 'zzq-title' })
    fireEvent.click(btn('Pop out to window'))
    expect(popouts.open).toHaveBeenCalledWith('zzq-slot', 'zzq-title')
  })

  it('a popped-out session offers Focus plus Bring back, never Pop out', () => {
    popouts.isPoppedOut.mockReturnValue(true)
    setup()
    expect(screen.queryByRole('button', { name: 'Pop out to window' })).not.toBeInTheDocument()

    fireEvent.click(btn('Focus popped-out window'))
    expect(popouts.focus).toHaveBeenCalledWith('zzq-slot')
    fireEvent.click(btn('Bring back to main'))
    expect(popouts.bringBack).toHaveBeenCalledWith('zzq-slot')
  })

  it('inside the popout window itself, the only window action is returning to main', () => {
    popouts.isSelfPopout.mockReturnValue(true)
    popouts.isPoppedOut.mockReturnValue(true)
    setup()
    expect(screen.queryByRole('button', { name: 'Focus popped-out window' })).not.toBeInTheDocument()
    fireEvent.click(btn('Bring back to main'))
    expect(popouts.returnSelfToMain).toHaveBeenCalledTimes(1)
    expect(popouts.bringBack).not.toHaveBeenCalled()
  })

  it('shows the folder submenu only once folders exist', async () => {
    const { unmount } = setup()
    expect(screen.queryByText(/zzq-folders/)).not.toBeInTheDocument()
    unmount()

    chatFolders.mockResolvedValue([{ id: 'f1', name: 'zzq-f' }] as never)
    setup()
    expect(await screen.findByText('zzq-folders:Move to folder…')).toBeInTheDocument()
  })

  it('an info slot adds a leading group and therefore one more separator', () => {
    const { unmount } = setup()
    const baseline = screen.getAllByTestId('zzq-sep').length
    unmount()

    setup({ infoSlots: [<div key="i">zzq-info</div>] })
    expect(screen.getByText('zzq-info')).toBeInTheDocument()
    expect(screen.getAllByTestId('zzq-sep')).toHaveLength(baseline + 1)
  })

  it('renders the same item set through the context-menu family', () => {
    const store = createTestStore()
    store.dispatch(sseSlots([{ key: 'zzq-slot', messages: 0, running: false } as ChatSlot]))
    renderWithProviders(
      <SessionActionsMenu variant="context" slotKey="zzq-slot" />,
      { store },
    )
    fireEvent.click(btn('Close session'))
    expect(actions.close).toHaveBeenCalledWith('zzq-slot')
  })
})
