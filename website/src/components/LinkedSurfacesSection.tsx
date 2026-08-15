import { useMutation, useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { ApiError, api } from '../api/client'
import { i18nT } from '../i18n/t'
import { useAppDispatch, useAppSelector } from '../store'
import { patchSlotLink, updateSlot } from '../store/dashboardSlice'
import { addNotification } from '../store/notificationsSlice'
import type { ConfiguredChannelTarget, SessionLink } from '../types'
import { channelBrandLabel } from '../utils/channelOrigin'
import { parseErrorCode } from '../utils/errorReport'
import { ChannelBrandIcon } from './ChannelBrandIcon'
import { ContextMenuItem } from './ui/context-menu'
import { DropdownMenuItem } from './ui/dropdown-menu'

/**
 * One row per channel, and the row's LABEL is the action.
 *
 * There are exactly two states — connected and not — for every channel alike, so
 * this renders one flat list: `Disconnect from X` when output is flowing there,
 * `Connect to X` otherwise. Nothing here explains the machinery. The role badge,
 * the offline badge, the reminder item and the release/stop-mirroring items are
 * all gone, along with the vocabulary they carried: `origin`, `mirror`, `two-way`
 * and `offline` each described an internal routing fact the user could not act on.
 *
 * Disconnect means output STOPS, never that the binding is severed: the
 * conversation still resolves to this session, so a reply there resumes it and
 * connecting again picks it back up. That is what lets one row carry both
 * directions — a disconnected channel and one that was never connected read
 * identically, and the click that connects either does the right thing without
 * the user knowing which it was.
 *
 * Every channel a session touches gets a row, INCLUDING the conversation it was
 * born in: you can stop a Slack-born session syndicating to its thread and carry
 * on in the dashboard. That was the last place a channel appeared with no control,
 * and removing the carve-out is what let the last badge go.
 */

/** What one rendered row needs, whichever channel it belongs to. */
type ChannelRow = {
  key: string
  channel: string
  label: string
  connected: boolean
  /** A mutation for THIS row is in flight. Transient, not a third state. */
  pending: boolean
  disabledReason: string
  toggle: () => void
}

export default function LinkedSurfacesSection({ slotKey, variant }: {
  slotKey: string
  variant: 'dropdown' | 'context'
}) {
  const Item = variant === 'context' ? ContextMenuItem : DropdownMenuItem
  const dispatch = useAppDispatch()
  const slot = useAppSelector(s => s.dashboard.slots.find(x => x.key === slotKey))
  // No synthesized Slack row. The wire emits a Slack row on exactly the condition
  // it reports `slack_linked`, and the row is what carries `paused` — a row
  // invented here could not know the disconnect, so it rendered a disconnected
  // thread as connected. Trusting the wire is what keeps the two from disagreeing.
  const links: SessionLink[] = slot?.links ?? []

  const { data: targets } = useQuery({
    queryKey: ['channel-targets'],
    queryFn: () => api.channelTargets().then(result => (
      Array.isArray(result) ? result as ConfiguredChannelTarget[] : []
    )),
    refetchInterval: 30_000,
  })

  const notify = (kind: 'success' | 'error', title: string) => {
    dispatch(addNotification({ ts: String(Date.now()), title, body: '', kind }))
  }
  const failure = (e: unknown) => (
    e instanceof Error && e.message
      ? e.message
      : i18nT('components.linkedSurfacesSection.unknown_error')
  )
  const labelFor = (channel: string, fallback: string) => (
    // The brand label, so a row's text does not change between its two states:
    // the wire's link label is "Slack" while the picker's target label is
    // "Slack · Direct Message", and a row whose name moved when you clicked it
    // would stop reading as one row with two states.
    channelBrandLabel(channel) || fallback
  )

  // Every mutation notifies on failure. None has a visible result outside this
  // menu — a disconnect is silent in the conversation, and a connect's catch-up
  // lands where the user is not looking — so a silent failure would leave them
  // believing the state flipped when it did not. Success needs no toast: the verb
  // flipping is the confirmation.
  //
  // Optimistic updates go through `patchSlotLink`, which touches ONE channel's row
  // against whatever is in the store at dispatch time. Rebuilding the array from a
  // captured snapshot is what made two toggles unsafe together.
  const setSlackDelivery = useMutation({
    mutationFn: (paused: boolean) => api.pauseSlack(slotKey, paused),
    onSuccess: (_r, paused) => dispatch(patchSlotLink({
      key: slotKey, channel: 'slack', patch: { paused },
    })),
    onError: (e, paused) => notify('error', i18nT(
      paused
        ? 'components.linkedSurfacesSection.disconnect_failed'
        : 'components.linkedSurfacesSection.connect_failed',
      { label: labelFor('slack', 'Slack'), reason: failure(e) },
    )),
  })
  const setMirrorDelivery = useMutation({
    // `origin` distinguishes the two non-Slack deliveries a session can hold at
    // once — the conversation it was born in and an explicit mirror — which are
    // muted separately. Without it, acting on one row moved both.
    mutationFn: ({ paused, origin }: { channel: string; paused: boolean; origin: boolean }) => (
      api.pauseMirror(slotKey, paused, origin)
    ),
    onSuccess: (_r, { channel, paused, origin }) => dispatch(patchSlotLink({
      key: slotKey, channel, origin, patch: { paused },
    })),
    onError: (e, { channel, paused }) => notify('error', i18nT(
      paused
        ? 'components.linkedSurfacesSection.disconnect_failed'
        : 'components.linkedSurfacesSection.connect_failed',
      { label: labelFor(channel, channel), reason: failure(e) },
    )),
  })
  const connectSlack = useMutation({
    mutationFn: (channel: string | undefined) => api.slackLink(slotKey, channel),
    onSuccess: (r) => {
      if (!r?.ok) return
      // Slot-level Slack fields and the Slack ROW are separate dispatches on
      // purpose: the row patch must not carry a whole-array rewrite, or a
      // concurrent toggle on another channel loses its row to this one's snapshot.
      dispatch(updateSlot({
        key: slotKey,
        slack_linked: true,
        slack_channel: r.channel,
        slack_thread_ts: r.thread_ts,
      }))
      dispatch(patchSlotLink({ key: slotKey, channel: 'slack', patch: { paused: false } }))
    },
    onError: (e) => notify('error', i18nT('components.linkedSurfacesSection.connect_failed', {
      label: labelFor('slack', 'Slack'), reason: failure(e),
    })),
  })
  const connectMirror = useMutation({
    mutationFn: (target: ConfiguredChannelTarget) => (
      api.linkMirror(slotKey, target.channel_type, target.target_id)
    ),
    onError: (e, target) => {
      // 409 conversation_occupied: another session holds this conversation. Only
      // Discord can hit it — a Slack session gets its own thread, so many sessions
      // coexist and it never conflicts. The connect is refused rather than
      // offering to take it over, so the honest report is that the conversation is
      // in use, not a prompt to evict someone.
      //
      // Status AND code, because the status alone is ambiguous: this endpoint also
      // answers 409 with `configured_target_unavailable`, and matching the status
      // by itself would report a merely unavailable channel as occupied. The prose
      // cannot be matched either (`friendlyErrText` drops `code`), so the code is
      // read from the retained raw body.
      const occupied = e instanceof ApiError
        && e.status === 409
        && parseErrorCode(e.body) === 'conversation_occupied'
      notify('error', occupied
        ? i18nT('components.linkedSurfacesSection.held_elsewhere', { label: target.label })
        : i18nT('components.linkedSurfacesSection.connect_failed', {
          label: target.label, reason: failure(e),
        }))
    },
  })

  // Which channel a mutation is in flight FOR, so the spinner lands on the row the
  // user clicked instead of on all of them. `variables` is the argument the
  // in-flight mutation was called with, which is the only per-row handle available
  // — the mutations are shared across rows.
  const pendingChannel = setSlackDelivery.isPending || connectSlack.isPending
    ? 'slack'
    : setMirrorDelivery.isPending
      ? setMirrorDelivery.variables?.channel ?? null
      : connectMirror.isPending
        ? connectMirror.variables?.channel_type ?? null
        : null

  const rows: ChannelRow[] = []

  // ONE row per channel — grouped, because "one row per channel" has to hold even
  // when the wire reports the same channel twice. A session BORN in Discord that
  // is then mirrored to Discord carries two links for it (an `origin` fact and a
  // `mirror` fact), which rendered two Discord controls sharing one piece of
  // state — the exact confusion this menu replaced.
  //
  // The row acts on EVERY delivery in its group rather than picking one. Those
  // deliveries carry separate flags, so collapsing to a single winner left the
  // dropped one with no control at all: it could be muted with nothing on screen
  // able to unmute it. The channel is the unit the user is choosing about, so the
  // channel is what the click changes — all of it.
  const byChannel = new Map<string, SessionLink[]>()
  for (const link of links) {
    const group = byChannel.get(link.channel)
    if (group) group.push(link)
    else byChannel.set(link.channel, [link])
  }
  for (const [channel, group] of byChannel) {
    // Connected while ANY delivery is still live, not only when all are. A mixed
    // group can only come from a partial failure or pre-existing data, and under
    // "all" such a row would read `Connect` while messages were still arriving.
    // Under "any" it reads `Disconnect` and one click stops the remainder, so the
    // control is self-correcting rather than lying.
    const connected = group.some(link => !link.paused)
    // Labelled and keyed from the explicit binding when there is one: it is the
    // real target, whereas an origin row's coordinates are provenance.
    const primary = group.find(link => link.direction !== 'origin') ?? group[0]
    rows.push({
      key: `${channel}:${primary.target}`,
      channel,
      label: labelFor(channel, primary.label),
      connected,
      pending: pendingChannel === channel,
      disabledReason: '',
      toggle: () => {
        // Guarded on THIS channel, not on any mutation: a disconnect in flight for
        // Discord must not swallow a click on the Slack row. Keying the guard on
        // `isPending` froze every sibling while one row was mid-flight, which
        // contradicts rows the design makes independently mutable.
        if (pendingChannel === channel) return
        if (channel === 'slack') {
          setSlackDelivery.mutate(connected)
          return
        }
        for (const link of group) {
          setMirrorDelivery.mutate({
            channel,
            paused: connected,
            origin: link.direction === 'origin',
          })
        }
      },
    })
  }

  // Offers for channels this session does not already hold. A channel already
  // bound has its row above instead of an offer, so connecting a second
  // conversation on the same channel is not offered.
  const bound = new Set(links.map(link => link.channel))
  for (const target of (targets ?? []).filter(t => !bound.has(t.channel_type))) {
    rows.push({
      key: `${target.channel_type}:${target.target_id}`,
      channel: target.channel_type,
      // The DESTINATION's own label here, not the brand label. Several
      // destinations on one channel can be offered at once, and the brand label
      // collapses them into identical "Connect to Slack" rows — so a click would
      // backfill this session's transcript to a conversation the user was never
      // shown. The row-name stability that `labelFor` protects is deliberately
      // traded away for offers: an offer has to say where it sends.
      label: target.label || labelFor(target.channel_type, target.channel_type),
      connected: false,
      pending: pendingChannel === target.channel_type,
      disabledReason: target.available
        ? ''
        : target.unavailable_reason || i18nT('components.linkedSurfacesSection.unavailable'),
      toggle: () => {
        if (pendingChannel === target.channel_type) return
        if (target.channel_type === 'slack') connectSlack.mutate(target.target_id)
        else connectMirror.mutate(target)
      },
    })
  }

  return (
    <>
      {rows.map(row => (
        <Item
          key={row.key}
          aria-disabled={row.disabledReason ? true : undefined}
          aria-busy={row.pending ? true : undefined}
          className={row.disabledReason ? 'opacity-60' : undefined}
          // The row's ONLY tooltip, and only when the channel cannot be connected
          // at all: a broken config is a fact the user cannot otherwise see. The
          // retained-binding behaviour is deliberately never explained.
          title={row.disabledReason || undefined}
          onSelect={(event) => {
            // Never close the menu: the row IS the state display, so the user has
            // to stay to see the verb flip. A menu that closes on click reads as
            // "nothing happened".
            event.preventDefault()
            if (row.disabledReason) {
              notify('error', row.disabledReason)
              return
            }
            row.toggle()
          }}
        >
          {/* A spinner rather than the dimming used for an unavailable row: both
            * looked identical before, so a slow connect — which runs a catch-up
            * delivery — was indistinguishable from a channel that cannot be
            * connected at all. */}
          {row.pending
            ? <Loader2 size={13} className="shrink-0 animate-spin" />
            : <ChannelBrandIcon channel={row.channel} size={13} />}
          <span className="flex min-w-0 flex-col">
            <span className="truncate">
              {row.connected
                ? i18nT('components.linkedSurfacesSection.disconnect_from', { label: row.label })
                : i18nT('components.linkedSurfacesSection.connect_to', { label: row.label })}
            </span>
            {/* VISIBLE, not hover-only. A dimmed row whose reason lives only in a
              * `title` and a click-triggered toast is unreadable to a keyboard or
              * touch user — they see a row that refuses to work and no way to find
              * out why, and the reason is exactly what gates the task. The tooltip
              * and the toast stay as the pointer and confirmation affordances;
              * this is the discoverable one. Same styling the pre-consolidation
              * row used, so a broken channel reads the way it always did. */}
            {row.disabledReason ? (
              <span className="truncate text-[11px] text-muted">{row.disabledReason}</span>
            ) : null}
          </span>
        </Item>
      ))}
    </>
  )
}
