import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../test/helpers'
import SlotTagPopover from './SlotTagPopover'
import { sseSlots } from '../store/dashboardSlice'
import { api } from '../api/client'
import { isTouchDevice } from '../utils/isTouchDevice'
import type { ChatSlot, ChatTag } from '../types'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: { ...mod.api, chatTags: vi.fn(), setSlotTags: vi.fn(), createChatTag: vi.fn() },
  }
})
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: vi.fn(() => false) }))

const popover = vi.hoisted(() => ({ slotKey: 'zzq-slot' as string | null, close: vi.fn() }))
vi.mock('../hooks/useTagPopover', () => ({ useTagPopover: () => popover }))

const chatTags = vi.mocked(api.chatTags)
const setSlotTags = vi.mocked(api.setSlotTags)
const createChatTag = vi.mocked(api.createChatTag)

const TAGS: ChatTag[] = [
  { id: 't2', name: 'zzq-beta', color: '#222', order: 2 } as ChatTag,
  { id: 't1', name: 'zzq-alpha', color: '#111', order: 1 } as ChatTag,
]

function mount(
  slotTags: string[] = [],
  extraSlots: ChatSlot[] = [],
  tagsRevision?: string,
) {
  const store = createTestStore()
  store.dispatch(sseSlots([
    { key: 'zzq-slot', messages: 0, running: false, tags: slotTags, tags_revision: tagsRevision } as ChatSlot,
    ...extraSlots,
  ]))
  return renderWithProviders(<SlotTagPopover />, { store })
}

const options = () => screen.getAllByRole('menuitemcheckbox')

