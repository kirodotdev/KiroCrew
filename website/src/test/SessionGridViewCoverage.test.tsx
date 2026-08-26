/**
 * Coverage for SessionGridView — Kiro Crew's native in-place "terminal split"
 * chat surface.
 *
 * The view is thin glue over useSessionGrid, and the parts worth pinning down are
 * exactly the glue: seeding on entry, the three collapse rules (0 leaves → leave,
 * 1 session → hand back to single chat, 1 placeholder → leave), the ⌘D/Ctrl+D
 * split binding and its modifier guards, healing a restored layout against the
 * live slot list, the slot-focus intent signal, per-leaf rendering (session /
 * terminal / picker), and the PlaceholderPane picker itself (search, sort,
 * occupied-slot exclusion, create, fork, split, close).
 *
 * ChatPane is stubbed: it is a full live chat surface of its own with its own
 * tests, and the only contract this view has with it is the six props it passes.
 * The split renderer (SessionGridLayout) and the grid state hook stay REAL so the
 * tree transforms are exercised end to end.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import SessionGridView from '../components/SessionGridView'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { emitSlotFocused } from '../hooks/useWebSocket'
import type { GridNode } from '../hooks/useSessionGrid'

vi.mock('../api/client')

vi.mock('../hooks/useWebSocket', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useWebSocket')>()
  return { ...actual, emitSlotFocused: vi.fn() }
})

// A live ChatPane mounts the whole per-slot composer (ChatInput, model/agent
// pickers, message store wiring). This view only ever hands it six props, so the
// stub surfaces each callback as its own button and echoes the focused flag.
vi.mock('../components/ChatPane', () => ({
  default: ({
    slotKey,
    focused,
    onFocus,
    onRemove,
    onSplitRight,
    onSplitDown,
  }: {
    slotKey: string
    focused?: boolean
    onFocus?: () => void
    onRemove?: () => void
    onSplitRight?: () => void
    onSplitDown?: () => void
  }) => (
    <div data-testid={`pane-${slotKey}`} data-focused={focused ? 'yes' : 'no'}>
      <button type="button" aria-label={`focus ${slotKey}`} onClick={onFocus} />
      <button type="button" aria-label={`remove ${slotKey}`} onClick={onRemove} />
      <button type="button" aria-label={`right ${slotKey}`} onClick={onSplitRight} />
      <button type="button" aria-label={`down ${slotKey}`} onClick={onSplitDown} />
    </div>
  ),
}))

const STORE_KEY = 'mc-split-layouts'

type Slot = {
  key: string
  title?: string
  running?: boolean
  pending_approval?: boolean
  needs_input?: boolean
  messages?: number
  agent?: string
  last_activity_ts?: string
}

const leaf = (id: string, slot?: string): GridNode =>
  slot ? { type: 'leaf', id, kind: 'session', slot } : { type: 'leaf', id, kind: 'placeholder' }

const splitOf = (children: GridNode[]): GridNode => ({
  type: 'split',
  id: 'split-root',
  dir: 'col',
  children,
  sizes: children.map(() => 1 / children.length),
})

/** Persist a layout under `anchor` so useSessionGrid restores it instead of seeding. */
const seedStore = (anchor: string, tree: GridNode) => {
  localStorage.setItem(STORE_KEY, JSON.stringify({ [anchor]: tree }))
}

/** Seed every endpoint the view (and its picker) can reach. */
function seedApi(slots: Slot[] = []) {
  const m = vi.mocked(api)
  m.chatSlots = vi.fn().mockResolvedValue(slots)
  m.createChatSlot = vi.fn().mockResolvedValue({ key: 'created-1' })
  m.forkChatSlot = vi.fn().mockResolvedValue({ ok: true, key: 'forked-1' })
  return m
}

function renderGrid(seedSlot?: string | null) {
  const onClose = vi.fn()
  const onCollapse = vi.fn()
  const utils = renderWithProviders(
    <SessionGridView onClose={onClose} onCollapse={onCollapse} seedSlot={seedSlot} />,
  )
  return { ...utils, onClose, onCollapse }
}

