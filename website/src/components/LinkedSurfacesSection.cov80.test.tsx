// LinkedSurfacesSection — ONE row per channel whose LABEL is the action:
// `Disconnect from X` while output flows there, `Connect to X` otherwise.
// The role/offline badges, reminder and release items were deliberately
// removed; these tests exercise the current contract only.
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders, createTestStore } from '../test/helpers'
import LinkedSurfacesSection from './LinkedSurfacesSection'
import { addSlotOptimistic, sseSlots } from '../store/dashboardSlice'
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
      unlinkMirror: vi.fn(),
      unlinkSlack: vi.fn(),
      chatSlots: vi.fn(),
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
const unlinkMirror = vi.mocked(api.unlinkMirror)
const unlinkSlack = vi.mocked(api.unlinkSlack)
const chatSlots = vi.mocked(api.chatSlots)

const SLOT = 'zzq-slot'
const L = (k: string, vars?: Record<string, unknown>) =>
  i18nT(`components.linkedSurfacesSection.${k}`, vars)

function link(over: Partial<SessionLink> = {}): SessionLink {
  const base: SessionLink = {
    channel: 'discord', label: 'zzq-guild', target: 't-1', binding: 'b-1', direction: 'out', live: true, ...over,
  }
  // `drives_session` as the projection emits it — a resume (`both`) mirror, a
  // Slack thread and the born-in conversation drive the session, a one-way
  // mirror does not — unless the test sets it, so a fixture can carry the
  // field alone or withhold it (a cached pre-field payload).
  return {
    drives_session: base.direction === 'both' || base.direction === 'origin' || base.channel === 'slack',
    ...base,
  }
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
    unlinkMirror.mockResolvedValue({ ok: true, was_linked: true } as never)
    unlinkSlack.mockResolvedValue({ ok: true, was_linked: true } as never)
  })

  describe('bound-channel rows', () => {
    it('a connected channel reads Disconnect, under the brand label', async () => {
      mount({ links: [link()] })
      expect(await screen.findByText(L('disconnect_from', { label: 'Discord' }))).toBeInTheDocument()
    })

    it('a paused explicit binding reads Resume replies — the verb names the state its sub-line describes', async () => {
      // Same row, same click as `Connect`, but under a sub-line that says the
      // link still stands a "Connect" verb read as a second link rather than a
      // resume: "the title says 'Connect' but the small text describes the
      // current paused state… I would not be confident which one I was about to
      // do." The verb now names what the click ends.
      mount({ links: [link({ paused: true })] })
      expect(await screen.findByText(L('resume_replies_to', { label: 'Discord' }))).toBeInTheDocument()
      expect(screen.queryByText(L('connect_to', { label: 'Discord' }))).not.toBeInTheDocument()
    })

    it('a paused born-in conversation still reads Connect: no sub-line, nothing to contradict', async () => {
      // A muted origin row carries no "the link stays" line (it is not
      // severable, so there is no binding to talk about), and its plain verb
      // already tells the whole truth. Only the row with the sub-line changes.
      mount({ links: [link({ direction: 'origin', paused: true })] })
      expect(await screen.findByText(L('connect_to', { label: 'Discord' }))).toBeInTheDocument()
      expect(screen.queryByText(L('resume_replies_to', { label: 'Discord' }))).not.toBeInTheDocument()
    })

    it('an origin row is a normal control, not a badge', async () => {
      mount({ links: [link({ direction: 'origin' })] })
      expect(await screen.findByText(L('disconnect_from', { label: 'Discord' }))).toBeInTheDocument()
    })

    it.each([
      ['imessage', 'iMessage'],
      ['feishu', 'Feishu'],
    ])('renders the %s brand instead of the target label', async (channel, brand) => {
      mount({ links: [link({ channel, label: 'zzq-personal-target' })] })
      // By text, not by exact accessible name: the row's name now also carries
      // the Disconnect sub-line (`disconnect_outcome`), and the label is what
      // this pins.
      expect(
        await screen.findByText(L('disconnect_from', { label: brand })),
      ).toBeInTheDocument()
      expect(screen.queryByText(L('disconnect_from', { label: 'zzq-personal-target' }))).not.toBeInTheDocument()
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
      expect(await screen.findByText(L('resume_replies_to', { label: 'Slack' }))).toBeInTheDocument()
      expect(pauseMirror).not.toHaveBeenCalled()
    })

    it('reconnecting a paused channel sends paused=false and patches the store', async () => {
      const { store } = mount({ links: [link({ paused: true })] })
      fireEvent.click(await screen.findByText(L('resume_replies_to', { label: 'Discord' })))
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
      // The notification is the durable record; the failure ALSO renders in
      // place under the row (a toast-only report of a write that did not
      // persist is the shape errors-use-error-notice forbids).
      const inPlace = screen.getByTestId('linked-surfaces-error-discord')
      expect(inPlace).toHaveAttribute('role', 'alert')
      expect(inPlace).toHaveTextContent('zzq-pause-broke')
      expect(slotOf(store).links?.[0].paused).toBeUndefined()
    })

    it('a failed connect on a paused row reports connect_failed', async () => {
      pauseMirror.mockRejectedValue(new Error('zzq-resume-broke'))
      const { store } = mount({ links: [link({ paused: true })] })
      fireEvent.click(await screen.findByText(L('resume_replies_to', { label: 'Discord' })))
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

  describe('the Unlink action', () => {
    // Disconnect PAUSES a mirror and keeps its binding (the header comment records
    // that), so a session whose mirror is merely paused is still refused by
    // session control and still driven from the channel. Unlink is the distinct
    // action that severs the binding, and it is offered on every explicitly
    // bound channel beside the Disconnect/Connect row.

    it('a connected channel offers Unlink beside Disconnect, and Unlink calls unlinkMirror', async () => {
      const { store } = mount({ links: [link({ direction: 'both' })] })
      expect(await screen.findByText(L('disconnect_from', { label: 'Discord' }))).toBeInTheDocument()
      // The two verbs are near-synonyms to a cold reader, so BOTH items name
      // their outcome under the label and define each other: Disconnect pauses
      // and keeps the link, Unlink removes it (and says reconnecting brings it
      // back). A sub-line under Unlink alone leaves nothing to compare it against.
      expect(screen.getByText(L('disconnect_outcome'))).toBeInTheDocument()
      expect(screen.getByText(L('unlink_outcome', { label: 'Discord' }))).toBeInTheDocument()
      fireEvent.click(screen.getByText(L('unlink_from', { label: 'Discord' })))
      // The request names the row's opaque binding token, never its display tail:
      // the server recomputes the token from the binding it holds and refuses a
      // row drawn from one that has since been replaced.
      await waitFor(() => expect(unlinkMirror).toHaveBeenCalledWith(SLOT, { channel_type: 'discord', binding: 'b-1' }))
      expect(pauseMirror).not.toHaveBeenCalled()
      // The binding is gone from the store, so the menu reads as unlinked: no
      // Disconnect, no Unlink, and the channel is free to be offered again.
      await waitFor(() => expect(slotOf(store).links).toEqual([]))
      expect(screen.queryByText(L('disconnect_from', { label: 'Discord' }))).not.toBeInTheDocument()
      expect(screen.queryByText(L('unlink_from', { label: 'Discord' }))).not.toBeInTheDocument()
      expect(notifications(store)).toEqual([])
    })

    it('a PAUSED channel is still linked: it reads Resume replies AND offers Unlink', async () => {
      // The middle state. Without the sub-line and the Unlink item a paused row is
      // indistinguishable from a channel that was never connected, while the
      // binding it keeps still routes inbound messages here and locks the session
      // out of session control. The sub-line says the consequence, not "linked"
      // (which collided with the neighbouring "Copy link" item).
      mount({ links: [link({ direction: 'both', paused: true })] })
      expect(await screen.findByText(L('resume_replies_to', { label: 'Discord' }))).toBeInTheDocument()
      expect(screen.getByText(L('unlink_from', { label: 'Discord' }))).toBeInTheDocument()
      expect(screen.getByText(L('still_linked'))).toBeInTheDocument()
      expect(screen.queryByText(L('still_linked_out'))).not.toBeInTheDocument()
      // The connected-state line belongs under `Disconnect` only: under a row
      // that reads `Connect` it would claim a pause that has already happened.
      expect(screen.queryByText(L('disconnect_outcome'))).not.toBeInTheDocument()
      fireEvent.click(screen.getByText(L('unlink_from', { label: 'Discord' })))
      await waitFor(() => expect(unlinkMirror).toHaveBeenCalledWith(SLOT, { channel_type: 'discord', binding: 'b-1' }))
    })

    it('a one-way mirror names what a one-way mirror does: replies, not driving', async () => {
      // An `out` binding only receives this session's replies; messages sent
      // there do not land here. Both consequence lines say so instead of
      // claiming the two-way behaviour.
      mount({ links: [link({ direction: 'out', paused: true })] })
      expect(await screen.findByText(L('resume_replies_to', { label: 'Discord' }))).toBeInTheDocument()
      expect(screen.getByText(L('still_linked_out'))).toBeInTheDocument()
      expect(screen.queryByText(L('still_linked'))).not.toBeInTheDocument()
      expect(screen.getByText(L('unlink_outcome_out', { label: 'Discord' }))).toBeInTheDocument()
      expect(screen.queryByText(L('unlink_outcome', { label: 'Discord' }))).not.toBeInTheDocument()
    })

    it('a paused Slack thread is two-way whatever the wire calls its direction: replies there still land here', async () => {
      // The projection marks every Slack row `out` — its inbound routing is the
      // thread index, not the mirror's inbound marker — yet a reply in the linked
      // thread still resumes this session while the link stands, and Unlink
      // evicts that index. The row says so in `drives_session`, and the
      // sub-lines follow that, not the direction.
      mount({
        slack_linked: true, slack_channel: 'C-zzq', slack_thread_ts: '1.2',
        links: [link({ channel: 'slack', label: 'zzq-slack', direction: 'out', paused: true, drives_session: true })],
      })
      expect(await screen.findByText(L('resume_replies_to', { label: 'Slack' }))).toBeInTheDocument()
      expect(screen.getByText(L('still_linked'))).toBeInTheDocument()
      expect(screen.queryByText(L('still_linked_out'))).not.toBeInTheDocument()
      expect(screen.getByText(L('unlink_outcome', { label: 'Slack' }))).toBeInTheDocument()
      expect(screen.queryByText(L('unlink_outcome_out', { label: 'Slack' }))).not.toBeInTheDocument()
    })

    it('the sub-lines read the wire, not the channel or direction: a row without drives_session reads one-way', async () => {
      // The inbound-routing fact is the server's. Re-derived here from
      // `direction` plus the channel name, a paused Slack row reads as a
      // one-way link, so the component reads the field alone: a `both`
      // mirror or a Slack thread whose payload predates the field (a cached
      // slots frame) reads as not driving until the next push redraws it.
      mount({
        slack_linked: true, slack_channel: 'C-zzq', slack_thread_ts: '1.2',
        links: [
          link({ channel: 'slack', label: 'zzq-slack', direction: 'out', paused: true, drives_session: undefined }),
          link({ channel: 'telegram', label: 'zzq-tg', target: 'tg-1', direction: 'both', paused: true, drives_session: undefined }),
        ],
      })
      expect(await screen.findByText(L('resume_replies_to', { label: 'Slack' }))).toBeInTheDocument()
      expect(screen.getByText(L('resume_replies_to', { label: 'Telegram' }))).toBeInTheDocument()
      expect(screen.getAllByText(L('still_linked_out'))).toHaveLength(2)
      expect(screen.queryByText(L('still_linked'))).not.toBeInTheDocument()
      expect(screen.getByText(L('unlink_outcome_out', { label: 'Slack' }))).toBeInTheDocument()
      expect(screen.getByText(L('unlink_outcome_out', { label: 'Telegram' }))).toBeInTheDocument()
    })

    it('a row from a cached pre-binding payload sends an empty token, so the server refuses it as stale', async () => {
      mount({ links: [link({ direction: 'both', binding: undefined })] })
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'Discord' })))
      await waitFor(() => expect(unlinkMirror).toHaveBeenCalledWith(SLOT, { channel_type: 'discord', binding: '' }))
    })

    it('an origin-only channel has no Unlink: the conversation IS the session', async () => {
      mount({ links: [link({ direction: 'origin' })] })
      expect(await screen.findByText(L('disconnect_from', { label: 'Discord' }))).toBeInTheDocument()
      expect(screen.queryByText(L('unlink_from', { label: 'Discord' }))).not.toBeInTheDocument()
      // The Disconnect sub-line rides on EVERY connected Disconnect, an Unlink
      // beneath it or not: a reader who learned the line on a mirrored session's
      // menu and then meets a bare Disconnect here cannot tell whether this one
      // is the gentle pause. It is — the conversation stays bound.
      expect(screen.getByText(L('disconnect_outcome'))).toBeInTheDocument()
    })

    it('a channel the session was born in has no Unlink, even beside its own self-mirror row', async () => {
      // A Discord-born session carries two rows for its own conversation: the
      // `origin` row and the self-mirror the dispatcher binds on every inbound
      // turn (`both`, since Discord resumes inbound). Severing that mirror would
      // leave dashboard-taken turns reaching nobody until the next inbound
      // message rebinds it, so the group is judged by its origin row: no Unlink,
      // no "still linked" sub-line, one Disconnect row as before.
      mount({
        links: [link({ direction: 'origin', target: 'o-1' }), link({ direction: 'both', target: 'm-1' })],
      })
      expect(await screen.findByText(L('disconnect_from', { label: 'Discord' }))).toBeInTheDocument()
      expect(screen.queryByText(L('unlink_from', { label: 'Discord' }))).not.toBeInTheDocument()
      expect(screen.getByText(L('disconnect_outcome'))).toBeInTheDocument()
      fireEvent.click(screen.getByText(L('disconnect_from', { label: 'Discord' })))
      await waitFor(() => expect(pauseMirror).toHaveBeenCalledTimes(2))
      expect(screen.queryByText(L('still_linked'))).not.toBeInTheDocument()
      expect(screen.queryByText(L('still_linked_out'))).not.toBeInTheDocument()
      expect(screen.queryByText(L('unlink_from', { label: 'Discord' }))).not.toBeInTheDocument()
      expect(unlinkMirror).not.toHaveBeenCalled()
    })

    it('a Slack row posts its Unlink to the one endpoint every row uses; the server routes it', async () => {
      // Which store a binding lives in is the server's fact (`mirror-link`
      // refuses Slack on channel type, and `mirror-unlink` hands a `slack` body
      // to the Slack teardown). The menu carries no channel-to-endpoint switch,
      // so the dedicated Slack client call is never made from here — and the
      // slot's Slack fields still clear on success, keyed on the row's token.
      const { store } = mount({
        slack_linked: true, slack_channel: 'C-zzq', slack_thread_ts: '1.2',
        links: [link({ channel: 'slack', label: 'zzq-slack', direction: 'out' })],
      })
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'Slack' })))
      await waitFor(() => expect(unlinkMirror).toHaveBeenCalledWith(SLOT, { channel_type: 'slack', binding: 'b-1' }))
      expect(unlinkSlack).not.toHaveBeenCalled()
      await waitFor(() => expect(slotOf(store).links).toEqual([]))
      expect(slotOf(store).slack_linked).toBe(false)
      expect(slotOf(store).slack_channel).toBeUndefined()
      expect(slotOf(store).slack_thread_ts).toBeUndefined()
    })

    it('a failed unlink is reported in place with the backend reason and the row stays', async () => {
      unlinkMirror.mockRejectedValue(new Error('zzq-unlink-broke'))
      const { store } = mount({ links: [link({ direction: 'both' })] })
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'Discord' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('unlink_failed', { label: 'Discord', reason: 'zzq-unlink-broke' })}`,
      ]))
      expect(screen.getByTestId('linked-surfaces-error-discord')).toHaveTextContent('zzq-unlink-broke')
      expect(slotOf(store).links).toHaveLength(1)
      // An ordinary failure keeps the item live: the row is still the binding
      // and a retry is the right next click. Only the STALE refusal dims it.
      expect(screen.getByText(L('unlink_from', { label: 'Discord' })).closest('button'))
        .not.toHaveAttribute('aria-disabled')
    })

    it('a stale row is refused by the server, reported as out of date, and the slots refetched', async () => {
      // The row this tab drew is no longer the slot's binding (another tab
      // rebound it to Telegram). The server compares the named binding and
      // answers 409 `mirror_changed` without clearing anything; the menu says so
      // and asks for a fresh slots frame, which replaces the stale Discord row
      // with the Telegram row that is really there.
      unlinkMirror.mockRejectedValue(
        new ApiError(409, 'changed', JSON.stringify({ code: 'mirror_changed' })),
      )
      chatSlots.mockResolvedValue([{
        key: SLOT, messages: 0, running: false,
        links: [link({ channel: 'telegram', label: 'zzq-tg', target: 'tg-1', direction: 'both' })],
      }] as never)
      const { store } = mount({ links: [link({ direction: 'both' })] })
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'Discord' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('unlink_stale', { label: 'Discord', reason: 'changed' })}`,
      ]))
      await waitFor(() => expect(chatSlots).toHaveBeenCalled())
      await waitFor(() => expect(slotOf(store).links?.map(l => l.channel)).toEqual(['telegram']))
      expect(screen.queryByText(L('unlink_from', { label: 'Discord' }))).not.toBeInTheDocument()
      expect(screen.getByText(L('unlink_from', { label: 'Telegram' }))).toBeInTheDocument()
    })

    it('a stale row dims its Unlink under the notice, and dismissing the notice restores it', async () => {
      // The refetch can redraw the SAME row — the binding was re-linked on the
      // same channel, or this tab's row was simply out of date — and then the
      // menu read "no longer linked… Nothing was unlinked" over a live "Unlink
      // from Discord" directly beneath. The item is dimmed and inert for as
      // long as the stale notice shows; dismissing the notice (or clicking the
      // row again) lifts both together.
      unlinkMirror.mockRejectedValueOnce(
        new ApiError(409, 'changed', JSON.stringify({ code: 'mirror_changed' })),
      )
      chatSlots.mockResolvedValue([{
        key: SLOT, messages: 0, running: false, links: [link({ direction: 'both' })],
      }] as never)
      mount({ links: [link({ direction: 'both' })] })
      const item = () => screen.getByText(L('unlink_from', { label: 'Discord' })).closest('button')!
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'Discord' })))
      await waitFor(() => expect(screen.getByTestId('linked-surfaces-error-discord')).toBeInTheDocument())
      await waitFor(() => expect(chatSlots).toHaveBeenCalled())
      // Redrawn with the same row: the notice still shows, so the item is dimmed
      // and a click on it sends nothing. The toggle row's own consequence line
      // ("the link stays") is withheld too — beside a notice that says the row
      // was out of date, it would make the row disagree with itself.
      expect(item()).toHaveAttribute('aria-disabled', 'true')
      expect(screen.queryByText(L('disconnect_outcome'))).not.toBeInTheDocument()
      expect(screen.getByText(L('disconnect_from', { label: 'Discord' }))).toBeInTheDocument()
      fireEvent.click(item())
      expect(unlinkMirror).toHaveBeenCalledTimes(1)
      fireEvent.click(within(screen.getByTestId('linked-surfaces-error-discord'))
        .getByRole('button', { name: i18nT('components.errorNotice.dismiss') }))
      expect(screen.queryByTestId('linked-surfaces-error-discord')).not.toBeInTheDocument()
      expect(item()).not.toHaveAttribute('aria-disabled')
      expect(screen.getByText(L('disconnect_outcome'))).toBeInTheDocument()
      unlinkMirror.mockResolvedValueOnce({ ok: true, was_linked: true } as never)
      fireEvent.click(item())
      await waitFor(() => expect(unlinkMirror).toHaveBeenCalledTimes(2))
    })

    it('a click on Unlink while its mutation is in flight is swallowed', async () => {
      let release: (v: unknown) => void = () => {}
      unlinkMirror.mockReturnValue(new Promise(r => { release = r }) as never)
      mount({ links: [link({ direction: 'both' })] })
      const row = await screen.findByText(L('unlink_from', { label: 'Discord' }))
      fireEvent.click(row)
      await waitFor(() => expect(row.closest('button')).toHaveAttribute('aria-busy', 'true'))
      fireEvent.click(row)
      expect(unlinkMirror).toHaveBeenCalledTimes(1)
      release({ ok: true, was_linked: true })
    })

    it('a PAUSED stale row falls back to the plain Connect verb while its notice shows', async () => {
      // "Resume replies to Discord" asserts the link the stale notice ("no
      // longer linked… Nothing was unlinked") says is gone — the same
      // self-contradiction the withheld consequence line would be. The plain
      // verb rides with the notice and the resume verb returns when it goes.
      unlinkMirror.mockRejectedValueOnce(
        new ApiError(409, 'changed', JSON.stringify({ code: 'mirror_changed' })),
      )
      chatSlots.mockResolvedValue([{
        key: SLOT, messages: 0, running: false, links: [link({ direction: 'both', paused: true })],
      }] as never)
      mount({ links: [link({ direction: 'both', paused: true })] })
      expect(await screen.findByText(L('resume_replies_to', { label: 'Discord' }))).toBeInTheDocument()
      fireEvent.click(screen.getByText(L('unlink_from', { label: 'Discord' })))
      await waitFor(() => expect(screen.getByTestId('linked-surfaces-error-discord')).toBeInTheDocument())
      await waitFor(() => expect(chatSlots).toHaveBeenCalled())
      expect(screen.getByText(L('connect_to', { label: 'Discord' }))).toBeInTheDocument()
      expect(screen.queryByText(L('resume_replies_to', { label: 'Discord' }))).not.toBeInTheDocument()
      expect(screen.queryByText(L('still_linked'))).not.toBeInTheDocument()
      fireEvent.click(within(screen.getByTestId('linked-surfaces-error-discord'))
        .getByRole('button', { name: i18nT('components.errorNotice.dismiss') }))
      expect(screen.getByText(L('resume_replies_to', { label: 'Discord' }))).toBeInTheDocument()
      expect(screen.getByText(L('still_linked'))).toBeInTheDocument()
    })

    it('a binding replaced between the request and its response survives the completion', async () => {
      // Unlink A; before A's response lands, another tab has unlinked A and
      // linked B on the same channel, and B's slots push reached this tab
      // first. The server deleted exactly A, so the completion must remove
      // exactly A's row — B is a binding the server still holds, and a tab
      // that dropped it would read as disconnected from a live mirror.
      let release: (v: unknown) => void = () => {}
      unlinkMirror.mockReturnValue(new Promise(r => { release = r }) as never)
      const { store } = mount({ links: [link({ direction: 'both', binding: 'b-1' })] })
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'Discord' })))
      await waitFor(() => expect(unlinkMirror).toHaveBeenCalledWith(SLOT, { channel_type: 'discord', binding: 'b-1' }))
      store.dispatch(sseSlots([{
        key: SLOT, messages: 0, running: false,
        links: [link({ direction: 'both', binding: 'b-2', target: 't-2' })],
      } as ChatSlot]))
      expect(slotOf(store).links?.map(l => l.binding)).toEqual(['b-2'])
      release({ ok: true, was_linked: true })
      // Give the completion its turn, then assert B is still there — a wait
      // for its absence would pass on the broken code, so the presence is what
      // is awaited and the menu still offers B's own controls.
      await waitFor(() => expect(unlinkMirror).toHaveBeenCalledTimes(1))
      await new Promise(r => setTimeout(r, 20))
      expect(slotOf(store).links?.map(l => l.binding)).toEqual(['b-2'])
      expect(screen.getByText(L('disconnect_from', { label: 'Discord' }))).toBeInTheDocument()
      expect(screen.getByText(L('unlink_from', { label: 'Discord' }))).toBeInTheDocument()
    })

    it('a Slack thread replaced mid-flight keeps its rows and the slot fields', async () => {
      // Same race on the Slack path: the completion must not clear
      // `slack_linked` / `slack_channel` / `slack_thread_ts` for a thread the
      // request did not name.
      let release: (v: unknown) => void = () => {}
      unlinkMirror.mockReturnValue(new Promise(r => { release = r }) as never)
      const { store } = mount({
        slack_linked: true, slack_channel: 'C-zzq', slack_thread_ts: '1.2',
        links: [link({ channel: 'slack', label: 'zzq-slack', direction: 'out', binding: 'b-1' })],
      })
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'Slack' })))
      await waitFor(() => expect(unlinkMirror).toHaveBeenCalledWith(SLOT, { channel_type: 'slack', binding: 'b-1' }))
      store.dispatch(sseSlots([{
        key: SLOT, messages: 0, running: false,
        slack_linked: true, slack_channel: 'C-zzq', slack_thread_ts: '3.4',
        links: [link({ channel: 'slack', label: 'zzq-slack', direction: 'out', binding: 'b-2', target: 't-2' })],
      } as ChatSlot]))
      release({ ok: true, was_linked: true })
      await new Promise(r => setTimeout(r, 20))
      expect(slotOf(store).links?.map(l => l.binding)).toEqual(['b-2'])
      expect(slotOf(store).slack_linked).toBe(true)
      expect(slotOf(store).slack_channel).toBe('C-zzq')
      expect(slotOf(store).slack_thread_ts).toBe('3.4')
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
