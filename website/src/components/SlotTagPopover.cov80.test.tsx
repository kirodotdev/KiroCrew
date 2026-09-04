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

function mount(slotTags: string[] = []) {
  const store = createTestStore()
  store.dispatch(sseSlots([
    { key: 'zzq-slot', messages: 0, running: false, tags: slotTags } as ChatSlot,
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