/** Every placeholder picker pane currently mounted (outer pane element). */
function pickers(): HTMLElement[] {
  return screen.queryAllByPlaceholderText('Search sessions…').map((input) => {
    const pane = input.parentElement?.parentElement
    if (!pane) throw new Error('picker pane element not found')
    return pane as HTMLElement
  })
}

const onlyPicker = (): HTMLElement => {
  const all = pickers()
  expect(all).toHaveLength(1)
  return all[0]
}

/** The session rows inside a picker's scroll list (its last child). */
const rowsOf = (picker: HTMLElement) =>
  within(picker.lastElementChild as HTMLElement).queryAllByRole('button')

beforeEach(() => {
  localStorage.clear()
  vi.mocked(emitSlotFocused).mockClear()
  seedApi()
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('SessionGridView — entry seeding', () => {
  it('splits the current session in place, session pane beside a fresh picker', async () => {
    seedApi([{ key: 'a', title: 'Alpha' }])
    renderGrid('a')

    expect(await screen.findByTestId('pane-a')).toBeTruthy()
    expect(pickers()).toHaveLength(1)
  })

  it('focuses the fresh picker, not the seeded session pane', async () => {
    renderGrid('a')

    const pane = await screen.findByTestId('pane-a')
    expect(pane.getAttribute('data-focused')).toBe('no')
  })

  it('leaves split mode when there is no session to seed from', async () => {
    const { onClose, onCollapse } = renderGrid(null)

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(onCollapse).not.toHaveBeenCalled()
    // A lone placeholder is not a split, but it is still what renders until the
    // parent surface tears the view down.
    expect(pickers()).toHaveLength(1)
  })

  it('treats an omitted seedSlot the same as an explicit null', async () => {
    const { onClose } = renderGrid()

    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('restores the layout persisted under the anchor instead of re-seeding', async () => {
    seedStore('a', splitOf([leaf('l-a', 'a'), leaf('l-b', 'b')]))
    seedApi([{ key: 'a' }, { key: 'b' }])
    renderGrid('a')

    expect(await screen.findByTestId('pane-a')).toBeTruthy()
    expect(screen.getByTestId('pane-b')).toBeTruthy()
    expect(pickers()).toHaveLength(0)
  })
})

describe('SessionGridView — collapse rules', () => {
  it('hands a lone surviving session back to the native single-chat surface', async () => {
    renderGrid('a')
    const picker = onlyPicker()

    fireEvent.click(within(picker).getByLabelText('Close cell'))

    await waitFor(() => expect(pickers()).toHaveLength(0))
    expect(screen.getByTestId('pane-a')).toBeTruthy()
  })

  it('reports the collapsed slot to onCollapse', async () => {
    const { onCollapse } = renderGrid('a')

    fireEvent.click(within(onlyPicker()).getByLabelText('Close cell'))

    await waitFor(() => expect(onCollapse).toHaveBeenCalledWith('a'))
  })

  it('falls back to the loading surface once every pane is closed', async () => {
    const { onClose, onCollapse } = renderGrid('a')
    fireEvent.click(within(onlyPicker()).getByLabelText('Close cell'))
    await waitFor(() => expect(onCollapse).toHaveBeenCalledWith('a'))

    fireEvent.click(screen.getByLabelText('remove a'))

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(screen.getByText('Loading…')).toBeTruthy()
  })
})

describe('SessionGridView — split keybinding', () => {
  it('splits the focused pane right on Cmd+D', async () => {
    renderGrid('a')
    await screen.findByTestId('pane-a')

    fireEvent.keyDown(document, { key: 'd', metaKey: true })

    await waitFor(() => expect(pickers()).toHaveLength(2))
  })

  it('splits on Ctrl+D as well, and accepts an upper-case key', async () => {
    renderGrid('a')
    await screen.findByTestId('pane-a')

    fireEvent.keyDown(document, { key: 'D', ctrlKey: true })

    await waitFor(() => expect(pickers()).toHaveLength(2))
  })

  it.each([
    ['no modifier', { key: 'd' }],
    ['Shift held', { key: 'd', metaKey: true, shiftKey: true }],
    ['Alt held', { key: 'd', metaKey: true, altKey: true }],
    ['another key', { key: 'k', metaKey: true }],
  ])('ignores a keydown with %s', async (_label, init) => {
    renderGrid('a')
    await screen.findByTestId('pane-a')

    fireEvent.keyDown(document, init)

    expect(pickers()).toHaveLength(1)
  })

  it('is inert once the grid holds no panes at all', async () => {
    const { onClose, onCollapse } = renderGrid('a')
    fireEvent.click(within(onlyPicker()).getByLabelText('Close cell'))
    await waitFor(() => expect(onCollapse).toHaveBeenCalled())
    fireEvent.click(screen.getByLabelText('remove a'))
    await waitFor(() => expect(onClose).toHaveBeenCalled())

    fireEvent.keyDown(document, { key: 'd', metaKey: true })

    expect(screen.getByText('Loading…')).toBeTruthy()
    expect(pickers()).toHaveLength(0)
  })

  it('stops splitting after unmount', async () => {
    const { unmount } = renderGrid('a')
    await screen.findByTestId('pane-a')

    unmount()
    fireEvent.keyDown(document, { key: 'd', metaKey: true })

    expect(pickers()).toHaveLength(0)
  })
})

describe('SessionGridView — healing a restored layout', () => {
  it('drops panes whose session disappeared while away', async () => {
    seedStore('a', splitOf([leaf('l-a', 'a'), leaf('l-b', 'b')]))
    seedApi([{ key: 'a' }])
    const { onCollapse } = renderGrid('a')

    await waitFor(() => expect(onCollapse).toHaveBeenCalledWith('a'))
    expect(screen.queryByTestId('pane-b')).toBeNull()
  })

  it('keeps every pane when the whole layout is still live', async () => {
    seedStore('a', splitOf([leaf('l-a', 'a'), leaf('l-b', 'b')]))
    seedApi([{ key: 'a' }, { key: 'b' }])
    const { onCollapse, onClose } = renderGrid('a')

    await waitFor(() => expect(vi.mocked(api).chatSlots).toHaveBeenCalled())
    expect(screen.getByTestId('pane-a')).toBeTruthy()
    expect(screen.getByTestId('pane-b')).toBeTruthy()
    expect(onCollapse).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('does not prune against an empty slot list', async () => {
    seedStore('a', splitOf([leaf('l-a', 'a'), leaf('l-b', 'b')]))
    seedApi([])
    const { onClose } = renderGrid('a')

    await waitFor(() => expect(vi.mocked(api).chatSlots).toHaveBeenCalled())
    expect(screen.getByTestId('pane-a')).toBeTruthy()
    expect(screen.getByTestId('pane-b')).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })
})

describe('SessionGridView — slot-focus intent signal', () => {
  it('emits a blur frame while a placeholder holds focus', async () => {
    renderGrid('a')
    await screen.findByTestId('pane-a')

    expect(vi.mocked(emitSlotFocused).mock.calls.at(-1)).toEqual([null])
  })

  it('emits the pane slot once a session pane takes focus', async () => {
    renderGrid('a')
    await screen.findByTestId('pane-a')

    fireEvent.click(screen.getByLabelText('focus a'))

    await waitFor(() => expect(vi.mocked(emitSlotFocused).mock.calls.at(-1)).toEqual(['a']))
    expect(screen.getByTestId('pane-a').getAttribute('data-focused')).toBe('yes')
  })

  it('emits the restored layout first pane on entry', async () => {
    seedStore('a', splitOf([leaf('l-a', 'a'), leaf('l-b', 'b')]))
    seedApi([{ key: 'a' }, { key: 'b' }])
    renderGrid('a')

    await waitFor(() => expect(vi.mocked(emitSlotFocused).mock.calls.at(-1)).toEqual(['a']))
  })
})

describe('SessionGridView — leaf rendering', () => {
  it('renders the Phase 2 notice for a terminal leaf', async () => {
    seedStore(
      'a',
      splitOf([
        leaf('l-a', 'a'),
        { type: 'leaf', id: 'l-t', kind: 'terminal', termId: 'pty-1' },
        leaf('l-b', 'b'),
      ]),
    )
    seedApi([{ key: 'a' }, { key: 'b' }])
    renderGrid('a')

    expect(await screen.findByText('Terminal pane — coming in Phase 2')).toBeTruthy()
  })

  it('renders a picker for a session leaf that carries no slot', async () => {
    seedStore(
      'a',
      splitOf([leaf('l-a', 'a'), { type: 'leaf', id: 'l-x', kind: 'session' }, leaf('l-b', 'b')]),
    )
    seedApi([{ key: 'a' }, { key: 'b' }])
    renderGrid('a')

    await screen.findByTestId('pane-a')
    expect(pickers()).toHaveLength(1)
  })

  it('splits a session pane right and down from its own controls', async () => {
    renderGrid('a')
    await screen.findByTestId('pane-a')

    fireEvent.click(screen.getByLabelText('right a'))
    await waitFor(() => expect(pickers()).toHaveLength(2))

    fireEvent.click(screen.getByLabelText('down a'))
    await waitFor(() => expect(pickers()).toHaveLength(3))
  })
})

describe('SessionGridView — picker list', () => {
  it('excludes sessions already pinned in a pane', async () => {
    seedApi([{ key: 'a', title: 'Alpha' }, { key: 'b', title: 'Bravo' }])
    renderGrid('a')

    await waitFor(() => expect(rowsOf(onlyPicker())).toHaveLength(1))
    expect(rowsOf(onlyPicker())[0].textContent).toContain('Bravo')
  })

  it('orders approval-waiting first, then running, then most recent', async () => {
    seedApi([
      { key: 'idle', title: 'Idle one', last_activity_ts: '2026-08-01T00:00:00' },
      { key: 'run', title: 'Running one', running: true },
      { key: 'appr', title: 'Approval one', pending_approval: true },
      { key: 'stale', title: 'Older one', last_activity_ts: '2026-07-01T00:00:00' },
    ])
    renderGrid(null)

    await waitFor(() => expect(rowsOf(onlyPicker())).toHaveLength(4))
    const titles = rowsOf(onlyPicker()).map((r) => r.textContent)
    expect(titles[0]).toContain('Approval one')
    expect(titles[1]).toContain('Running one')
    expect(titles[2]).toContain('Idle one')
    expect(titles[3]).toContain('Older one')
  })

  it('ranks a session waiting on your answer with the approvals, above running', async () => {
    // Both are things the user owes the session, and this list is where a pane's
    // session gets picked — so the ones that cannot advance come first, whatever
    // their last activity says.
    seedApi([
      { key: 'run', title: 'Running one', running: true, last_activity_ts: '2026-08-02T00:00:00' },
      { key: 'ask', title: 'Asking one', needs_input: true, last_activity_ts: '2026-07-01T00:00:00' },
      { key: 'appr', title: 'Approval one', pending_approval: true },
    ])
    renderGrid(null)

    await waitFor(() => expect(rowsOf(onlyPicker())).toHaveLength(3))
    const rows = rowsOf(onlyPicker())
    expect(rows[0].textContent).toContain('Approval one')
    expect(rows[1].textContent).toContain('Asking one')
    expect(rows[2].textContent).toContain('Running one')
    // Its dot is the info one: distinct from the warn approval above it and from
    // the ok "running" below, which is the state it would otherwise be read as.
    expect(rows[1].querySelector('svg')?.getAttribute('class')).toContain('fill-info')
  })

  it('marks each row status on its dot and shows the message count', async () => {
    seedApi([
      { key: 'appr', title: 'Approval one', pending_approval: true, messages: 7 },
      { key: 'run', title: 'Running one', running: true, messages: 3 },
      { key: 'idle', title: 'Idle one' },
    ])
    renderGrid(null)

    await waitFor(() => expect(rowsOf(onlyPicker())).toHaveLength(3))
    const rows = rowsOf(onlyPicker())
    expect(rows[0].querySelector('svg')?.getAttribute('class')).toContain('fill-warn')
    expect(rows[1].querySelector('svg')?.getAttribute('class')).toContain('fill-ok')
    expect(rows[2].querySelector('svg')?.getAttribute('class')).toContain('fill-muted')
    expect(rows[0].textContent).toContain('7 msgs')
    expect(rows[2].textContent).toContain('0 msgs')
  })

  it('falls back to the slot key when a session has no title', async () => {
    seedApi([{ key: 'untitled-slot' }])
    renderGrid(null)

    await waitFor(() => expect(rowsOf(onlyPicker())).toHaveLength(1))
    expect(rowsOf(onlyPicker())[0].textContent).toContain('untitled-slot')
  })

  it('filters by title, key and agent', async () => {
    seedApi([
      { key: 'a1', title: 'Alpha', agent: 'kirocrew' },
      { key: 'b2', title: 'Bravo', agent: 'reviewer' },
    ])
    renderGrid(null)
    await waitFor(() => expect(rowsOf(onlyPicker())).toHaveLength(2))
    const input = screen.getByPlaceholderText('Search sessions…')

    fireEvent.change(input, { target: { value: 'brav' } })
    expect(rowsOf(onlyPicker())).toHaveLength(1)

    fireEvent.change(input, { target: { value: 'a1' } })
    expect(rowsOf(onlyPicker())[0].textContent).toContain('Alpha')

    fireEvent.change(input, { target: { value: 'reviewer' } })
    expect(rowsOf(onlyPicker())[0].textContent).toContain('Bravo')
  })

  it('shows the empty state when nothing matches the search', async () => {
    seedApi([{ key: 'a1', title: 'Alpha' }])
    renderGrid(null)
    await waitFor(() => expect(rowsOf(onlyPicker())).toHaveLength(1))

    fireEvent.change(screen.getByPlaceholderText('Search sessions…'), {
      target: { value: 'nothing-like-this' },
    })

    expect(screen.getByText('No matching sessions')).toBeTruthy()
    expect(rowsOf(onlyPicker())).toHaveLength(0)
  })

  it('pins the picked session into that cell', async () => {
    seedApi([{ key: 'b', title: 'Bravo' }])
    renderGrid('a')
    await waitFor(() => expect(rowsOf(onlyPicker())).toHaveLength(1))

    fireEvent.click(rowsOf(onlyPicker())[0])

    expect(await screen.findByTestId('pane-b')).toBeTruthy()
    expect(pickers()).toHaveLength(0)
  })
})

describe('SessionGridView — picker controls', () => {
  it('creates a session and pins it into the cell', async () => {
    const m = seedApi([{ key: 'a' }, { key: 'zeta', title: 'Zeta' }])
    renderGrid('a')
    // Wait for the first slot payload: the view's heal-once prune runs off it and
    // drops any session pane the list does not name (see the note on the fork test).
    await waitFor(() => expect(rowsOf(onlyPicker())).toHaveLength(1))

    fireEvent.click(within(onlyPicker()).getByRole('button', { name: 'New session' }))

    expect(await screen.findByTestId('pane-created-1')).toBeTruthy()
    expect(m.createChatSlot).toHaveBeenCalled()
  })

  it('leaves the cell empty when the created session has no key', async () => {
    const m = seedApi([{ key: 'a' }])
    m.createChatSlot = vi.fn().mockResolvedValue({})
    renderGrid('a')

    fireEvent.click(within(onlyPicker()).getByRole('button', { name: 'New session' }))

    await waitFor(() => expect(m.createChatSlot).toHaveBeenCalled())
    expect(pickers()).toHaveLength(1)
  })

  it('disables the create button and shows a spinner while creating', async () => {
    const m = seedApi([{ key: 'a' }])
    let release: (v: { key?: string }) => void = () => {}
    m.createChatSlot = vi.fn().mockReturnValue(new Promise((res) => { release = res }))
    renderGrid('a')
    const create = within(onlyPicker()).getByRole('button', { name: 'New session' })

    fireEvent.click(create)

    await waitFor(() => expect((create as HTMLButtonElement).disabled).toBe(true))
    expect(onlyPicker().querySelector('.animate-spin')).toBeTruthy()
    release({ key: 'created-1' })
    expect(await screen.findByTestId('pane-created-1')).toBeTruthy()
  })

  it('forks the focused session and pins the child', async () => {
    const m = seedApi([{ key: 'a' }, { key: 'zeta', title: 'Zeta' }])
    renderGrid('a')
    // The wait is load-bearing, not politeness: the heal-once prune fires on the
    // first non-empty slot payload, and a payload that predates the fork does not
    // name the child — so a fork picked before it lands is pruned straight back out.
    await waitFor(() => expect(rowsOf(onlyPicker())).toHaveLength(1))

    fireEvent.click(within(onlyPicker()).getByRole('button', { name: 'Fork' }))

    expect(await screen.findByTestId('pane-forked-1')).toBeTruthy()
    expect(m.forkChatSlot).toHaveBeenCalledWith('a')
  })

  it('leaves the cell empty when the fork is refused', async () => {
    const m = seedApi([{ key: 'a' }])
    m.forkChatSlot = vi.fn().mockResolvedValue({ ok: false, key: 'forked-1' })
    renderGrid('a')

    fireEvent.click(within(onlyPicker()).getByRole('button', { name: 'Fork' }))

    await waitFor(() => expect(m.forkChatSlot).toHaveBeenCalled())
    expect(screen.queryByTestId('pane-forked-1')).toBeNull()
  })

  it('disables the fork button while no session is in the grid', async () => {
    renderGrid(null)

    const fork = within(onlyPicker()).getByRole('button', { name: 'Fork' }) as HTMLButtonElement
    expect(fork.disabled).toBe(true)
    expect(fork.getAttribute('title')).toBe('No session to fork yet')
  })

  it('names the fork source by title, falling back to its slot key', async () => {
    seedApi([{ key: 'a', title: 'Alpha' }])
    const { unmount } = renderGrid('a')

    await waitFor(() =>
      expect(within(onlyPicker()).getByRole('button', { name: 'Fork' }).getAttribute('title')).toBe(
        'Fork Alpha (child session)',
      ),
    )

    unmount()
    localStorage.clear()
    seedApi([])
    renderGrid('a')
    expect(within(onlyPicker()).getByRole('button', { name: 'Fork' }).getAttribute('title')).toBe(
      'Fork a (child session)',
    )
  })

  it('forks the pane that holds focus, not merely the first one', async () => {
    seedStore('a', splitOf([leaf('l-a', 'a'), leaf('l-b', 'b'), leaf('l-p')]))
    seedApi([{ key: 'a', title: 'Alpha' }, { key: 'b', title: 'Bravo' }])
    renderGrid('a')
    await screen.findByTestId('pane-b')

    fireEvent.click(screen.getByLabelText('focus b'))

    await waitFor(() =>
      expect(within(onlyPicker()).getByRole('button', { name: 'Fork' }).getAttribute('title')).toBe(
        'Fork Bravo (child session)',
      ),
    )
  })

  it('splits the picker cell right and down from its own controls', async () => {
    renderGrid('a')
    const picker = onlyPicker()

    fireEvent.click(within(picker).getByLabelText('Split right'))
    await waitFor(() => expect(pickers()).toHaveLength(2))

    fireEvent.click(within(pickers()[0]).getByLabelText('Split down'))
    await waitFor(() => expect(pickers()).toHaveLength(3))
  })

  it('focuses the picker cell it is pressed in', async () => {
    renderGrid('a')
    await screen.findByTestId('pane-a')
    fireEvent.click(screen.getByLabelText('focus a'))
    await waitFor(() => expect(screen.getByTestId('pane-a').getAttribute('data-focused')).toBe('yes'))

    fireEvent.mouseDown(onlyPicker())

    await waitFor(() => expect(screen.getByTestId('pane-a').getAttribute('data-focused')).toBe('no'))
  })
})
