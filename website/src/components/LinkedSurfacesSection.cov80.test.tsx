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
      slackLink: vi.fn(),
      linkMirror: vi.fn(),
      pauseSlack: vi.fn(),
      pauseMirror: vi.fn(),
    },
  }
})

/**
 * happy-dom cannot drive a real Radix menu open (no PointerEvent), so both
 * menu families collapse to plain buttons. `onSelect` gets a cancelable Event
 * so the row's `preventDefault()` (keep the menu open) can really run, and the
 * aria/tooltip attributes are forwarded because the disabled and in-flight
 * assertions read them off the rendered row.
 */
function stubItem(prefix: string) {
  const Item = ({ children, onSelect, title, ...rest }: {
    children?: React.ReactNode
    onSelect?: (e: Event) => void
    title?: string
    'aria-disabled'?: boolean
    'aria-busy'?: boolean
    className?: string
  }) => (
    <button
      type="button"
      aria-disabled={rest['aria-disabled']}
      aria-busy={rest['aria-busy']}
      title={title}
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
const slackLink = vi.mocked(api.slackLink)
const linkMirror = vi.mocked(api.linkMirror)
const pauseSlack = vi.mocked(api.pauseSlack)
const pauseMirror = vi.mocked(api.pauseMirror)

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

const rowOf = (store: ReturnType<typeof createTestStore>, channel: string, origin = false) =>
  slotOf(store).links!.find(l => l.channel === channel && (l.direction === 'origin') === origin)

describe('LinkedSurfacesSection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    channelTargets.mockResolvedValue([] as never)
    slackLink.mockResolvedValue({ ok: true, channel: 'C-zzq', thread_ts: '1.2' } as never)
    linkMirror.mockResolvedValue({ ok: true, conversation_id: 'conv-zzq' } as never)
    pauseSlack.mockResolvedValue({ ok: true } as never)
    pauseMirror.mockResolvedValue({ ok: true } as never)
  })

  describe('one row per channel — the label is the action', () => {
    it('disconnects a connected Slack channel and flips the verb in place', async () => {
      const { store } = mount({ links: [link({ channel: 'slack', label: 'Slack' })] })
      fireEvent.click(await screen.findByText(L('disconnect_from', { label: 'Slack' })))
      await waitFor(() => expect(pauseSlack).toHaveBeenCalledWith(SLOT, true))
      // The row is patched in place: the binding is retained, never dropped.
      await waitFor(() => expect(rowOf(store, 'slack')?.paused).toBe(true))
      expect(slotOf(store).links).toHaveLength(1)
      expect(screen.getByText(L('connect_to', { label: 'Slack' }))).toBeInTheDocument()
    })

    it('reconnects a disconnected Slack channel on the same single row', async () => {
      const { store } = mount({ links: [link({ channel: 'slack', label: 'Slack', paused: true })] })
      expect(screen.queryByText(L('disconnect_from', { label: 'Slack' }))).not.toBeInTheDocument()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'Slack' })))
      await waitFor(() => expect(pauseSlack).toHaveBeenCalledWith(SLOT, false))
      await waitFor(() => expect(rowOf(store, 'slack')?.paused).toBe(false))
    })

    it('a failed Slack disconnect notifies and keeps the row connected', async () => {
      pauseSlack.mockRejectedValue(new Error('zzq-pause-refused'))
      const { store } = mount({ links: [link({ channel: 'slack', label: 'Slack' })] })
      fireEvent.click(await screen.findByText(L('disconnect_from', { label: 'Slack' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('disconnect_failed', { label: 'Slack', reason: 'zzq-pause-refused' })}`,
      ]))
      expect(rowOf(store, 'slack')?.paused).toBeUndefined()
      expect(screen.getByText(L('disconnect_from', { label: 'Slack' }))).toBeInTheDocument()
    })

    it('a non-Error Slack reconnect failure falls back to the generic reason', async () => {
      pauseSlack.mockRejectedValue('zzq-not-an-error')
      const { store } = mount({ links: [link({ channel: 'slack', label: 'Slack', paused: true })] })
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'Slack' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('connect_failed', { label: 'Slack', reason: L('unknown_error') })}`,
      ]))
    })

    it('disconnects a mirror through the channel-neutral endpoint and patches its row', async () => {
      const { store } = mount({ links: [link()] })
      fireEvent.click(await screen.findByText(L('disconnect_from', { label: 'Discord' })))
      await waitFor(() => expect(pauseMirror).toHaveBeenCalledWith(SLOT, true, false))
      await waitFor(() => expect(rowOf(store, 'discord')?.paused).toBe(true))
    })

    it('marks the born-in conversation as origin when disconnecting it', async () => {
      const { store } = mount({ links: [link({ direction: 'origin' })] })
      fireEvent.click(await screen.findByText(L('disconnect_from', { label: 'Discord' })))
      await waitFor(() => expect(pauseMirror).toHaveBeenCalledWith(SLOT, true, true))
      await waitFor(() => expect(rowOf(store, 'discord', true)?.paused).toBe(true))
    })

    it('a failed mirror disconnect notifies and leaves the delivery flowing', async () => {
      pauseMirror.mockRejectedValue(new Error('zzq-mirror-refused'))
      const { store } = mount({ links: [link()] })
      fireEvent.click(await screen.findByText(L('disconnect_from', { label: 'Discord' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('disconnect_failed', { label: 'Discord', reason: 'zzq-mirror-refused' })}`,
      ]))
      expect(rowOf(store, 'discord')?.paused).toBeUndefined()
    })

    it('a failed mirror reconnect reports the connect direction', async () => {
      pauseMirror.mockRejectedValue(new Error('zzq-resume-refused'))
      const { store } = mount({ links: [link({ paused: true })] })
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'Discord' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('connect_failed', { label: 'Discord', reason: 'zzq-resume-refused' })}`,
      ]))
    })

    it('renders ONE row when the wire reports a channel twice, and acts on both deliveries', async () => {
      // A session born in Discord and then mirrored to Discord carries two links
      // for the one channel: an origin fact and a mirror fact with separate
      // paused flags. One row, and its click covers the whole group — acting on
      // only one delivery left the other muted with no control on screen.
      const { store } = mount({
        links: [
          link({ direction: 'origin' }),
          link({ direction: 'out', target: 't-2' }),
        ],
      })
      expect(await screen.findAllByText(L('disconnect_from', { label: 'Discord' }))).toHaveLength(1)
      fireEvent.click(screen.getByText(L('disconnect_from', { label: 'Discord' })))
      await waitFor(() => expect(pauseMirror).toHaveBeenCalledTimes(2))
      // One call per delivery, addressed by role, all of them disconnects.
      const calls = pauseMirror.mock.calls
      expect(calls.map(call => call[2]).sort()).toEqual([false, true])
      expect(calls.every(call => call[1] === true)).toBe(true)
      await waitFor(() => expect(slotOf(store).links!.every(l => l.paused === true)).toBe(true))
    })

    it('reads as connected while ANY delivery in the group is still live', async () => {
      // Under "all" a partially-failed group would read Connect while messages
      // were still arriving; under "any" one click stops the remainder.
      mount({
        links: [
          link({ direction: 'origin', paused: true }),
          link({ direction: 'out', target: 't-2' }),
        ],
      })
      expect(await screen.findByText(L('disconnect_from', { label: 'Discord' }))).toBeInTheDocument()
      expect(screen.queryByText(L('connect_to', { label: 'Discord' }))).not.toBeInTheDocument()
    })

    it('falls back to the wire label for a channel with no brand name', async () => {
      mount({ links: [link({ channel: 'zzq-chan', label: 'zzq-row-label' })] })
      expect(await screen.findByText(L('disconnect_from', { label: 'zzq-row-label' })))
        .toBeInTheDocument()
    })
  })

  describe('in-flight guard — per row, not per menu', () => {
    it('shows the spinner on the busy row and swallows a second click', async () => {
      let release: (v: unknown) => void = () => {}
      pauseSlack.mockReturnValue(new Promise(r => { release = r }) as never)
      mount({ links: [link({ channel: 'slack', label: 'Slack' })] })
      const row = await screen.findByText(L('disconnect_from', { label: 'Slack' }))
      fireEvent.click(row)
      await waitFor(() => expect(row.closest('button')).toHaveAttribute('aria-busy', 'true'))
      fireEvent.click(row)
      expect(pauseSlack).toHaveBeenCalledTimes(1)
      release({ ok: true })
      // The verb flip is the confirmation — no success toast.
      expect(await screen.findByText(L('connect_to', { label: 'Slack' }))).toBeInTheDocument()
    })

    it('guards an offer row while its connect is in flight', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      let release: (v: unknown) => void = () => {}
      linkMirror.mockReturnValue(new Promise(r => { release = r }) as never)
      mount()
      const row = await screen.findByText(L('connect_to', { label: 'zzq-target-label' }))
      fireEvent.click(row)
      await waitFor(() => expect(row.closest('button')).toHaveAttribute('aria-busy', 'true'))
      fireEvent.click(row)
      expect(linkMirror).toHaveBeenCalledTimes(1)
      release({ ok: true })
    })
  })

  describe('offers — channels the session does not hold', () => {
    it('connects an offered mirror target, named by destination', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-target-label' })))
      await waitFor(() => expect(linkMirror).toHaveBeenCalledWith(SLOT, 'discord', 'zzq-target'))
    })

    it('routes a Slack offer through the slack-link path and stores the binding', async () => {
      channelTargets.mockResolvedValue([
        target({ channel_type: 'slack', target_id: 'dm', label: 'Slack · Direct Message' }),
      ] as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'Slack · Direct Message' })))
      await waitFor(() => expect(slackLink).toHaveBeenCalledWith(SLOT, 'dm'))
      expect(linkMirror).not.toHaveBeenCalled()
      await waitFor(() => expect(slotOf(store).slack_linked).toBe(true))
      expect(slotOf(store).slack_channel).toBe('C-zzq')
      expect(slotOf(store).slack_thread_ts).toBe('1.2')
    })

    it('a not-ok Slack response leaves the slot untouched', async () => {
      channelTargets.mockResolvedValue([
        target({ channel_type: 'slack', target_id: 'dm', label: 'Slack · Direct Message' }),
      ] as never)
      slackLink.mockResolvedValue({ ok: false } as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'Slack · Direct Message' })))
      await waitFor(() => expect(slackLink).toHaveBeenCalled())
      expect(slotOf(store).slack_linked).toBeUndefined()
      expect(slotOf(store).slack_channel).toBeUndefined()
    })

    it('a failed Slack connect is reported in the notification feed', async () => {
      channelTargets.mockResolvedValue([
        target({ channel_type: 'slack', target_id: 'dm', label: 'Slack · Direct Message' }),
      ] as never)
      slackLink.mockRejectedValue(new Error('zzq-slack-refused'))
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'Slack · Direct Message' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('connect_failed', { label: 'Slack', reason: 'zzq-slack-refused' })}`,
      ]))
    })

    it('reports a conversation held by another session as in use, not as an error', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      linkMirror.mockRejectedValue(
        new ApiError(409, 'conflict', JSON.stringify({ code: 'conversation_occupied' })),
      )
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-target-label' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('held_elsewhere', { label: 'zzq-target-label' })}`,
      ]))
    })

    it('a 409 with a different code stays a plain connect failure', async () => {
      // The endpoint answers 409 for an unavailable target too — the status alone
      // must not be read as occupied.
      channelTargets.mockResolvedValue([target()] as never)
      linkMirror.mockRejectedValue(
        new ApiError(409, 'zzq-target-down', JSON.stringify({ code: 'configured_target_unavailable' })),
      )
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-target-label' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('connect_failed', { label: 'zzq-target-label', reason: 'zzq-target-down' })}`,
      ]))
    })

    it('a non-Error connect failure falls back to the generic reason', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      linkMirror.mockRejectedValue('zzq-not-an-error')
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-target-label' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('connect_failed', { label: 'zzq-target-label', reason: L('unknown_error') })}`,
      ]))
    })

    it('an unavailable target explains itself visibly and refuses the click', async () => {
      channelTargets.mockResolvedValue([
        target({ available: false, unavailable_reason: 'zzq-transport-absent' }),
      ] as never)
      const { store } = mount()
      const row = await screen.findByText(L('connect_to', { label: 'zzq-target-label' }))
      // The reason is visible text, not tooltip-only — a keyboard or touch user
      // never sees a title attribute.
      expect(screen.getByText('zzq-transport-absent')).toBeInTheDocument()
      const button = row.closest('button')!
      expect(button).toHaveAttribute('aria-disabled', 'true')
      expect(button).toHaveAttribute('title', 'zzq-transport-absent')
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

    it('an offer with no label of its own falls back to the brand label', async () => {
      channelTargets.mockResolvedValue([target({ label: '' })] as never)
      mount()
      expect(await screen.findByText(L('connect_to', { label: 'Discord' }))).toBeInTheDocument()
    })

    it('does not offer a second conversation on a channel the session already holds', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      mount({ links: [link()] })
      await waitFor(() => expect(channelTargets).toHaveBeenCalled())
      expect(screen.queryByText(L('connect_to', { label: 'zzq-target-label' })))
        .not.toBeInTheDocument()
      expect(screen.getByText(L('disconnect_from', { label: 'Discord' }))).toBeInTheDocument()
    })

    it('a non-array payload degrades to an empty picker instead of throwing', async () => {
      channelTargets.mockResolvedValue({ oops: true } as never)
      const { container } = mount()
      await waitFor(() => expect(channelTargets).toHaveBeenCalled())
      expect(container.querySelectorAll('button')).toHaveLength(0)
    })

    it('renders nothing while the target list has not resolved', async () => {
      let release: (v: unknown) => void = () => {}
      channelTargets.mockReturnValue(new Promise(r => { release = r }) as never)
      const { container } = mount()
      await waitFor(() => expect(channelTargets).toHaveBeenCalled())
      expect(container.textContent).toBe('')
      release([])
    })
  })

  it('never invents a row from slack_linked alone — rows come from the wire', async () => {
    // An invented row cannot know `paused`, which is how a disconnected thread
    // once rendered as connected.
    const { container } = mount({ slack_linked: true })
    await waitFor(() => expect(channelTargets).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('behaves identically inside the context-menu family', async () => {
    channelTargets.mockResolvedValue([target()] as never)
    mount({}, 'context')
    fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-target-label' })))
    await waitFor(() => expect(linkMirror).toHaveBeenCalledWith(SLOT, 'discord', 'zzq-target'))
  })

  it('renders nothing at all for an unknown slot key', async () => {
    channelTargets.mockResolvedValue([] as never)
    const { container } = renderWithProviders(
      <LinkedSurfacesSection slotKey="zzq-missing" variant="dropdown" />,
    )
    await waitFor(() => expect(channelTargets).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
})