describe('SlotTagPopover', () => {
  beforeEach(() => {
    popover.slotKey = 'zzq-slot'
    popover.close.mockReset()
    vi.mocked(isTouchDevice).mockReturnValue(false)
    chatTags.mockReset()
    chatTags.mockResolvedValue(TAGS as never)
    setSlotTags.mockReset()
    setSlotTags.mockResolvedValue(undefined as never)
    createChatTag.mockReset()
    createChatTag.mockResolvedValue(undefined as never)
  })

  it('renders nothing when no slot has the picker open', () => {
    popover.slotKey = null
    const { container } = mount()
    expect(container.firstChild).toBeNull()
    expect(chatTags).not.toHaveBeenCalled()
  })

  it('lists tags in order and reflects the slot assignment', async () => {
    mount(['t2'])
    await screen.findByText('zzq-alpha')
    expect(options().map(o => o.textContent)).toEqual(['zzq-alpha', 'zzq-beta'])
    expect(options()[0].getAttribute('aria-checked')).toBe('false')
    expect(options()[1].getAttribute('aria-checked')).toBe('true')
  })

  it('shows the empty hint when there are no tags at all', async () => {
    chatTags.mockResolvedValue([] as never)
    mount()
    expect(await screen.findByText('No tags yet. Create one below.')).toBeInTheDocument()
  })

  it('the deferred focus lands on the first option, and is skipped on touch', async () => {
    // The focus is deferred a tick so the list has painted. Switching slots with
    // the tag list already cached is the case where options exist immediately.
    const { rerender } = mount()
    await screen.findByText('zzq-alpha')
    document.body.focus()

    popover.slotKey = 'zzq-slot-2'
    rerender(<SlotTagPopover />)
    await waitFor(() => expect(document.activeElement).toBe(options()[0]))

    vi.mocked(isTouchDevice).mockReturnValue(true)
    document.body.focus()
    popover.slotKey = 'zzq-slot-3'
    rerender(<SlotTagPopover />)
    await new Promise(r => setTimeout(r, 5))
    expect(document.activeElement).not.toBe(options()[0])
  })

  it('toggling a tag on writes the extended list optimistically', async () => {
    mount()
    await screen.findByText('zzq-alpha')
    fireEvent.click(options()[0])
    expect(options()[0].getAttribute('aria-checked')).toBe('true')
    await waitFor(() =>
      expect(setSlotTags).toHaveBeenCalledWith('zzq-slot', ['t1']))
  })

  it('toggling an assigned tag off removes it', async () => {
    mount(['t1'])
    await screen.findByText('zzq-alpha')
    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledWith('zzq-slot', []))
  })

  it('a rapid burst composes onto the newest pending list', async () => {
    mount()
    await screen.findByText('zzq-alpha')
    await act(async () => {
      fireEvent.click(options()[0])
      fireEvent.click(options()[1])
    })
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    expect(setSlotTags.mock.calls[1][1]).toEqual(['t1', 't2'])
  })

  it('keeps the checkmark when PUT settles before the slots frame', async () => {
    let finish!: (value: unknown) => void
    setSlotTags.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as never)
    const { store } = mount()
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0])
    expect(options()[0].getAttribute('aria-checked')).toBe('true')
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())

    await act(async () => { finish({ ok: true, tags: ['t1'] }) })
    expect(options()[0].getAttribute('aria-checked')).toBe('true')
    expect(store.getState().dashboard.slots[0].tags).toEqual([])

    // A pre-PUT frame can arrive after the HTTP response. It must not expose
    // the old Redux value while the authoritative confirmation is pending.
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: [] } as ChatSlot,
      ]))
    })
    expect(options()[0].getAttribute('aria-checked')).toBe('true')

    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1'] } as ChatSlot,
      ]))
    })
    await waitFor(() => expect(store.getState().dashboard.slots[0].tags).toEqual(['t1']))
    expect(options()[0].getAttribute('aria-checked')).toBe('true')

    // Once confirmed, a later authoritative update is visible rather than
    // being hidden forever behind the optimistic overlay.
    await act(async () => {})
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: [] } as ChatSlot,
      ]))
    })
    await waitFor(() => expect(options()[0].getAttribute('aria-checked')).toBe('false'))
  })

  it('a newer revision retires the overlay even when tags reverse to the baseline', async () => {
    let finish!: (value: unknown) => void
    setSlotTags.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as never)
    const { store } = mount([], [], 'revision-1')
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    await act(async () => {
      finish({ ok: true, tags: ['t1'], tags_revision: 'revision-2' })
    })

    // A delayed pre-PUT frame has the baseline revision and must not flicker.
    act(() => {
      store.dispatch(sseSlots([
        {
          key: 'zzq-slot', messages: 0, running: false, tags: [],
          tags_revision: 'revision-1',
        } as ChatSlot,
      ]))
    })
    expect(options()[0].getAttribute('aria-checked')).toBe('true')

    // A later client can legitimately restore the same tag list. Its distinct
    // revision proves this is a newer authoritative reversal, not the stale frame.
    act(() => {
      store.dispatch(sseSlots([
        {
          key: 'zzq-slot', messages: 0, running: false, tags: [],
          tags_revision: 'revision-3',
        } as ChatSlot,
      ]))
    })
    await waitFor(() => expect(options()[0].getAttribute('aria-checked')).toBe('false'))

    setSlotTags.mockResolvedValueOnce({
      ok: true, tags: ['t2'], tags_revision: 'revision-4',
    } as never)
    fireEvent.click(options()[1])
    await waitFor(() => expect(setSlotTags).toHaveBeenLastCalledWith('zzq-slot', ['t2']))
  })

  it('cancels later queued intents after a predecessor write fails', async () => {
    let rejectFirst!: (reason: unknown) => void
    setSlotTags.mockImplementationOnce(() => new Promise((_resolve, reject) => {
      rejectFirst = reject
    }) as never)
    mount()
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0])
    fireEvent.click(options()[1])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())

    await act(async () => { rejectFirst(new Error('first write failed')) })
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'false'])
    })
    await act(async () => {})
    expect(setSlotTags).toHaveBeenCalledOnce()

    // The failed chain is removed after its queued intents are discarded, so a
    // deliberate retry starts a fresh chain from the accepted server state.
    fireEvent.click(options()[1])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    expect(setSlotTags).toHaveBeenLastCalledWith('zzq-slot', ['t2'])
  })

  it('keeps an unseen server predecessor frame beneath the latest committed overlay', async () => {
    let finish!: (value: unknown) => void
    setSlotTags.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as never)
    const { store } = mount([], [], 'revision-1')
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())

    // Another writer commits revision 2 after this client captured revision 1.
    // Its frame lands before this client's queued PUT response is observed.
    act(() => {
      store.dispatch(sseSlots([
        {
          key: 'zzq-slot', messages: 0, running: false, tags: ['t2'],
          tags_revision: 'revision-2',
        } as ChatSlot,
      ]))
    })
    await act(async () => {
      finish({
        ok: true,
        tags: ['t1'],
        tags_revision: 'revision-3',
        prior_tags_revision: 'revision-2',
      })
    })

    // Revision 2 is the server-declared predecessor of the committed revision 3,
    // so it must not replace the latest optimistic intent while revision 3 travels.
    expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'false'])

    act(() => {
      store.dispatch(sseSlots([
        {
          key: 'zzq-slot', messages: 0, running: false, tags: ['t1'],
          tags_revision: 'revision-3',
        } as ChatSlot,
      ]))
    })
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'false'])
    })

    setSlotTags.mockResolvedValueOnce({
      ok: true, tags: ['t1', 't2'], tags_revision: 'revision-4',
      prior_tags_revision: 'revision-3',
    } as never)
    fireEvent.click(options()[1])
    await waitFor(() => {
      expect(setSlotTags).toHaveBeenLastCalledWith('zzq-slot', ['t1', 't2'])
    })
  })

  it('a newer authoritative frame retires the overlay and becomes the next toggle base', async () => {
    let finish!: (value: unknown) => void
    setSlotTags.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as never)
    const { store } = mount()
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    await act(async () => { finish({ ok: true, tags: ['t1'] }) })

    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t2'] } as ChatSlot,
      ]))
    })
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'true'])
    })

    setSlotTags.mockResolvedValueOnce({ ok: true, tags: ['t2', 't1'] } as never)
    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenLastCalledWith('zzq-slot', ['t2', 't1']))
  })

  it('keeps a newer authoritative frame as fallback after an older PUT settles', async () => {
    let finishFirst!: (value: unknown) => void
    setSlotTags
      .mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve }) as never)
      .mockRejectedValueOnce(new Error('newer write failed'))
    const { store } = mount()
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())

    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t2'] } as ChatSlot,
      ]))
    })
    await act(async () => { finishFirst({ ok: true, tags: ['t1'] }) })
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'true'])
    })

    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    expect(setSlotTags).toHaveBeenLastCalledWith('zzq-slot', ['t2', 't1'])
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'true'])
    })
  })

  it('serializes rapid writes so the latest desired list reaches the server last', async () => {
    const finish: Array<(value: unknown) => void> = []
    setSlotTags.mockImplementation(() => new Promise(resolve => { finish.push(resolve) }) as never)
    const { store } = mount()
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0])
    fireEvent.click(options()[1])
    await waitFor(() => expect(finish).toHaveLength(1))
    expect(setSlotTags).toHaveBeenCalledOnce()
    expect(setSlotTags.mock.calls[0][1]).toEqual(['t1'])

    await act(async () => { finish[0]({ ok: true, tags: ['t1'] }) })
    await waitFor(() => expect(finish).toHaveLength(2))
    expect(setSlotTags.mock.calls[1][1]).toEqual(['t1', 't2'])

    await act(async () => { finish[1]({ ok: true, tags: ['t1', 't2'] }) })
    expect(store.getState().dashboard.slots[0].tags).toEqual([])
    expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'true'])

    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1', 't2'] } as ChatSlot,
      ]))
    })
    await waitFor(() => expect(store.getState().dashboard.slots[0].tags).toEqual(['t1', 't2']))
  })

  it('does not show a settled slot failure on a different open slot', async () => {
    let rejectFirst!: (reason: unknown) => void
    setSlotTags.mockImplementationOnce(() => new Promise((_resolve, reject) => {
      rejectFirst = reject
    }) as never)
    const { rerender } = mount([], [
      { key: 'zzq-slot-2', messages: 0, running: false, tags: [] } as ChatSlot,
    ])
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    popover.slotKey = 'zzq-slot-2'
    rerender(<SlotTagPopover />)
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'false'])
    })

    await act(async () => { rejectFirst(new Error('slot A failed')) })
    await act(async () => {})
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('reseeds rollback state when switching away from another pending slot', async () => {
    let finishFirst!: (value: unknown) => void
    setSlotTags
      .mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve }) as never)
      .mockRejectedValueOnce(new Error('slot B write failed'))
    const { store, rerender } = mount([], [
      {
        key: 'zzq-slot-2', messages: 0, running: false, tags: ['t1'],
        tags_revision: 'slot-b-revision-1',
      } as ChatSlot,
    ], 'slot-a-revision-1')
    await screen.findByText('zzq-alpha')

    // Visit B once so its old accepted state is cached, then leave A pending.
    popover.slotKey = 'zzq-slot-2'
    rerender(<SlotTagPopover />)
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'false'])
    })
    popover.slotKey = 'zzq-slot'
    rerender(<SlotTagPopover />)
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'false'])
    })
    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    expect(finishFirst).toBeTypeOf('function')

    // B changes while A's intent still occupies pendingRef. Opening B must seed
    // this newer state before clearing A's pending overlay.
    act(() => {
      store.dispatch(sseSlots([
        {
          key: 'zzq-slot', messages: 0, running: false, tags: [],
          tags_revision: 'slot-a-revision-1',
        } as ChatSlot,
        {
          key: 'zzq-slot-2', messages: 0, running: false, tags: ['t2'],
          tags_revision: 'slot-b-revision-2',
        } as ChatSlot,
      ]))
    })
    popover.slotKey = 'zzq-slot-2'
    rerender(<SlotTagPopover />)
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'true'])
    })

    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    expect(setSlotTags).toHaveBeenLastCalledWith('zzq-slot-2', ['t2', 't1'])
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'true'])
    })
  })

  it('a stalled write in one slot does not block another slot', async () => {
    let finishFirst!: (value: unknown) => void
    setSlotTags
      .mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve }) as never)
      .mockResolvedValueOnce({ ok: true, tags: ['t2'] } as never)
    const { rerender } = mount([], [
      { key: 'zzq-slot-2', messages: 0, running: false, tags: [] } as ChatSlot,
    ])
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    expect(finishFirst).toBeTypeOf('function')

    popover.slotKey = 'zzq-slot-2'
    rerender(<SlotTagPopover />)
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'false'])
    })
    fireEvent.click(options()[1])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    expect(setSlotTags.mock.calls[1]).toEqual(['zzq-slot-2', ['t2']])
  })

  it('a failed latest rapid write falls back to the preceding successful write', async () => {
    const writes: Array<{
      resolve: (value: unknown) => void
      reject: (reason: unknown) => void
    }> = []
    setSlotTags.mockImplementation(() => new Promise((resolve, reject) => {
      writes.push({ resolve, reject })
    }) as never)
    const { store } = mount()
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0])
    fireEvent.click(options()[1])
    await waitFor(() => expect(writes).toHaveLength(1))
    await act(async () => { writes[0].resolve({ ok: true, tags: ['t1'] }) })
    await waitFor(() => expect(writes).toHaveLength(2))
    await act(async () => { writes[1].reject(new Error('second write failed')) })

    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'false'])
    })
    expect(store.getState().dashboard.slots[0].tags).toEqual([])

    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1'] } as ChatSlot,
      ]))
    })
    await waitFor(() => expect(store.getState().dashboard.slots[0].tags).toEqual(['t1']))
  })

  it('restores authoritative slot tags when the latest write fails', async () => {
    setSlotTags.mockRejectedValueOnce(new Error('write failed'))
    mount()
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0])
    expect(options()[0].getAttribute('aria-checked')).toBe('true')
    await waitFor(() => expect(options()[0].getAttribute('aria-checked')).toBe('false'))

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('write failed')
    expect(screen.queryByRole('button', { name: /ask.*agent/i })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('roving focus walks the option list and wraps at both ends', async () => {
    mount()
    await screen.findByText('zzq-alpha')
    const list = screen.getByRole('menu')
    const opts = options()

    opts[0].focus()
    fireEvent.keyDown(list, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(opts[1])
    fireEvent.keyDown(list, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(opts[0])
    fireEvent.keyDown(list, { key: 'ArrowUp' })
    expect(document.activeElement).toBe(opts[opts.length - 1])
    fireEvent.keyDown(list, { key: 'Home' })
    expect(document.activeElement).toBe(opts[0])
    fireEvent.keyDown(list, { key: 'End' })
    expect(document.activeElement).toBe(opts[opts.length - 1])
  })

  it('an unhandled key in the list leaves focus alone', async () => {
    mount()
    await screen.findByText('zzq-alpha')
    const opts = options()
    opts[0].focus()
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Tab' })
    expect(document.activeElement).toBe(opts[0])
  })

  it('the roving handler no-ops when the list has no options', async () => {
    chatTags.mockResolvedValue([] as never)
    mount()
    await screen.findByText('No tags yet. Create one below.')
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'ArrowDown' })
    expect(popover.close).not.toHaveBeenCalled()
  })

  it('the backdrop closes on click and on Enter/Space/Escape, but not from inside', async () => {
    mount()
    await screen.findByText('zzq-alpha')
    const backdrop = screen.getByLabelText('Close tag picker')

    fireEvent.click(backdrop)
    expect(popover.close).toHaveBeenCalledTimes(1)
    for (const key of ['Enter', ' ', 'Escape']) fireEvent.keyDown(backdrop, { key })
    expect(popover.close).toHaveBeenCalledTimes(4)

    // A click and a key from within the dialog must NOT dismiss.
    fireEvent.click(screen.getByTestId('slot-tag-picker'))
    fireEvent.keyDown(options()[0], { key: 'Enter' })
    expect(popover.close).toHaveBeenCalledTimes(4)
  })

  it('an unrelated key on the backdrop does nothing', async () => {
    mount()
    await screen.findByText('zzq-alpha')
    fireEvent.keyDown(screen.getByLabelText('Close tag picker'), { key: 'a' })
    expect(popover.close).not.toHaveBeenCalled()
  })

  it('Escape inside the dialog closes it, and the X button too', async () => {
    mount()
    await screen.findByText('zzq-alpha')
    fireEvent.keyDown(screen.getByTestId('slot-tag-picker'), { key: 'Escape' })
    expect(popover.close).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByLabelText('Close'))
    expect(popover.close).toHaveBeenCalledTimes(2)
  })

  it('Enter in the new-tag input creates the tag and clears the field', async () => {
    mount()
    await screen.findByText('zzq-alpha')
    const input = screen.getByPlaceholderText('New tag…') as HTMLInputElement

    input.focus()
    fireEvent.change(input, { target: { value: '  zzq-new  ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(createChatTag).toHaveBeenCalledWith('zzq-new'))
    expect(input.value).toBe('')
  })

  it('an empty new-tag name creates nothing', async () => {
    mount()
    await screen.findByText('zzq-alpha')
    const input = screen.getByPlaceholderText('New tag…') as HTMLInputElement
    input.focus()
    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(createChatTag).not.toHaveBeenCalled()
  })

  it('Escape in the new-tag input closes the picker', async () => {
    mount()
    await screen.findByText('zzq-alpha')
    const input = screen.getByPlaceholderText('New tag…')
    input.focus()
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(popover.close).toHaveBeenCalled()
  })
})
