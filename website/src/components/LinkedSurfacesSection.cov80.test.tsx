// LinkedSurfacesSection — ONE row per channel whose LABEL is the action:
// `Disconnect from X` while output flows there, `Connect to X` otherwise.
// The role/offline badges, reminder and release items were deliberately
// removed; these tests exercise the current contract only.
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../test/helpers'
import LinkedSurfacesSection from './LinkedSurfacesSection'
import { addSlotOptimistic } from '../store/dashboardSlice'
import { ApiError, api } from '../api/client'
import { i18nT } from '../i18n/t'
import type { ChatSlot, ConfiguredChannelTarget, SessionLink } from '../types'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      channelTargets: vi.fn(),
      pauseSlack: vi.fn(),
      pauseMirror: vi.fn(),
      slackLink: vi.fn(),
      linkMirror: vi.fn(),
    },
  }
})

/**
 * happy-dom cannot drive a real Radix menu open (no PointerEvent), so both
 * menu families collapse to plain buttons. `onSelect` gets a cancelable Event
 * so the unavailable-target branch can really call `preventDefault()`.
 */
function stubItem(prefix: string) {
  const Item = ({ children, onSelect, ...rest }: {
    children?: React.ReactNode
    onSelect?: (e: Event) => void
    'aria-disabled'?: boolean
    'aria-busy'?: boolean
    title?: string
    className?: string
  }) => (
    <button
      type="button"
      aria-disabled={rest['aria-disabled']}
      aria-busy={rest['aria-busy']}
      title={rest.title}
      className={rest.className}
      onClick={() => onSelect?.(new Event('select', { cancelable: true }))}
    >
      {children}
    </button>
  )
  return { [`${prefix}Item`]: Item }
}

vi.mock('./ui/dropdown-menu', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...stubItem('DropdownMenu'),
}))
vi.mock('./ui/context-menu', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...stubItem('ContextMenu'),
}))

const channelTargets = vi.mocked(api.channelTargets)
const pauseSlack = vi.mocked(api.pauseSlack)
const pauseMirror = vi.mocked(api.pauseMirror)
const slackLink = vi.mocked(api.slackLink)
const linkMirror = vi.mocked(api.linkMirror)

const SLOT = 'zzq-slot'
const L = (k: string, vars?: Record<string, unknown>) =>
  i18nT(`components.linkedSurfacesSection.${k}`, vars)

function link(over: Partial<SessionLink> = {}): SessionLink {
  return { channel: 'discord', label: 'zzq-guild', target: 't-1', direction: 'out', live: true, ...over }
}

function target(over: Partial<ConfiguredChannelTarget> = {}): ConfiguredChannelTarget {
  return {
    channel_type: 'discord',
    target_id: 'zzq-target',
    label: 'zzq-target-label',
    available: true,
    unavailable_reason: '',
    ...over,
  }
}

function mount(slot: Partial<ChatSlot> = {}, variant: 'dropdown' | 'context' = 'dropdown') {
  const store = createTestStore()
  store.dispatch(addSlotOptimistic({
    key: SLOT, messages: 0, running: false, ...slot,
  } as ChatSlot))
  const view = renderWithProviders(
    <LinkedSurfacesSection slotKey={SLOT} variant={variant} />,
    { store },
  )
  return { store, ...view }
}

const notifications = (store: ReturnType<typeof createTestStore>) =>
  store.getState().notifications.items.map(n => `${n.kind}:${n.title}`)

const slotOf = (store: ReturnType<typeof createTestStore>) =>
  store.getState().dashboard.slots.find(s => s.key === SLOT)!

