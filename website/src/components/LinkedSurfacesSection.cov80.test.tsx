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
    'aria-describedby'?: string
    title?: string
    className?: string
  }) => (
    <button
      type="button"
      aria-disabled={rest['aria-disabled']}
      aria-busy={rest['aria-busy']}
      aria-describedby={rest['aria-describedby']}
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

function mount(
  slot: Partial<ChatSlot> = {},
  variant: 'dropdown' | 'context' = 'dropdown',
  channel?: string,
) {
  const store = createTestStore()
  store.dispatch(addSlotOptimistic({
    key: SLOT, messages: 0, running: false, ...slot,
  } as ChatSlot))
  const view = renderWithProviders(
    <LinkedSurfacesSection slotKey={SLOT} variant={variant} channel={channel} />,
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
      expect(await screen.findByText(L('disconnect_from', { label: 'zzq-guild' }))).toBeInTheDocument()
    })

    it('a paused explicit binding reads Resume replies — the verb names the state its sub-line describes', async () => {
      // Same row, same click as `Connect`, but under a sub-line that says the
      // link still stands a "Connect" verb read as a second link rather than a
      // resume: "the title says 'Connect' but the small text describes the
      // current paused state… I would not be confident which one I was about to
      // do." The verb now names what the click ends.
      mount({ links: [link({ paused: true })] })
      expect(await screen.findByText(L('resume_replies_to', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.queryByText(L('connect_to', { label: 'zzq-guild' }))).not.toBeInTheDocument()
    })

    it('a paused born-in conversation reads Resume replies under its tag, says it is paused, and says why it has no Unlink', async () => {
      // Bound, so it carries the direction tag; under a tag asserting the
      // connection stands, a bare `Connect` read as a second link rather than a
      // resume — the contradiction the mirrored row fixed — so the paused
      // born-in row names the state it ends too. It says "paused" in the same
      // consequence line the mirrored row uses (with only the verb saying it, a
      // reader found the menu itself said "paused" nowhere), ABOVE the note
      // that explains the missing Unlink: state first, then why.
      mount({ links: [link({ direction: 'origin', paused: true })] })
      expect(await screen.findByText(L('resume_replies_to', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.queryByText(L('connect_to', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      expect(screen.getByText(L('direction_two_way', { label: 'zzq-guild' }))).toBeInTheDocument()
      const paused = screen.getByText(L('still_linked'))
      const note = screen.getByText(L('born_in_note', { label: 'zzq-guild' }))
      expect(paused.compareDocumentPosition(note) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
      expect(screen.queryByText(L('disconnect_outcome'))).not.toBeInTheDocument()
      expect(screen.queryByText(L('unlink_from', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      fireEvent.click(screen.getByText(L('resume_replies_to', { label: 'zzq-guild' })))
      await waitFor(() => expect(pauseMirror).toHaveBeenCalledWith(SLOT, false, true))
    })

    it('an origin row is a normal control, not a badge', async () => {
      mount({ links: [link({ direction: 'origin' })] })
      expect(await screen.findByText(L('disconnect_from', { label: 'zzq-guild' }))).toBeInTheDocument()
    })

    it.each([
      ['imessage', 'iMessage'],
      ['feishu', 'Feishu'],
    ])('renders the wire label of a %s row, not the client brand table', async (channel, brand) => {
      // One destination, one name: the header chip reads the link row's label
      // as the server sent it, so the row does too, and the offer for the same
      // destination is that same form with the destination on it. A brand
      // table kept here gave the row a third name ("Discord" beside the chip's
      // "Discord DM" and the offer's "Discord DM · 1234").
      mount({ links: [link({ channel, label: 'zzq-wire-label' })] })
      // By text, not by exact accessible name: the row's name now also carries
      // the Disconnect sub-line (`disconnect_outcome`), and the label is what
      // this pins.
      expect(
        await screen.findByText(L('disconnect_from', { label: 'zzq-wire-label' })),
      ).toBeInTheDocument()
      expect(screen.queryByText(L('disconnect_from', { label: brand }))).not.toBeInTheDocument()
    })

    it('a wire row with no label falls back to the brand table, then to the channel type', async () => {
      mount({ links: [link({ channel: 'imessage', label: '' })] })
      expect(await screen.findByText(L('disconnect_from', { label: 'iMessage' }))).toBeInTheDocument()
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
      const rows = await screen.findAllByText(L('disconnect_from', { label: 'zzq-guild' }))
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
      fireEvent.click(await screen.findByText(L('disconnect_from', { label: 'zzq-guild' })))
      await waitFor(() => expect(pauseMirror).toHaveBeenCalledTimes(2))
      expect(pauseMirror).toHaveBeenCalledWith(SLOT, true, false)
    })

    it('a Slack row toggles through the slack-pause path and flips the verb', async () => {
      const { store } = mount({ links: [link({ channel: 'slack', label: 'zzq-slack' })] })
      fireEvent.click(await screen.findByText(L('disconnect_from', { label: 'zzq-slack' })))
      await waitFor(() => expect(pauseSlack).toHaveBeenCalledWith(SLOT, true))
      await waitFor(() => expect(slotOf(store).links?.[0].paused).toBe(true))
      expect(await screen.findByText(L('resume_replies_to', { label: 'zzq-slack' }))).toBeInTheDocument()
      expect(pauseMirror).not.toHaveBeenCalled()
    })

    it('reconnecting a paused channel sends paused=false and patches the store', async () => {
      const { store } = mount({ links: [link({ paused: true })] })
      fireEvent.click(await screen.findByText(L('resume_replies_to', { label: 'zzq-guild' })))
      await waitFor(() => expect(pauseMirror).toHaveBeenCalledWith(SLOT, false, false))
      await waitFor(() => expect(slotOf(store).links?.[0].paused).toBe(false))
    })

    it('a failed disconnect is reported with the backend reason and the row stays connected', async () => {
      pauseMirror.mockRejectedValue(new Error('zzq-pause-broke'))
      const { store } = mount({ links: [link()] })
      fireEvent.click(await screen.findByText(L('disconnect_from', { label: 'zzq-guild' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('disconnect_failed', { label: 'zzq-guild', reason: 'zzq-pause-broke' })}`,
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
      fireEvent.click(await screen.findByText(L('resume_replies_to', { label: 'zzq-guild' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('connect_failed', { label: 'zzq-guild', reason: 'zzq-resume-broke' })}`,
      ]))
    })

    it('a non-Error failure falls back to the generic reason', async () => {
      pauseSlack.mockRejectedValue('zzq-not-an-error')
      const { store } = mount({ links: [link({ channel: 'slack', label: 'zzq-slack' })] })
      fireEvent.click(await screen.findByText(L('disconnect_from', { label: 'zzq-slack' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('disconnect_failed', { label: 'zzq-slack', reason: L('unknown_error') })}`,
      ]))
    })

    it('a click on a row whose mutation is in flight is swallowed', async () => {
      let release: (v: unknown) => void = () => {}
      pauseMirror.mockReturnValue(new Promise(r => { release = r }) as never)
      mount({ links: [link()] })
      const row = await screen.findByText(L('disconnect_from', { label: 'zzq-guild' }))
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
      const offer = await screen.findByText(L('connect_to', { label: 'zzq-target-label' }))
      // Bound to nothing yet, so no direction to label.
      expect(screen.queryByText(L('direction_one_way', { label: 'zzq-target-label' }))).not.toBeInTheDocument()
      expect(screen.queryByText(L('direction_two_way', { label: 'zzq-target-label' }))).not.toBeInTheDocument()
      fireEvent.click(offer)
      await waitFor(() => expect(linkMirror).toHaveBeenCalledWith(SLOT, 'discord', 'zzq-target'))
      expect(notifications(store)).toEqual([])
      // Deliberately NO onSuccess store write: the link row arrives via refetch,
      // never from a captured snapshot that could drop a concurrent toggle's row.
      expect(slotOf(store).links).toBeUndefined()
    })

    it('an offer keeps the channel form on its verb and moves the discriminator into its sub-line', async () => {
      // The server labels a target "<channel form> · <which one>". On the verb
      // line the tail read as a raw number on a button ("I don't know whose
      // number that is"); it now sits in the muted sub-line the bound rows use,
      // and a bare id under a `user:` target is named for what it is. A human
      // tail is shown as sent. Red on the head that rendered the whole label
      // on the verb line.
      channelTargets.mockResolvedValue([
        target({ channel_type: 'discord', target_id: 'user:118273645', label: 'Discord DM · 118273645' }),
        target({ channel_type: 'slack', target_id: 'C-eng', label: 'Slack · #eng' }),
        target({ channel_type: 'whatsapp', target_id: 'user:me', label: 'WhatsApp · yourself' }),
      ] as never)
      mount()
      const discord = await screen.findByText(L('connect_to', { label: 'Discord DM' }))
      expect(discord.closest('button')).toHaveTextContent(L('direct_message_id', { id: '118273645' }))
      expect(screen.queryByText(L('connect_to', { label: 'Discord DM · 118273645' }))).not.toBeInTheDocument()
      expect(screen.getByText(L('connect_to', { label: 'Slack' })).closest('button')).toHaveTextContent('#eng')
      expect(screen.getByText(L('connect_to', { label: 'WhatsApp' })).closest('button')).toHaveTextContent('yourself')
      expect(screen.queryByText(L('direct_message_id', { id: 'me' }))).not.toBeInTheDocument()
      // The click still links the exact destination.
      fireEvent.click(discord)
      await waitFor(() => expect(linkMirror).toHaveBeenCalledWith(SLOT, 'discord', 'user:118273645'))
    })

    it('an unavailable offer shows its discriminator above its reason', async () => {
      channelTargets.mockResolvedValue([
        target({ channel_type: 'slack', target_id: 'C-eng', label: 'Slack · #eng', available: false, unavailable_reason: 'zzq-why' }),
      ] as never)
      mount()
      const item = (await screen.findByText(L('connect_to', { label: 'Slack' }))).closest('button')
      expect(item).toHaveTextContent('#eng')
      expect(item).toHaveTextContent('zzq-why')
    })

    it('a bound channel gets no second offer', async () => {
      channelTargets.mockResolvedValue([target()] as never)
      mount({ links: [link()] })
      await waitFor(() => expect(channelTargets).toHaveBeenCalled())
      expect(screen.queryByText(L('connect_to', { label: 'zzq-target-label' }))).not.toBeInTheDocument()
      expect(screen.getByText(L('disconnect_from', { label: 'zzq-guild' }))).toBeInTheDocument()
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

    it('a failed slack connect is reported under the offer label it was clicked as', async () => {
      slackLink.mockRejectedValue(new Error('zzq-slack-refused'))
      channelTargets.mockResolvedValue([
        target({ channel_type: 'slack', target_id: 'C-dm', label: 'zzq-slack-dm' }),
      ] as never)
      const { store } = mount()
      fireEvent.click(await screen.findByText(L('connect_to', { label: 'zzq-slack-dm' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('connect_failed', { label: 'zzq-slack-dm', reason: 'zzq-slack-refused' })}`,
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
      expect(await screen.findByText(L('disconnect_from', { label: 'zzq-guild' }))).toBeInTheDocument()
      // The two verbs are near-synonyms to a cold reader, so BOTH items name
      // their outcome under the label and define each other: Disconnect pauses
      // and keeps the link, Unlink removes it (and says reconnecting brings it
      // back). A sub-line under Unlink alone leaves nothing to compare it against.
      expect(screen.getByText(L('disconnect_outcome'))).toBeInTheDocument()
      expect(screen.getByText(L('unlink_outcome', { label: 'zzq-guild' }))).toBeInTheDocument()
      fireEvent.click(screen.getByText(L('unlink_from', { label: 'zzq-guild' })))
      // The request names the row's opaque binding token, never its display tail:
      // the server recomputes the token from the binding it holds and refuses a
      // row drawn from one that has since been replaced.
      await waitFor(() => expect(unlinkMirror).toHaveBeenCalledWith(SLOT, { channel_type: 'discord', binding: 'b-1' }))
      expect(pauseMirror).not.toHaveBeenCalled()
      // The binding is gone from the store, so the menu reads as unlinked: no
      // Disconnect, no Unlink, and the channel is free to be offered again.
      await waitFor(() => expect(slotOf(store).links).toEqual([]))
      expect(screen.queryByText(L('disconnect_from', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      expect(screen.queryByText(L('unlink_from', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      expect(notifications(store)).toEqual([])
    })

    it('a PAUSED channel is still linked: it reads Resume replies AND offers Unlink', async () => {
      // The middle state. Without the sub-line and the Unlink item a paused row is
      // indistinguishable from a channel that was never connected, while the
      // binding it keeps still routes inbound messages here and locks the session
      // out of session control. The sub-line says the consequence, not "linked"
      // (which collided with the neighbouring "Copy link" item).
      mount({ links: [link({ direction: 'both', paused: true })] })
      expect(await screen.findByText(L('resume_replies_to', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.getByText(L('unlink_from', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.getByText(L('still_linked'))).toBeInTheDocument()
      // The direction is the row's explicit label, not the caption's business.
      expect(screen.getByText(L('direction_two_way', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.queryByText(L('direction_one_way', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      // The connected-state line belongs under `Disconnect` only: under a row
      // that reads `Connect` it would claim a pause that has already happened.
      expect(screen.queryByText(L('disconnect_outcome'))).not.toBeInTheDocument()
      fireEvent.click(screen.getByText(L('unlink_from', { label: 'zzq-guild' })))
      await waitFor(() => expect(unlinkMirror).toHaveBeenCalledWith(SLOT, { channel_type: 'discord', binding: 'b-1' }))
    })

    it('a one-way mirror is labelled one-way, and its Unlink names what a one-way mirror stops: replies', async () => {
      // An `out` binding only receives this session's replies; messages sent
      // there do not land here. The row's direction label says so — the paused
      // caption is the same sentence on every row, so two rows whose captions
      // differ visibly differ in the label that explains it — and the Unlink
      // line names what stops for THIS kind of link.
      mount({ links: [link({ direction: 'out', paused: true })] })
      expect(await screen.findByText(L('resume_replies_to', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.getByText(L('direction_one_way', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.queryByText(L('direction_two_way', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      expect(screen.getByText(L('still_linked'))).toBeInTheDocument()
      expect(screen.getByText(L('unlink_outcome_out', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.queryByText(L('unlink_outcome', { label: 'zzq-guild' }))).not.toBeInTheDocument()
    })

    it('a paused Slack thread is two-way whatever the wire calls its direction: replies there still land here', async () => {
      // The projection marks every Slack row `out` — its inbound routing is the
      // thread index, not the mirror's inbound marker — yet a reply in the linked
      // thread still resumes this session while the link stands, and Unlink
      // evicts that index. The row says so in `drives_session`, and the label
      // and the Unlink line follow that, not the direction.
      mount({
        slack_linked: true, slack_channel: 'C-zzq', slack_thread_ts: '1.2',
        links: [link({ channel: 'slack', label: 'zzq-slack', direction: 'out', paused: true, drives_session: true })],
      })
      expect(await screen.findByText(L('resume_replies_to', { label: 'zzq-slack' }))).toBeInTheDocument()
      expect(screen.getByText(L('direction_two_way', { label: 'zzq-slack' }))).toBeInTheDocument()
      expect(screen.getByText(L('still_linked'))).toBeInTheDocument()
      expect(screen.getByText(L('unlink_outcome', { label: 'zzq-slack' }))).toBeInTheDocument()
      expect(screen.queryByText(L('unlink_outcome_out', { label: 'zzq-slack' }))).not.toBeInTheDocument()
    })

    it('the label and the Unlink line read the wire, not the channel or direction: a row without drives_session reads one-way', async () => {
      // The inbound-routing fact is the server's. Re-derived here from
      // `direction` plus the channel name, a paused Slack row reads as a
      // two-way link, so the component reads the field alone: a `both`
      // mirror or a Slack thread whose payload predates the field (a cached
      // slots frame) reads as not driving until the next push redraws it.
      mount({
        slack_linked: true, slack_channel: 'C-zzq', slack_thread_ts: '1.2',
        links: [
          link({ channel: 'slack', label: 'zzq-slack', direction: 'out', paused: true, drives_session: undefined }),
          link({ channel: 'telegram', label: 'zzq-tg', target: 'tg-1', direction: 'both', paused: true, drives_session: undefined }),
        ],
      })
      expect(await screen.findByText(L('resume_replies_to', { label: 'zzq-slack' }))).toBeInTheDocument()
      expect(screen.getByText(L('resume_replies_to', { label: 'zzq-tg' }))).toBeInTheDocument()
      expect(screen.getByText(L('direction_one_way', { label: 'zzq-slack' }))).toBeInTheDocument()
      expect(screen.getByText(L('direction_one_way', { label: 'zzq-tg' }))).toBeInTheDocument()
      expect(screen.queryByText(L('direction_two_way', { label: 'zzq-slack' }))).not.toBeInTheDocument()
      expect(screen.queryByText(L('direction_two_way', { label: 'zzq-tg' }))).not.toBeInTheDocument()
      // One paused caption for both rows: it carries no direction to flip.
      expect(screen.getAllByText(L('still_linked'))).toHaveLength(2)
      expect(screen.getByText(L('unlink_outcome_out', { label: 'zzq-slack' }))).toBeInTheDocument()
      expect(screen.getByText(L('unlink_outcome_out', { label: 'zzq-tg' }))).toBeInTheDocument()
    })

    it('a row from a cached pre-binding payload sends an empty token, so the server refuses it as stale', async () => {
      mount({ links: [link({ direction: 'both', binding: undefined })] })
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'zzq-guild' })))
      await waitFor(() => expect(unlinkMirror).toHaveBeenCalledWith(SLOT, { channel_type: 'discord', binding: '' }))
    })

    it('an origin-only channel has no Unlink: the conversation IS the session', async () => {
      mount({ links: [link({ direction: 'origin' })] })
      expect(await screen.findByText(L('disconnect_from', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.queryByText(L('unlink_from', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      // The Disconnect sub-line rides on EVERY connected Disconnect, an Unlink
      // beneath it or not: a reader who learned the line on a mirrored session's
      // menu and then meets a bare Disconnect here cannot tell whether this one
      // is the gentle pause. It is — the conversation stays bound.
      expect(screen.getByText(L('disconnect_outcome'))).toBeInTheDocument()
      // And WHY there is no Unlink is said on the row: a reader who met a
      // mirrored channel's two controls could not tell why this one only gets
      // the pause.
      expect(screen.getByText(L('born_in_note', { label: 'zzq-guild' }))).toBeInTheDocument()
    })

    it('a mirrored channel carries no born-in note', async () => {
      mount({ links: [link({ direction: 'both' })] })
      expect(await screen.findByText(L('unlink_from', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.queryByText(L('born_in_note', { label: 'zzq-guild' }))).not.toBeInTheDocument()
    })

    it('a channel the session was born in has no Unlink, even beside its own self-mirror row', async () => {
      // A Discord-born session carries two rows for its own conversation: the
      // `origin` row and the self-mirror the dispatcher binds on every inbound
      // turn (`both`, since Discord resumes inbound). Severing that mirror would
      // leave dashboard-taken turns reaching nobody until the next inbound
      // message rebinds it, so the group is judged by its origin row: no Unlink and
      // one Disconnect row as before (the paused line rides on it like any bound row).
      mount({
        links: [link({ direction: 'origin', target: 'o-1' }), link({ direction: 'both', target: 'm-1' })],
      })
      expect(await screen.findByText(L('disconnect_from', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.queryByText(L('unlink_from', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      expect(screen.getByText(L('disconnect_outcome'))).toBeInTheDocument()
      expect(screen.getByText(L('born_in_note', { label: 'zzq-guild' }))).toBeInTheDocument()
      // Bound, so labelled: the conversation the session was born in drives it.
      expect(screen.getByText(L('direction_two_way', { label: 'zzq-guild' }))).toBeInTheDocument()
      fireEvent.click(screen.getByText(L('disconnect_from', { label: 'zzq-guild' })))
      await waitFor(() => expect(pauseMirror).toHaveBeenCalledTimes(2))
      // Paused now, and it says so — the born-in row carries the paused line
      // like any bound row — but still no Unlink.
      expect(screen.getByText(L('still_linked'))).toBeInTheDocument()
      expect(screen.queryByText(L('unlink_from', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      expect(unlinkMirror).not.toHaveBeenCalled()
    })

    it('scoped to one channel, the section renders that channel alone and offers nothing', async () => {
      // The paused header chip's menu: its label names Discord and its hint
      // promises to resume or unlink Discord, so a Telegram row or a Slack offer
      // under it would be a menu that does not match its trigger. The target
      // list is not fetched for a scoped section — it has nothing to offer.
      channelTargets.mockResolvedValue([target({ channel_type: 'slack', target_id: 'zzq-slack', label: 'zzq-slack-offer' })] as never)
      mount({
        links: [
          link({ direction: 'both', paused: true }),
          link({ channel: 'telegram', label: 'zzq-tg', target: 'tg-1', direction: 'out' }),
        ],
      }, 'dropdown', 'discord')
      expect(await screen.findByText(L('resume_replies_to', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.getByText(L('unlink_from', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.queryByText(L('disconnect_from', { label: 'zzq-tg' }))).not.toBeInTheDocument()
      expect(screen.queryByText(L('unlink_from', { label: 'zzq-tg' }))).not.toBeInTheDocument()
      expect(screen.queryByText(L('connect_to', { label: 'zzq-slack-offer' }))).not.toBeInTheDocument()
      expect(channelTargets).not.toHaveBeenCalled()
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
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'zzq-slack' })))
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
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'zzq-guild' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('unlink_failed', { label: 'zzq-guild', reason: 'zzq-unlink-broke' })}`,
      ]))
      expect(screen.getByTestId('linked-surfaces-error-discord')).toHaveTextContent('zzq-unlink-broke')
      expect(slotOf(store).links).toHaveLength(1)
      // An ordinary failure keeps the item live: the row is still the binding
      // and a retry is the right next click. Only the STALE refusal dims it.
      expect(screen.getByText(L('unlink_from', { label: 'zzq-guild' })).closest('button'))
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
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'zzq-guild' })))
      await waitFor(() => expect(notifications(store)).toEqual([
        `error:${L('unlink_stale', { label: 'zzq-guild', reason: 'changed' })}`,
      ]))
      await waitFor(() => expect(chatSlots).toHaveBeenCalled())
      await waitFor(() => expect(slotOf(store).links?.map(l => l.channel)).toEqual(['telegram']))
      expect(screen.queryByText(L('unlink_from', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      expect(screen.getByText(L('unlink_from', { label: 'zzq-tg' }))).toBeInTheDocument()
    })

    it('a stale row withholds its Unlink but keeps the agent hand-off under the notice; dismissing the notice restores the Unlink', async () => {
      // The refetch can redraw the SAME row — the binding was re-linked on the
      // same channel, or this tab's row was simply out of date — and then the
      // menu read "no longer linked… Nothing was unlinked" over an "Unlink from
      // Discord" directly beneath (a dimmed one still read as "odd"). The
      // Unlink goes for as long as the stale notice shows; dismissing the
      // notice (or clicking the row again) brings it back. The agent hand-off
      // does NOT go: inside Radix menu content it is the notice's only
      // keyboard-reachable escalation (`errors-use-error-notice`), it renders in
      // the notice's own branch, and the notice now says something the agent
      // can act on — the connection changed while the menu was open.
      unlinkMirror.mockRejectedValueOnce(
        new ApiError(409, 'changed', JSON.stringify({ code: 'mirror_changed' })),
      )
      chatSlots.mockResolvedValue([{
        key: SLOT, messages: 0, running: false, links: [link({ direction: 'both' })],
      }] as never)
      mount({ links: [link({ direction: 'both' })] })
      const handOff = () => screen.queryByText(i18nT('components.askAgent.ask_the_agent'))
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'zzq-guild' })))
      await waitFor(() => expect(screen.getByTestId('linked-surfaces-error-discord')).toBeInTheDocument())
      await waitFor(() => expect(chatSlots).toHaveBeenCalled())
      // Redrawn with the same row: the notice still shows, so there is no Unlink
      // item. The toggle row's own consequence line ("the connection stays")
      // and direction tag are withheld too — beside a notice that says the row
      // was out of date, they would make the row disagree with itself. The
      // hand-off item stays, described by the notice it escalates.
      expect(screen.queryByText(L('unlink_from', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      const notice = screen.getByTestId('linked-surfaces-error-discord')
      expect(notice).toHaveTextContent(L('unlink_stale', { label: 'zzq-guild' }))
      expect(handOff()).toBeInTheDocument()
      expect(handOff()!.closest('[aria-describedby]')).toHaveAttribute(
        'aria-describedby', notice.getAttribute('id') ?? '',
      )
      // And it says its job, in the sub-line grammar every other item here
      // uses: a bare sparkle item was the one control a reader could not
      // identify in a menu whose every other action self-describes.
      expect(handOff()!.closest('[aria-describedby]')).toHaveTextContent(L('ask_agent_stale_outcome'))
      expect(screen.queryByText(L('disconnect_outcome'))).not.toBeInTheDocument()
      expect(screen.queryByText(L('direction_two_way', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      expect(screen.getByText(L('disconnect_from', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(unlinkMirror).toHaveBeenCalledTimes(1)
      fireEvent.click(within(notice).getByRole('button', { name: i18nT('components.errorNotice.dismiss') }))
      expect(screen.queryByTestId('linked-surfaces-error-discord')).not.toBeInTheDocument()
      expect(handOff()).not.toBeInTheDocument()
      expect(screen.getByText(L('disconnect_outcome'))).toBeInTheDocument()
      expect(screen.getByText(L('direction_two_way', { label: 'zzq-guild' }))).toBeInTheDocument()
      unlinkMirror.mockResolvedValueOnce({ ok: true, was_linked: true } as never)
      fireEvent.click(screen.getByText(L('unlink_from', { label: 'zzq-guild' })))
      await waitFor(() => expect(unlinkMirror).toHaveBeenCalledTimes(2))
    })

    it('an ordinary unlink failure keeps the Unlink item and offers the agent hand-off', async () => {
      // Only the STALE refusal withholds the item: nothing broke there and the
      // refetch repairs. A failure the user cannot repair from the menu keeps
      // the item live for a retry, and the hand-off reachable in both cases.
      unlinkMirror.mockRejectedValueOnce(new Error('zzq-unlink-broke'))
      mount({ links: [link({ direction: 'both' })] })
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'zzq-guild' })))
      await waitFor(() => expect(screen.getByTestId('linked-surfaces-error-discord')).toBeInTheDocument())
      expect(screen.getByText(L('unlink_from', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.getByText(i18nT('components.askAgent.ask_the_agent'))).toBeInTheDocument()
      // The stale refusal's outcome line is the stale refusal's: an ordinary
      // failure keeps the bare item, as the hand-off reads everywhere else.
      expect(screen.queryByText(L('ask_agent_stale_outcome'))).not.toBeInTheDocument()
      expect(screen.getByText(L('direction_two_way', { label: 'zzq-guild' }))).toBeInTheDocument()
    })

    it('a click on Unlink while its mutation is in flight is swallowed', async () => {
      let release: (v: unknown) => void = () => {}
      unlinkMirror.mockReturnValue(new Promise(r => { release = r }) as never)
      mount({ links: [link({ direction: 'both' })] })
      const row = await screen.findByText(L('unlink_from', { label: 'zzq-guild' }))
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
      expect(await screen.findByText(L('resume_replies_to', { label: 'zzq-guild' }))).toBeInTheDocument()
      fireEvent.click(screen.getByText(L('unlink_from', { label: 'zzq-guild' })))
      await waitFor(() => expect(screen.getByTestId('linked-surfaces-error-discord')).toBeInTheDocument())
      await waitFor(() => expect(chatSlots).toHaveBeenCalled())
      expect(screen.getByText(L('connect_to', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.queryByText(L('resume_replies_to', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      expect(screen.queryByText(L('still_linked'))).not.toBeInTheDocument()
      // The direction label asserts a standing link too, so it goes with the line.
      expect(screen.queryByText(L('direction_two_way', { label: 'zzq-guild' }))).not.toBeInTheDocument()
      fireEvent.click(within(screen.getByTestId('linked-surfaces-error-discord'))
        .getByRole('button', { name: i18nT('components.errorNotice.dismiss') }))
      expect(screen.getByText(L('resume_replies_to', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.getByText(L('still_linked'))).toBeInTheDocument()
      expect(screen.getByText(L('direction_two_way', { label: 'zzq-guild' }))).toBeInTheDocument()
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
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'zzq-guild' })))
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
      expect(screen.getByText(L('disconnect_from', { label: 'zzq-guild' }))).toBeInTheDocument()
      expect(screen.getByText(L('unlink_from', { label: 'zzq-guild' }))).toBeInTheDocument()
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
      fireEvent.click(await screen.findByText(L('unlink_from', { label: 'zzq-slack' })))
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
    expect(screen.queryByText(L('disconnect_from', { label: 'zzq-guild' }))).not.toBeInTheDocument()
  })
})
