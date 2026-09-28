import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../test/helpers'
import SlotTagPopover, { compareRevisions, shouldSeedAcceptedSnapshot } from './SlotTagPopover'
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
// A PUT is (slot, tags[, baseTagsRevision]); most assertions care only about
// what was written, not which revision it was composed onto.
const writes = () => setSlotTags.mock.calls.map(call => call.slice(0, 2))
const lastWrite = () => writes().at(-1)
const lastWriteBase = () => setSlotTags.mock.lastCall?.[2]

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
      expect(writes()).toContainEqual(['zzq-slot', ['t1']]))
  })

  it('toggling an assigned tag off removes it', async () => {
    mount(['t1'])
    await screen.findByText('zzq-alpha')
    fireEvent.click(options()[0])
    await waitFor(() => expect(writes()).toContainEqual(['zzq-slot', []]))
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

  it('treats a leaked provisional revision named by the 409 as stale, not as a newer writer', async () => {
    // Server: provisional revision-2 sits on the live slot while the save
    // awaits; an unrelated broadcast leaks it; the save is then refused and
    // rolled back. The 409 names revision-2 as rejected.
    let reject!: (error: unknown) => void
    setSlotTags
      .mockImplementationOnce(() => new Promise((_resolve, rej) => { reject = rej }) as never)
      .mockResolvedValueOnce({
        ok: true, tags: ['t2'], tags_revision: 'revision-3', prior_tags_revision: 'revision-1',
      } as never)
    const { store } = mount([], [], 'revision-1')
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0]) // t1
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())

    // The leaked frame carries the provisional revision and the rejected tags.
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1'], tags_revision: 'revision-2' } as ChatSlot,
      ]))
    })
    await act(async () => {
      reject(Object.assign(new Error('session was deleted or rebound'), {
        status: 409,
        body: JSON.stringify({
          error: 'session was deleted or rebound',
          code: 'session_gone',
          rejected_tags_revision: 'revision-2',
          tags_revision: 'revision-1',
        }),
      }))
    })
    // Rolled back: the rejected tag is NOT shown as accepted, and the notice
    // is visible.
    await waitFor(() => expect(options()[0].getAttribute('aria-checked')).toBe('false'))
    expect(screen.getByRole('alert')).toHaveTextContent('session was deleted or rebound')

    // The rollback frame (revision-1, []) lands; still rolled back.
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: [], tags_revision: 'revision-1' } as ChatSlot,
      ]))
    })
    await waitFor(() => expect(options()[0].getAttribute('aria-checked')).toBe('false'))

    // A deliberate new click composes from the rolled-back list, not from the
    // rejected ['t1'] the leaked frame showed.
    fireEvent.click(options()[1]) // t2
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    expect(lastWrite()).toEqual(['zzq-slot', ['t2']])
  })

  it('records rejected lineage even when the failure lands while another slot is open', async () => {
    let reject!: (error: unknown) => void
    setSlotTags
      .mockImplementationOnce(() => new Promise((_resolve, rej) => { reject = rej }) as never)
      .mockResolvedValueOnce({
        ok: true, tags: ['t2'], tags_revision: 'revision-3', prior_tags_revision: 'revision-1',
      } as never)
    const { store, rerender } = mount([], [
      { key: 'zzq-other', messages: 0, running: false, tags: [], tags_revision: 'other-1' } as ChatSlot,
    ], 'revision-1')
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0]) // t1 on zzq-slot
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())

    // Provisional revision-2 leaks, then the user opens ANOTHER slot's picker
    // before the save is refused.
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1'], tags_revision: 'revision-2' } as ChatSlot,
        { key: 'zzq-other', messages: 0, running: false, tags: [], tags_revision: 'other-1' } as ChatSlot,
      ]))
    })
    popover.slotKey = 'zzq-other'
    rerender(<SlotTagPopover />)
    await act(async () => {
      reject(Object.assign(new Error('session was deleted or rebound'), {
        status: 409,
        body: JSON.stringify({
          error: 'session was deleted or rebound', code: 'session_gone',
          rejected_tags_revision: 'revision-2', tags_revision: 'revision-1',
        }),
      }))
    })

    // Reopening the original slot while the store still shows the leaked
    // revision-2 frame must NOT promote the rejected ['t1'] as accepted.
    popover.slotKey = 'zzq-slot'
    rerender(<SlotTagPopover />)
    await waitFor(() => expect(options()[0].getAttribute('aria-checked')).toBe('false'))

    // And the next toggle composes from the rolled-back list, not from ['t1'].
    fireEvent.click(options()[1]) // t2
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    expect(lastWrite()).toEqual(['zzq-slot', ['t2']])
  })

  it('adopts a concurrent writer frame even when a newer click already owns the overlay', async () => {
    // Tab A: click t1 (PUT1 in flight). Tab B commits ['t2'] (foreign
    // revision). Tab A clicks again BEFORE PUT1 responds. PUT1's success path
    // must still adopt B's frame as the accepted base, so A's queued delta
    // composes onto ['t2'] and does not silently drop B's tag.
    let finishFirst!: (value: unknown) => void
    setSlotTags
      .mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve }) as never)
      .mockResolvedValueOnce({
        ok: true, tags: ['t2'], tags_revision: 'revision-9', prior_tags_revision: 'revision-B',
      } as never)
    const { store } = mount([], [], 'revision-1')
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0]) // t1 -> display ['t1'], PUT1 in flight
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())

    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t2'], tags_revision: 'revision-B' } as ChatSlot,
      ]))
    })
    fireEvent.click(options()[0]) // t1 again -> display [] ; newer click owns overlay
    expect(setSlotTags).toHaveBeenCalledOnce()

    await act(async () => {
      finishFirst({ ok: true, tags: ['t1'], tags_revision: 'revision-2', prior_tags_revision: 'revision-1' })
    })
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    // Delta "remove t1" applied to B's adopted ['t2'] keeps B's tag.
    expect(lastWrite()).toEqual(['zzq-slot', ['t2']])
  })

  it('a write completing while another slot is open does not cache that slot under its target', async () => {
    let finish!: (value: unknown) => void
    setSlotTags
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as never)
      .mockResolvedValueOnce({
        ok: true, tags: ['t1', 't2'], tags_revision: 'revision-3', prior_tags_revision: 'revision-2',
      } as never)
    const { rerender } = mount([], [
      { key: 'zzq-other', messages: 0, running: false, tags: ['t2'], tags_revision: 'other-1' } as ChatSlot,
    ], 'revision-1')
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0]) // t1 on zzq-slot, PUT in flight
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())

    // Picker moves to another slot (whose store tags are ['t2']) before the
    // write for zzq-slot completes.
    popover.slotKey = 'zzq-other'
    rerender(<SlotTagPopover />)
    await act(async () => {
      finish({ ok: true, tags: ['t1'], tags_revision: 'revision-2', prior_tags_revision: 'revision-1' })
    })

    // Reopen the original slot: its store frame is still revision-1 (a known
    // predecessor), so the accepted ['t1'] overlays it — NOT the other slot's
    // ['t2'] that was on screen when the write completed.
    popover.slotKey = 'zzq-slot'
    rerender(<SlotTagPopover />)
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'false'])
    })
    fireEvent.click(options()[1]) // t2 -> composes onto zzq-slot's ['t1']
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    expect(lastWrite()).toEqual(['zzq-slot', ['t1', 't2']])
  })

  it('does not promote a delayed older snapshot with an unseen orderable revision', async () => {
    // Server revisions are "<8 hex epoch>.<20-digit sequence>-<hex>"; within
    // one epoch (one gateway process) the sequence orders them.
    const R = (n: number, epoch = '0000000000000001') => `${epoch}.${String(n).padStart(20, '0')}-ab12cd34`
    setSlotTags
      .mockResolvedValueOnce({
        ok: true, tags: ['t1'], tags_revision: R(2), prior_tags_revision: R(1),
      } as never)
      .mockResolvedValueOnce({
        ok: true, tags: ['t1', 't2'], tags_revision: R(3), prior_tags_revision: R(2),
      } as never)
    const { store } = mount([], [], R(1))
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0]) // t1
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    // The confirming WebSocket frame (R2) retires the overlay: accepted=R2.
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1'], tags_revision: R(2) } as ChatSlot,
      ]))
    })
    await waitFor(() => expect(options()[0].getAttribute('aria-checked')).toBe('true'))

    // A delayed HTTP snapshot X arrives: revision R0 — OLDER than anything
    // accepted, and never seen by this client (not in the predecessor set).
    // It must not be promoted as authoritative.
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: [], tags_revision: R(0) } as ChatSlot,
      ]))
    })
    expect(options()[0].getAttribute('aria-checked')).toBe('true')

    // The next toggle composes onto the accepted ['t1'], not onto X's [].
    fireEvent.click(options()[1]) // t2
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    expect(lastWrite()).toEqual(['zzq-slot', ['t1', 't2']])
  })

  it('adopts the restarted gateway first frame despite its lower sequence (higher epoch)', async () => {
    // After a restart the gateway mints under a NEW epoch with sequences that
    // start over. Those revisions are not comparable to the cached ones and
    // must be adopted as the new authoritative lineage — never ignored as
    // "older" because their sequence is smaller.
    const R = (n: number, epoch: string) => `${epoch}.${String(n).padStart(20, '0')}-ab12cd34`
    setSlotTags
      .mockResolvedValueOnce({
        ok: true, tags: ['t1'], tags_revision: R(900, '0000000000000001'), prior_tags_revision: R(899, '0000000000000001'),
      } as never)
      .mockResolvedValueOnce({
        ok: true, tags: ['t2', 't1'], tags_revision: R(2, '0000000000000002'), prior_tags_revision: R(1, '0000000000000002'),
      } as never)
    const { store } = mount([], [], R(899, '0000000000000001'))
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0]) // t1
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1'], tags_revision: R(900, '0000000000000001') } as ChatSlot,
      ]))
    })
    await waitFor(() => expect(options()[0].getAttribute('aria-checked')).toBe('true'))

    // Gateway restarts; its first frame for this slot carries sequence 1 of a
    // new epoch and a different (authoritative) tag list.
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t2'], tags_revision: R(1, '0000000000000002') } as ChatSlot,
      ]))
    })
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'true'])
    })
    // The next toggle composes onto the adopted post-restart list.
    fireEvent.click(options()[0]) // t1
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    expect(lastWrite()).toEqual(['zzq-slot', ['t2', 't1']])
  })

  it('rejects a slow pre-restart reply (lower epoch) after the restarted gateway frame', async () => {
    // Old process (epoch A) → restart → new process (epoch B) frame adopted →
    // a slow reply from the OLD process lands afterwards. Its epoch is retired,
    // so it must not be re-adopted even though A's sequence is far higher.
    const R = (n: number, epoch: string) => `${epoch}.${String(n).padStart(20, '0')}-ab12cd34`
    setSlotTags.mockResolvedValueOnce({
      ok: true, tags: ['t2', 't1'], tags_revision: R(2, '0000000000000002'), prior_tags_revision: R(1, '0000000000000002'),
    } as never)
    const { store } = mount(['t1'], [], R(900, '0000000000000001'))
    await screen.findByText('zzq-alpha')
    expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'false'])

    // Restarted gateway's first frame: new epoch B, authoritative list ['t2'].
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t2'], tags_revision: R(1, '0000000000000002') } as ChatSlot,
      ]))
    })
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'true'])
    })

    // Late reply from the retired epoch A with the pre-restart list ['t1'].
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1'], tags_revision: R(901, '0000000000000001') } as ChatSlot,
      ]))
    })
    // Not re-adopted: the accepted epoch-B list stays on screen ...
    expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'true'])
    // ... and the next toggle composes onto it, not onto the stale ['t1'].
    fireEvent.click(options()[0]) // t1
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    expect(lastWrite()).toEqual(['zzq-slot', ['t2', 't1']])
  })

  describe('revision ordering', () => {
    const R = (n: number, epoch: string) => `${epoch}.${String(n).padStart(20, '0')}-ab12cd34`
    const E1 = '0000000000000001'
    const E2 = '0000000000000002'

    it('totally orders revisions: later epoch wins, then sequence; opaque forms are unordered', () => {
      expect(compareRevisions(R(1, E2), R(999, E1))).toBeGreaterThan(0) // restart beats any earlier seq
      expect(compareRevisions(R(999, E1), R(1, E2))).toBeLessThan(0)
      expect(compareRevisions(R(3, E2), R(2, E2))).toBeGreaterThan(0)
      expect(compareRevisions(R(2, E2), R(2, E2))).toBe(0)
      expect(compareRevisions('rev-x', R(2, E2))).toBeNull()
      expect(compareRevisions(R(2, E2), null)).toBeNull()
    })

    it('seeds only the accepted revision or a strictly newer one', () => {
      // Nothing accepted yet: any frame seeds (the client has no better data).
      expect(shouldSeedAcceptedSnapshot(R(901, E1), undefined, [])).toBe(true)
      expect(shouldSeedAcceptedSnapshot(R(3, E2), R(2, E2), [])).toBe(true)
      expect(shouldSeedAcceptedSnapshot(R(2, E2), R(2, E2), [])).toBe(true)
      expect(shouldSeedAcceptedSnapshot(R(1, E2), R(2, E2), [])).toBe(false)
      // A slow pre-restart reply (earlier epoch, however high its sequence)
      // never replaces a post-restart snapshot ...
      expect(shouldSeedAcceptedSnapshot(R(999, E1), R(1, E2), [])).toBe(false)
      // ... while the restarted gateway's first frame replaces any pre-restart one.
      expect(shouldSeedAcceptedSnapshot(R(1, E2), R(999, E1), [])).toBe(true)
      // Legacy opaque revisions keep the lineage rule.
      expect(shouldSeedAcceptedSnapshot('rev-x', 'rev-y', ['rev-x'])).toBe(false)
      expect(shouldSeedAcceptedSnapshot('rev-z', 'rev-y', ['rev-x'])).toBe(true)
      // A frame with NO revision comes from a gateway that does not mint them
      // (reconnect to a pre-revision process): it is authoritative, never held
      // against a revision this client cached from the previous process.
      expect(shouldSeedAcceptedSnapshot(null, R(2, E2), [])).toBe(true)
      expect(shouldSeedAcceptedSnapshot(null, 'rev-y', ['rev-x'])).toBe(true)
    })

    it('names its base revision and rebases the click onto a concurrent commit the server refused it for', async () => {
      // Client B committed t2 (R2) and its frame has NOT reached this client
      // when the user adds t1 onto the accepted [] @ R1. The absolute list
      // [t1] must not replace [t2]: the PUT names R1 as its base, the server
      // refuses with the list it holds, and the click's delta is re-applied
      // onto THAT list — [t2, t1] @ base R2 — without any user action. The
      // overlay ends on the committed list; no error is shown.
      setSlotTags
        .mockRejectedValueOnce(Object.assign(new Error('tags changed since the list was composed'), {
          status: 409,
          body: JSON.stringify({
            error: 'tags changed since the list was composed', code: 'stale_base',
            base_tags_revision: R(1, E1), tags_revision: R(2, E1), tags: ['t2'],
          }),
        }))
        .mockResolvedValueOnce({
          ok: true, tags: ['t2', 't1'], tags_revision: R(3, E1), prior_tags_revision: R(2, E1),
        } as never)
      mount([], [], R(1, E1))
      await screen.findByText('zzq-alpha')
      fireEvent.click(options()[0]) // t1
      await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
      expect(writes()[0]).toEqual(['zzq-slot', ['t1']])
      expect(setSlotTags.mock.calls[0][2]).toBe(R(1, E1))
      expect(lastWrite()).toEqual(['zzq-slot', ['t2', 't1']])
      expect(lastWriteBase()).toBe(R(2, E1))
      await waitFor(() => {
        expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'true'])
      })
      expect(screen.queryByRole('alert')).toBeNull()
      // The next click composes onto the committed list at its revision.
      setSlotTags.mockResolvedValueOnce({
        ok: true, tags: ['t1'], tags_revision: R(4, E1), prior_tags_revision: R(3, E1),
      } as never)
      fireEvent.click(options()[1]) // remove t2
      await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(3))
      expect(lastWrite()).toEqual(['zzq-slot', ['t1']])
      expect(lastWriteBase()).toBe(R(3, E1))
    })

    it('adopts a revision-less legacy frame and clears the cached lineage', async () => {
      // Reconnect to a pre-revision gateway: the store's next frame carries no
      // tags_revision. It must become the toggle base (rendered as-is, not
      // re-covered by the retired overlay), and the lineage cached from the
      // old process must not be held against it or anything that follows.
      setSlotTags.mockResolvedValueOnce({ ok: true, tags: ['t2'] } as never)
      const { store } = mount(['t1'], [], R(5, E2))
      await screen.findByText('zzq-alpha')
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'false'])
      act(() => {
        store.dispatch(sseSlots([
          { key: 'zzq-slot', messages: 0, running: false, tags: [] } as ChatSlot,
        ]))
      })
      // The legacy frame is authoritative: no overlay re-covers it with ['t1'].
      await waitFor(() => {
        expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'false'])
      })
      // The next toggle composes onto the legacy list, not the retired ['t1'].
      fireEvent.click(options()[1]) // t2
      await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
      expect(lastWrite()).toEqual(['zzq-slot', ['t2']])
    })
  })

  it('replaces a pre-restart frame seeded on an uncached slot with the restarted gateway first frame', async () => {
    // Epoch B frame for another slot retires epoch A; a delayed epoch-A fetch
    // then overwrites Redux for a slot whose picker was never opened. Opening
    // it must not record that stale frame as the slot's accepted revision:
    // the restarted gateway's first frame for it is adopted outright.
    const R = (n: number, epoch: string) => `${epoch}.${String(n).padStart(20, '0')}-ab12cd34`
    setSlotTags.mockResolvedValueOnce({
      ok: true, tags: ['t1'], tags_revision: R(2, '0000000000000002'), prior_tags_revision: R(1, '0000000000000002'),
    } as never)
    popover.slotKey = 'zzq-other'
    const { store, rerender } = mount([], [
      { key: 'zzq-other', messages: 0, running: false, tags: [], tags_revision: R(1, '0000000000000002') } as ChatSlot,
    ], R(900, '0000000000000001'))
    await screen.findByText('zzq-alpha')
    // Delayed epoch-A fetch lands for the never-opened slot.
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1', 't2'], tags_revision: R(901, '0000000000000001') } as ChatSlot,
        { key: 'zzq-other', messages: 0, running: false, tags: [], tags_revision: R(1, '0000000000000002') } as ChatSlot,
      ]))
    })
    popover.slotKey = 'zzq-slot'
    rerender(<SlotTagPopover />)
    // Store is all the client knows for this slot, so it renders it ...
    expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'true'])
    // ... but the restarted gateway's first frame replaces it outright, even
    // though its sequence (1) is far below the stale frame's (901).
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: [], tags_revision: R(1, '0000000000000002') } as ChatSlot,
        { key: 'zzq-other', messages: 0, running: false, tags: [], tags_revision: R(1, '0000000000000002') } as ChatSlot,
      ]))
    })
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'false'])
    })
    fireEvent.click(options()[0]) // t1 composes onto the adopted []
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    expect(lastWrite()).toEqual(['zzq-slot', ['t1']])
    popover.slotKey = 'zzq-slot'
  })

  it('a retry after a refused write composes onto the server-restored list, not the pre-write baseline', async () => {
    // Concurrent writer commits ['t2'] (R2) while our PUT (t1) is in flight; the
    // save is refused and the backend restores R2's list as R4. Its rollback
    // frame has NOT arrived yet when the user retries. The retry must compose
    // onto the restored ['t2'] carried by the 409 — never onto our stale
    // pre-write baseline [] — so the concurrent tag is not removed.
    const R = (n: number) => `0000000000000001.${String(n).padStart(20, '0')}-ab12cd34`
    let reject!: (error: unknown) => void
    setSlotTags
      .mockImplementationOnce(() => new Promise((_resolve, rej) => { reject = rej }) as never)
      .mockResolvedValueOnce({
        ok: true, tags: ['t2', 't1'], tags_revision: R(5), prior_tags_revision: R(4),
      } as never)
    mount([], [], R(1))
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0]) // t1
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    await act(async () => {
      reject(Object.assign(new Error('session was deleted or rebound'), {
        status: 409,
        body: JSON.stringify({
          error: 'session was deleted or rebound', code: 'session_gone',
          rejected_tags_revision: R(3), tags_revision: R(4), tags: ['t2'],
        }),
      }))
    })
    // The rollback overlay shows the server's restored list (store is still R1).
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'true'])
    })
    expect(screen.getByRole('alert')).toHaveTextContent('session was deleted or rebound')

    // Rapid retry: add t1 again. Composes onto ['t2'], keeping the concurrent tag.
    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    expect(lastWrite()).toEqual(['zzq-slot', ['t2', 't1']])
  })

  it('re-covers the confirmed checkmark when a delayed known-predecessor frame arrives late', async () => {
    setSlotTags.mockResolvedValueOnce({
      ok: true, tags: ['t1'], tags_revision: 'revision-2', prior_tags_revision: 'revision-1',
    } as never)
    const { store } = mount([], [], 'revision-1')
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0]) // t1
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    // Confirming frame retires the overlay: accepted=revision-2, known={1}.
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1'], tags_revision: 'revision-2' } as ChatSlot,
      ]))
    })
    await waitFor(() => expect(options()[0].getAttribute('aria-checked')).toBe('true'))

    // A delayed revision-1 frame (empty tags) arrives out of order. Without an
    // overlay active it would render and visibly reverse the checkmark.
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: [], tags_revision: 'revision-1' } as ChatSlot,
      ]))
    })
    expect(options()[0].getAttribute('aria-checked')).toBe('true')

    // The store catching back up to the accepted revision keeps it checked.
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1'], tags_revision: 'revision-2' } as ChatSlot,
      ]))
    })
    await waitFor(() => expect(options()[0].getAttribute('aria-checked')).toBe('true'))
  })

  it('a click after close/reopen composes onto the in-flight write, not the stale frame', async () => {
    let finish!: (value: unknown) => void
    setSlotTags
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as never)
      .mockResolvedValueOnce({
        ok: true, tags: ['t1', 't2'], tags_revision: 'revision-3', prior_tags_revision: 'revision-2',
      } as never)
    const { store, rerender } = mount([], [], 'revision-1')
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0]) // t1
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    expect(lastWrite()).toEqual(['zzq-slot', ['t1']])

    popover.slotKey = null
    rerender(<SlotTagPopover />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    // Redux still carries revision 1 and the PUT has not settled. Reopening
    // renders the store honestly (no resurrected intent) ...
    popover.slotKey = 'zzq-slot'
    rerender(<SlotTagPopover />)
    expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'false'])

    // ... yet the next click is a DELTA queued behind the in-flight write, so
    // it composes onto that write's confirmed result rather than the stale
    // list the click was made against: t1 is not removed.
    fireEvent.click(options()[1]) // t2
    expect(setSlotTags).toHaveBeenCalledOnce()
    await act(async () => {
      finish({ ok: true, tags: ['t1'], tags_revision: 'revision-2', prior_tags_revision: 'revision-1' })
    })
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    expect(lastWrite()).toEqual(['zzq-slot', ['t1', 't2']])

    // The kept overlay is the server-confirmed list, and its frame retires it.
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'true'])
    })
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1', 't2'], tags_revision: 'revision-3' } as ChatSlot,
      ]))
    })
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'true'])
    })
  })

  it('a click made against a foreign frame composes onto the in-flight write', async () => {
    // GPT round-4 scenario: PUT1 (t1) in flight -> another writer's frame
    // (revision-3, ['t2']) arrives -> reopen -> click. The click must not ship
    // an absolute list derived from that frame and drop t1 once PUT1 lands.
    let finish!: (value: unknown) => void
    setSlotTags
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as never)
      .mockResolvedValueOnce({
        ok: true, tags: ['t1', 't2'], tags_revision: 'revision-5', prior_tags_revision: 'revision-4',
      } as never)
    const { store, rerender } = mount([], [], 'revision-1')
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0]) // t1
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())

    popover.slotKey = null
    rerender(<SlotTagPopover />)
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t2'], tags_revision: 'revision-3' } as ChatSlot,
      ]))
    })
    popover.slotKey = 'zzq-slot'
    rerender(<SlotTagPopover />)
    expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'true'])

    // User adds t1 again against the foreign frame; still queued behind PUT1.
    fireEvent.click(options()[0])
    expect(setSlotTags).toHaveBeenCalledOnce()

    // PUT1 lands last-write-wins on the server as revision-4 (['t1']).
    await act(async () => {
      finish({ ok: true, tags: ['t1'], tags_revision: 'revision-4', prior_tags_revision: 'revision-3' })
    })
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    // Delta "add t1" onto PUT1's confirmed ['t1'] is idempotent: t1 is kept,
    // and no absolute list built from the foreign frame is shipped.
    expect(lastWrite()).toEqual(['zzq-slot', ['t1']])
  })

  it('discards a queued intent when a newer revision arrives before reopen', async () => {
    let finish!: (value: unknown) => void
    setSlotTags
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as never)
      .mockResolvedValueOnce({
        ok: true, tags: ['t2', 't1'], tags_revision: 'revision-4', prior_tags_revision: 'revision-3',
      } as never)
    const { store, rerender } = mount([], [], 'revision-1')
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0]) // t1 (options are sorted by tag order)
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    expect(lastWrite()).toEqual(['zzq-slot', ['t1']])

    popover.slotKey = null
    rerender(<SlotTagPopover />)
    await act(async () => {
      finish({ ok: true, tags: ['t1'], tags_revision: 'revision-2', prior_tags_revision: 'revision-1' })
    })
    // Another client commits revision 3 (drops t1, adds t2) while the picker
    // is closed. revision-3 is outside this intent's lineage {1, 2}.
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t2'], tags_revision: 'revision-3' } as ChatSlot,
      ]))
    })

    popover.slotKey = 'zzq-slot'
    rerender(<SlotTagPopover />)
    // The stale intent ['t1'] must not resurrect over the newer store state.
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['false', 'true'])
    })

    // The next toggle composes from the newer revision-3 list, not from ['t1'].
    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    expect(lastWrite()).toEqual(['zzq-slot', ['t2', 't1']])
  })

  it('does not treat a delayed known-predecessor frame as a concurrent winner', async () => {
    let finishSecond!: (value: unknown) => void
    setSlotTags
      .mockResolvedValueOnce({
        ok: true, tags: ['t1'], tags_revision: 'revision-2', prior_tags_revision: 'revision-1',
      } as never)
      .mockImplementationOnce(() => new Promise(resolve => { finishSecond = resolve }) as never)
      .mockResolvedValueOnce({
        ok: true, tags: ['t2'], tags_revision: 'revision-4', prior_tags_revision: 'revision-3',
      } as never)
    const { store } = mount([], [], 'revision-1')
    await screen.findByText('zzq-alpha')

    // First write lands and its frame retires the overlay: accepted=2, known={1}.
    fireEvent.click(options()[0]) // t1
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1'], tags_revision: 'revision-2' } as ChatSlot,
      ]))
    })
    await waitFor(() => expect(options()[0].getAttribute('aria-checked')).toBe('true'))

    // Second write is in flight when a DELAYED revision-1 frame (a known
    // predecessor, not a newer writer) arrives out of order.
    fireEvent.click(options()[1]) // t2 -> ['t1', 't2']
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(2))
    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: [], tags_revision: 'revision-1' } as ChatSlot,
      ]))
    })
    await act(async () => {
      finishSecond({
        ok: true, tags: ['t1', 't2'], tags_revision: 'revision-3', prior_tags_revision: 'revision-2',
      })
    })
    // The stale frame must not clear the overlay or become the next base.
    expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'true'])

    act(() => {
      store.dispatch(sseSlots([
        { key: 'zzq-slot', messages: 0, running: false, tags: ['t1', 't2'], tags_revision: 'revision-3' } as ChatSlot,
      ]))
    })
    await waitFor(() => {
      expect(options().map(o => o.getAttribute('aria-checked'))).toEqual(['true', 'true'])
    })
    fireEvent.click(options()[0]) // t1 off, composed from ['t1', 't2'] not from []
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledTimes(3))
    expect(lastWrite()).toEqual(['zzq-slot', ['t2']])
  })

  it('preserves an accepted PUT across close and reopen before its frame arrives', async () => {
    let finish!: (value: unknown) => void
    setSlotTags.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }) as never)
    const { rerender } = mount([], [], 'revision-1')
    await screen.findByText('zzq-alpha')

    fireEvent.click(options()[0])
    await waitFor(() => expect(setSlotTags).toHaveBeenCalledOnce())
    await act(async () => {
      finish({
        ok: true,
        tags: ['t1'],
        tags_revision: 'revision-2',
        prior_tags_revision: 'revision-1',
      })
    })

    popover.slotKey = null
    rerender(<SlotTagPopover />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    // Redux still carries revision 1. Reopening must retain accepted revision 2
    // as the overlay and as the base for the next user intent.
    popover.slotKey = 'zzq-slot'
    rerender(<SlotTagPopover />)
    await waitFor(() => expect(options()[0].getAttribute('aria-checked')).toBe('true'))

    setSlotTags.mockResolvedValueOnce({
      ok: true,
      tags: ['t1', 't2'],
      tags_revision: 'revision-3',
      prior_tags_revision: 'revision-2',
    } as never)
    fireEvent.click(options()[1])
    await waitFor(() => {
      expect(lastWrite()).toEqual(['zzq-slot', ['t1', 't2']])
    })
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
    await waitFor(() => expect(lastWrite()).toEqual(['zzq-slot', ['t2']]))
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
    expect(lastWrite()).toEqual(['zzq-slot', ['t2']])
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
      expect(lastWrite()).toEqual(['zzq-slot', ['t1', 't2']])
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
    await waitFor(() => expect(lastWrite()).toEqual(['zzq-slot', ['t2', 't1']]))
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
    expect(lastWrite()).toEqual(['zzq-slot', ['t2', 't1']])
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
    expect(lastWrite()).toEqual(['zzq-slot-2', ['t2', 't1']])
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