describe('LinkedSurfacesSection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    channelTargets.mockResolvedValue([] as never)
    pauseSlack.mockResolvedValue({ ok: true } as never)
    pauseMirror.mockResolvedValue({ ok: true } as never)
    slackLink.mockResolvedValue({ ok: true, channel: 'C-zzq', thread_ts: '1.2' } as never)
    linkMirror.mockResolvedValue({ ok: true, conversation_id: 'conv-zzq' } as never)
  })

  describe('bound-channel rows', () => {
    it('a connected channel reads Disconnect, under the brand label', async () => {
      mount({ links: [link()] })
      expect(await screen.findByText(L('disconnect_from', { label: 'Discord' }))).toBeInTheDocument()
    })

    it('a disconnected channel reads Connect — same row, other verb', async () => {
      mount({ links: [link({ paused: true })] })
      expect(await screen.findByText(L('connect_to', { label: 'Discord' }))).toBeInTheDocument()
    })

    it('an origin row is a normal control, not a badge', async () => {
      mount({ links: [link({ direction: 'origin' })] })
      expect(await screen.findByText(L('disconnect_from', { label: 'Discord' }))).toBeInTheDocument()
    })

    it('an unrecognised channel type falls back to the link label', async () => {
      mount({ links: [link({ channel: 'zzq-exotic', label: 'zzq-exotic-label' })] })
      expect(
        await screen.findByText(L('disconnect_from', { label: 'zzq-exotic-label' })),
      ).toBeInTheDocument()
    })

    it('two links on one channel collapse to ONE row that acts on both', async () => {
      const { store } = mount({
        links: [link({ direction: 'origin', target: 'o-1' }), link({ target: 'm-1' })],
      })
      const rows = await screen.findAllByText(L('disconnect_from', { label: 'Discord' }))
      expect(rows).toHaveLength(1)
      fireEvent.click(rows[0])
      await waitFor(() => expect(pauseMirror).toHaveBeenCalledTimes(2))
      expect(pauseMirror).toHaveBeenCalledWith(SLOT, true, true)
      expect(pauseMirror).toHaveBeenCalledWith(SLOT, true, false)
      await waitFor(() => expect(slotOf(store).links?.every(l => l.paused)).toBe(true))
    })

    it('a mixed group reads Disconnect and one click stops the remainder', async () => {
      mount({
        links: [link({ direction: 'origin', paused: true, target: 'o-1' }), link({ target: 'm-1' })],
      })
      fireEvent.click(await screen.findByText(L('disconnect_from', { label: 'Discord' })))
      await waitFor(() => expect(pauseMirror).toHaveBeenCalledTimes(2))
      expect(pauseMirror).toHaveBeenCalledWith(SLOT, true, false)
    })

    it('a Slack row toggles through the slack-pause path and flips the verb', async () => {
      const { store } = mount({ links: [link({ channel: 'slack', label: 'zzq-slack' })] })
      fireEvent.click(await screen.findByText(L('disconnect_from', { label: 'Slack' })))
      await waitFor(() => expect(pauseSlack).toHaveBeenCalledWith(SLOT, true))
      await waitFor(() => expect(slotOf(store).links?.[0].paused).toBe(true))
      expect(await screen.findByText(L('connect_to', { label: 'Slack' }))).toBeInTheDocument()
      expect(pauseMirror).not.toHaveBeenCalled()
    })

    it('reconnecting a paused channel sends paused=false and patches the store', async () => {
      const { store } = mount({ links: [link({ paused: true })] })
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'Discord' })))
      await waitFor(() => expect(pauseMirror).toHaveBeenCalledWith(SLOT, false, false))
      await waitFor(() => expect(slotOf(store).links?.[0].paused).toBe(false))
    })

    it('a failed disconnect is reported with the backend reason and the row stays connected', async () => {
      pauseMirror.mockRejectedValue(new Error('zzq-pause-broke'))
      const { store } = mount({ links: [link()] })
      fireEvent.click(await screen.findByText(L('disconnect_from', { label: 'Discord' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('disconnect_failed', { label: 'Discord', reason: 'zzq-pause-broke' })}`,
      ]))
      expect(slotOf(store).links?.[0].paused).toBeUndefined()
    })

    it('a failed connect on a paused row reports connect_failed', async () => {
      pauseMirror.mockRejectedValue(new Error('zzq-resume-broke'))
      const { store } = mount({ links: [link({ paused: true })] })
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'Discord' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('connect_failed', { label: 'Discord', reason: 'zzq-resume-broke' })}`,
      ]))
    })

    it('a non-Error failure falls back to the generic reason', async () => {
      pauseSlack.mockRejectedValue('zzq-not-an-error')
      const { store } = mount({ links: [link({ channel: 'slack' })] })
      fireEvent.click(await screen.findByText(L('disconnect_from', { label: 'Slack' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('disconnect_failed', { label: 'Slack', reason: L('unknown_error') })}`,
      ]))
    })

    it('a click on a row whose mutation is in flight is swallowed', async () => {
      let release: (v: unknown) => void = () => {}
      pauseMirror.mockReturnValue(new Promise(r => { release = r }) as never)
      mount({ links: [link()] })
      const row = await screen.findByText(L('disconnect_from', { label: 'Discord' }))
      fireEvent.click(row)
      await waitFor(() => expect(row.closest('button')).toHaveAttribute('aria-busy', 'true'))
      fireEvent.click(row)
      expect(pauseMirror).toHaveBeenCalledTimes(1)
      release({ ok: true })
    })
  })

  describe('configured-target offers', () => {
    it('an unbound target is offered under its OWN label and links on click', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-target-label' })))
      await waitFor(() => expect(linkMirror).toHaveBeenCalledWith(SLOT, 'discord', 'zzq-target'))
      expect(notifications(store)).toEqual([])
      // Deliberately NO onSuccess store write: the link row arrives via refetch,
      // never from a captured snapshot that could drop a concurrent toggle's row.
      expect(slotOf(store).links).toBeUndefined()
    })

    it('a bound channel gets no second offer', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      mount({ links: [link()] })
      await waitFor(() => expect(channelTargets).toHaveBeenCalled())
      expect(screen.queryByText(L('connect_to', { label: 'zzq-target-label' }))).not.toBeInTheDocument()
      expect(screen.getByText(L('disconnect_from', { label: 'Discord' }))).toBeInTheDocument()
    })

    it('a slack offer routes through the slack-link path and stores the returned thread', async () => {
      channelTargets.mockResolvedValue([
        target({ channel_type: 'slack', target_id: 'C-dm', label: 'zzq-slack-dm' }),
      ] as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-slack-dm' })))
      await waitFor(() => expect(slackLink).toHaveBeenCalledWith(SLOT, 'C-dm'))
      await waitFor(() => expect(slotOf(store).slack_linked).toBe(true))
      expect(slotOf(store).slack_channel).toBe('C-zzq')
      expect(slotOf(store).slack_thread_ts).toBe('1.2')
      expect(linkMirror).not.toHaveBeenCalled()
    })

    it('a not-ok slack response leaves the slot untouched', async () => {
      slackLink.mockResolvedValue({ ok: false } as never)
      channelTargets.mockResolvedValue([
        target({ channel_type: 'slack', target_id: 'C-dm', label: 'zzq-slack-dm' }),
      ] as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-slack-dm' })))
      await waitFor(() => expect(slackLink).toHaveBeenCalled())
      expect(slotOf(store).slack_linked).toBeUndefined()
    })

    it('a failed slack connect is reported under the brand label', async () => {
      slackLink.mockRejectedValue(new Error('zzq-slack-refused'))
      channelTargets.mockResolvedValue([
        target({ channel_type: 'slack', target_id: 'C-dm', label: 'zzq-slack-dm' }),
      ] as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-slack-dm' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('connect_failed', { label: 'Slack', reason: 'zzq-slack-refused' })}`,
      ]))
    })

    it('a 409 conversation_occupied connect reports the conversation as in use', async () => {
      linkMirror.mockRejectedValue(
        new ApiError(409, 'conflict', JSON.stringify({ code: 'conversation_occupied' })),
      )
      channelTargets.mockResolvedValue([target()] as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-target-label' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('held_elsewhere', { label: 'zzq-target-label' })}`,
      ]))
    })

    it('a 409 with a different code stays an ordinary connect failure', async () => {
      linkMirror.mockRejectedValue(
        new ApiError(409, 'zzq-target-down', JSON.stringify({ code: 'configured_target_unavailable' })),
      )
      channelTargets.mockResolvedValue([target()] as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-target-label' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('connect_failed', { label: 'zzq-target-label', reason: 'zzq-target-down' })}`,
      ]))
    })

    it('a non-Error mirror-connect failure falls back to the generic reason', async () => {
      linkMirror.mockRejectedValue('zzq-not-an-error')
      channelTargets.mockResolvedValue([target()] as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-target-label' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('connect_failed', { label: 'zzq-target-label', reason: L('unknown_error') })}`,
      ]))
    })

    it('an unavailable target shows its reason, refuses the click and calls nothing', async () => {
      channelTargets.mockResolvedValue([
        target({ available: false, unavailable_reason: 'zzq-transport-absent' }),
      ] as never)
      const { store } = mount()
      const row = await screen.findByText(L('connect_to', { label: 'zzq-target-label' }))
      expect(screen.getByText('zzq-transport-absent')).toBeInTheDocument()
      expect(row.closest('button')).toHaveAttribute('aria-disabled', 'true')
      fireEvent.click(row)
      await waitFor(() => expect(notifications(store)).toEqual(['error:zzq-transport-absent']))
      expect(linkMirror).not.toHaveBeenCalled()
    })

    it('an unavailable target with no reason uses the generic explanation', async () => {
      channelTargets.mockResolvedValue([target({ available: false })] as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-target-label' })))
      await waitFor(() => expect(notifications(store)).toEqual([`error:${L('unavailable')}`]))
    })

    it('a non-array payload degrades to an empty picker instead of throwing', async () => {
      channelTargets.mockResolvedValue({ oops: true } as never)
      const { container } = mount()
      await waitFor(() => expect(channelTargets).toHaveBeenCalled())
      expect(container.querySelectorAll('button')).toHaveLength(0)
    })
  })

  it('behaves identically inside the context-menu family', async () => {
    channelTargets.mockResolvedValue([target()] as never)
    mount({}, 'context')
    fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-target-label' })))
    await waitFor(() => expect(linkMirror).toHaveBeenCalledWith(SLOT, 'discord', 'zzq-target'))
  })

  it('renders only offers for an unknown slot key — no link rows', async () => {
    channelTargets.mockResolvedValue([target()] as never)
    renderWithProviders(
      <LinkedSurfacesSection slotKey="zzq-missing" variant="dropdown" />,
    )
    // The slot is read defensively (`slot?.links ?? []`): a store entry that has
    // not landed yet must still get its Connect offers, or a mount race leaves
    // the session menu with no way to connect a channel.
    expect(
      await screen.findByText(L('connect_to', { label: 'zzq-target-label' })),
    ).toBeInTheDocument()
    expect(screen.queryByText(L('disconnect_from', { label: 'Discord' }))).not.toBeInTheDocument()
  })
})
