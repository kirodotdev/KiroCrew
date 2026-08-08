import { useMutation, useQuery } from '@tanstack/react-query'
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
 * this renders one flat list with no per-channel branches: `Disconnect from X`
 * when bound and delivering, `Connect to X` otherwise. Nothing here explains the
 * machinery. The role badge (Origin / Mirror / Two-way), the offline badge, the
 * reminder item, the release/stop-mirroring items and their confirms are all
 * gone, along with the vocabulary they carried.
 *
 * Disconnect means MUTE, never release: the binding survives, so the
 * conversation still resolves to this session and connecting again picks it back
 * up and catches it up. That is what lets one row carry both directions — a muted
 * channel and one that was never connected read identically, and the click that
 * connects either one does the right thing without the user knowing which it was.
 *
 * An `origin` link (the conversation a session was BORN in) gets no row: there is
 * no binding to connect or disconnect, and the sidebar already marks it from the
 * slot key.
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
  const wireLinks = slot?.links ?? []
  // No synthesized Slack row. The wire now emits a Slack row on exactly the
  // condition it reports `slack_linked`, and the row is what carries `paused` — a
  // row invented here could not know the mute, so it rendered a MUTED thread as
  // connected. Trusting the wire is what keeps the two from disagreeing;
  // `TestASlackRowAlwaysAccompaniesSlackLinked` pins the backend side.
  const links: SessionLink[] = wireLinks
  const slackLink = links.find(link => link.channel === 'slack')
  // Every non-Slack binding this session holds. `origin` is not a binding — it is
  // where the session came from — so it is never a row.
  const mirrorLinks = links.filter(link => link.channel !== 'slack' && link.direction !== 'origin')

  const { data: targets } = useQuery({
    queryKey: ['channel-targets'],
    queryFn: () => api.channelTargets().then(result => (
      Array.isArray(result) ? result as ConfiguredChannelTarget[] : []
    )),
    refetchInterval: 30_000,
  })

  // The Slack row's label must NOT vary with connection state, or it stops
  // reading as one row with two states: the wire's link label is "Slack" while the
  // picker's target label is "Slack · Direct Message". Take the brand label, which
  // is the one place the repo keeps channel brand names (and documents why they are
  // not catalog entries), so both states read the same.
  const slackLabel = channelBrandLabel('slack')
    || slackLink?.label
    || (targets ?? []).find(target => target.channel_type === 'slack')?.label
    || ''

  const notify = (kind: 'success' | 'error', title: string) => {
    dispatch(addNotification({ ts: String(Date.now()), title, body: '', kind }))
  }
  const failure = (e: unknown) => (
    e instanceof Error && e.message
      ? e.message
      : i18nT('components.linkedSurfacesSection.unknown_error')
  )

  /**
   * Optimistic row updates go through `patchSlotLink`, which touches ONE channel's
   * row against whatever is in the store at dispatch time.
   *
   * They used to be built here from the `links` this render closed over, and
   * rebuilding the whole array from a captured snapshot is what made two toggles
   * unsafe together: Slack and Discord in flight at once both derived from the same
   * pre-mutation list, so whichever callback landed second dropped the other's row
   * until the next slots push corrected it. Each row is independently mutable by
   * design, so the store operation is per-row and losing a sibling is impossible
   * rather than merely unlikely.
   *
   * Disconnect RETAINS the binding and only sets `paused` — dropping the row would
   * tell the user the opposite of what happened and strip the control they need to
   * connect again. A connect states `paused: false` as a fact rather than guessing,
   * because it just performed the connect; the component never invents a row from
   * `slack_linked` (an invented row cannot know `paused`, so a muted thread rendered
   * as connected).
   */

  // Every mutation notifies on failure. None of them has a visible result outside
  // this menu — a disconnect is silent in the conversation, and a connect's
  // catch-up lands where the user is not looking — so a silent failure would
  // leave them believing the state flipped when it did not. Success needs no
  // toast: the verb flipping is the confirmation.
  const connectSlack = useMutation({
    mutationFn: (channel: string | undefined) => api.slackLink(slotKey, channel),
    onSuccess: (r) => {
      if (!r?.ok) return
      // Slot-level Slack fields and the Slack ROW are separate dispatches on
      // purpose: the row patch must not carry a whole-array rewrite, or a
      // concurrent Discord toggle loses its row to this one's stale snapshot.
      dispatch(updateSlot({
        key: slotKey,
        slack_linked: true,
        slack_channel: r.channel,
        slack_thread_ts: r.thread_ts,
      }))
      dispatch(patchSlotLink({
        key: slotKey,
        channel: 'slack',
        link: {
          channel: 'slack',
          label: slackLabel,
          target: r.channel ?? '',
          direction: 'out' as const,
          live: true,
          paused: false,
        },
      }))
    },
    onError: (e) => notify('error', i18nT('components.linkedSurfacesSection.connect_failed', {
      label: slackLabel, reason: failure(e),
    })),
  })
  const disconnectSlack = useMutation({
    mutationFn: () => api.pauseSlack(slotKey),
    onSuccess: () => dispatch(patchSlotLink({
      key: slotKey, channel: 'slack', link: null, patch: { paused: true },
    })),
    onError: (e) => notify('error', i18nT('components.linkedSurfacesSection.disconnect_failed', {
      label: slackLabel, reason: failure(e),
    })),
  })
  const connectMirror = useMutation({
    mutationFn: ({ target, channel, confirm }: {
      target: ConfiguredChannelTarget | null
      channel: string
      confirm?: boolean
    }) => (
      target
        ? api.linkMirror(slotKey, target.channel_type, target.target_id, confirm)
        : api.reconnectMirror(slotKey, channel)
    ),
    onSuccess: (result, { target, channel }) => {
      if (!result?.ok) return
      // A fresh link needs its row minted; a reconnect only needs the mute lifted.
      // Either way it touches ONE row, so a concurrent toggle on another channel
      // cannot be overwritten by this callback's view of the list.
      if (target) {
        dispatch(patchSlotLink({
          key: slotKey,
          channel: target.channel_type,
          link: {
            channel: target.channel_type,
            label: target.label,
            target: result.conversation_id || target.target_id,
            // The direction the SERVER says this binding has. Only a transport
            // whose inbound path resolves the mirror binding routes replies back
            // (Discord today), so hard-coding `both` here told the user a Telegram
            // link was two-way when a reply there starts a separate session. Which
            // channels resume is a transport capability, so the server reports the
            // resulting direction instead of the client keeping its own copy of
            // the list. `paused` is stated too — an omitted flag is what let a
            // muted row read as connected elsewhere.
            direction: result.direction ?? 'out',
            live: true,
            paused: false,
          },
        }))
      } else {
        // Reconnect of an existing binding: lift the mute on that row alone.
        dispatch(patchSlotLink({
          key: slotKey, channel, link: null, patch: { paused: false },
        }))
      }
    },
    onError: (e, { target, channel, confirm }) => {
      // 409 conversation_occupied: another session holds this conversation. A
      // conversation cannot host two (there are no threads to scope them to), so
      // taking it means disconnecting the other — the user's call, asked once.
      //
      // Status AND code, because the status alone is ambiguous: this endpoint also
      // answers 409 with `configured_target_unavailable`, and matching the status
      // by itself offered to disconnect a session from a channel that was merely
      // unavailable — a takeover prompt for a takeover that was never possible.
      // The prose cannot be matched either (`friendlyErrText` unwraps the body's
      // `error` field and drops `code`), so the code is read from the retained raw
      // body via the same `parseErrorCode` the error journal uses.
      const occupied = e instanceof ApiError
        && e.status === 409
        && parseErrorCode(e.body) === 'conversation_occupied'
      if (occupied && target && !confirm) {
        if (window.confirm(i18nT('components.linkedSurfacesSection.confirm_takeover', {
          label: target.label,
        }))) {
          connectMirror.mutate({ target, channel, confirm: true })
        }
        return
      }
      notify('error', i18nT('components.linkedSurfacesSection.connect_failed', {
        label: target?.label ?? channel, reason: failure(e),
      }))
    },
  })
  const disconnectMirror = useMutation({
    mutationFn: (channel: string) => api.pauseMirror(slotKey, channel),
    onSuccess: (_result, channel) => dispatch(patchSlotLink({
      key: slotKey, channel, link: null, patch: { paused: true },
    })),
    onError: (e, channel) => notify(
      'error',
      i18nT('components.linkedSurfacesSection.disconnect_failed', {
        label: links.find(link => link.channel === channel)?.label ?? channel,
        reason: failure(e),
      }),
    ),
  })

  // Which channel a mutation is in flight FOR, so the spinner lands on the row the
  // user clicked instead of on all of them. `variables` is the argument the
  // in-flight mutation was called with, which is the only per-row handle available
  // — the four mutations are shared across rows.
  const pendingChannel = connectSlack.isPending || disconnectSlack.isPending
    ? 'slack'
    : connectMirror.isPending
      ? connectMirror.variables?.channel ?? null
      : disconnectMirror.isPending
        ? disconnectMirror.variables ?? null
        : null

  const rows: ChannelRow[] = []

  // Slack, when it is linked or offered. A muted link reconnects with NO channel
  // argument so the endpoint reuses its existing thread; only a session that has
  // never linked passes the picker's target and mints one.
  const slackTarget = (targets ?? []).find(target => target.channel_type === 'slack')
  if (slackLink || slackTarget) {
    const connected = slackLink != null && !slackLink.paused
    rows.push({
      key: 'slack',
      channel: 'slack',
      label: slackLabel,
      connected,
      pending: pendingChannel === 'slack',
      disabledReason: !slackLink && slackTarget && !slackTarget.available
        ? slackTarget.unavailable_reason || i18nT('components.linkedSurfacesSection.unavailable')
        : '',
      toggle: () => {
        // Guarded on THIS channel, not on any mutation: a disconnect in flight for
        // Discord must not swallow a click on the Slack row.
        if (pendingChannel === 'slack') return
        if (connected) {
          disconnectSlack.mutate()
        } else {
          connectSlack.mutate(slackLink ? undefined : slackTarget?.target_id)
        }
      },
    })
  }

  // Every non-Slack binding, live or muted — one row each.
  for (const link of mirrorLinks) {
    const connected = !link.paused
    rows.push({
      key: `${link.channel}:${link.target}`,
      channel: link.channel,
      label: link.label,
      connected,
      pending: pendingChannel === link.channel,
      disabledReason: '',
      toggle: () => {
        // Per CHANNEL. The mutations are shared across rows, so keying the guard on
        // `isPending` froze every sibling while one row was mid-flight — which
        // contradicts rows the design makes independently mutable.
        if (pendingChannel === link.channel) return
        if (connected) {
          disconnectMirror.mutate(link.channel)
        } else {
          connectMirror.mutate({ target: null, channel: link.channel })
        }
      },
    })
  }

  // Offers for channels this session does not already hold. Slack has its own row
  // above; a channel already bound has its row instead of an offer, so connecting
  // a second conversation on the same channel is not offered — one binding per
  // channel type.
  const boundChannels = new Set(mirrorLinks.map(link => link.channel))
  const offers = (targets ?? []).filter(
    target => target.channel_type !== 'slack' && !boundChannels.has(target.channel_type),
  )
  for (const target of offers) {
    rows.push({
      key: `${target.channel_type}:${target.target_id}`,
      channel: target.channel_type,
      label: target.label,
      connected: false,
      pending: pendingChannel === target.channel_type,
      disabledReason: target.available
        ? ''
        : target.unavailable_reason || i18nT('components.linkedSurfacesSection.unavailable'),
      toggle: () => {
        if (pendingChannel === target.channel_type) return
        connectMirror.mutate({ target, channel: target.channel_type })
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
          className={row.disabledReason || row.pending ? 'opacity-60' : undefined}
          // The row's ONLY tooltip, and only when the channel cannot be linked at
          // all: a broken config is a fact the user cannot otherwise see. The
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
          <ChannelBrandIcon channel={row.channel} size={13} />
          <span className="truncate">
            {row.connected
              ? i18nT('components.linkedSurfacesSection.disconnect_from', { label: row.label })
              : i18nT('components.linkedSurfacesSection.connect_to', { label: row.label })}
          </span>
        </Item>
      ))}
    </>
  )
}
