import { Fragment, useId, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Loader2, Unlink } from 'lucide-react'
import { ApiError, api } from '../api/client'
import { i18nT } from '../i18n/t'
import { useAppDispatch, useAppSelector } from '../store'
import { dropSlotLinks, fetchSlots, patchSlotLink, updateSlot } from '../store/dashboardSlice'
import { addNotification } from '../store/notificationsSlice'
import type { ConfiguredChannelTarget, SessionLink } from '../types'
import { channelBrandLabel } from '../utils/channelOrigin'
import { parseErrorCode } from '../utils/errorReport'
import { ChannelBrandIcon } from './ChannelBrandIcon'
import ErrorNotice, { ErrorNoticeMenuItem } from './ErrorNotice'
import { ContextMenuItem } from './ui/context-menu'
import { DropdownMenuItem } from './ui/dropdown-menu'

/**
 * One row per channel, and the row's LABEL is the action.
 *
 * The row has two states for every channel alike: `Disconnect from X` when
 * output is flowing there, `Connect to X` otherwise — except that a paused row
 * which still holds its binding reads `Resume replies to X`, because its
 * sub-line says the link stands and a `Connect` verb above that line read as a
 * second link rather than a resume. Nothing here explains the
 * machinery. The role badge, the offline badge, the reminder item and the
 * release/stop-mirroring items are all gone, along with the vocabulary they
 * carried: `origin`, `mirror`, `two-way` and `offline` each described an
 * internal routing fact the user could not act on.
 *
 * Disconnect means output STOPS, never that the binding is severed: the
 * conversation still resolves to this session, so a reply there resumes it and
 * connecting again picks it back up. That is what lets one row carry both
 * directions, and the click that connects either state does the right thing.
 *
 * Because Disconnect keeps the binding, a channel this session is explicitly
 * bound to gets ONE more item: `Unlink from X`, which severs it. The two items
 * define each other under their labels — Disconnect "Pauses replies — the link
 * stays", Unlink "Removes the link — X stops driving this session. Reconnect
 * anytime from the session menu." — so neither is mistaken for the other: a
 * line under Unlink alone
 * leaves nothing to compare it against. A sever action is back in this menu on
 * purpose: without one, a session paused from here stays refused by session
 * control and still receives that conversation's messages, with no exit
 * anywhere on the dashboard. A paused row says so under its label ("Replies
 * paused — messages there still reach this session."), so the middle state is
 * never mistaken for "never connected". A
 * channel the session was BORN in has no Unlink: that conversation IS the
 * session, and the explicit row it carries there is the dispatcher's own
 * self-mirror, not a binding anyone chose to sever.
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
  /**
   * Disconnected, binding retained — the state the toggle alone cannot show,
   * because a paused channel and a never-connected one both read `Connect`.
   */
  stillLinked: boolean
  /**
   * Messages sent there land in this session — the wire's `drives_session` on
   * the explicit binding's row: a two-way (`both`) mirror, or a Slack thread,
   * which the wire marks `out` although a reply in it resumes this session. A
   * one-way `out` mirror only receives replies. The two sub-lines
   * (`still_linked`, `unlink_outcome`) name what stops and what stays, and that
   * differs between the two, so each has an `_out` sibling. Each pair shares
   * its opening clause and differs only in the consequence ("messages there
   * still reach this session" / "messages there don't reach this session"):
   * the consequence is what the reader can act on, where a kind-name ("two-way"
   * / "one-way") read as jargon or as a mistake.
   */
  driven: boolean
  /** Sever the binding. Absent for an origin-only channel and for offers. */
  unlink?: () => void
}

export default function LinkedSurfacesSection({ slotKey, variant }: {
  slotKey: string
  variant: 'dropdown' | 'context'
}) {
  const Item = variant === 'context' ? ContextMenuItem : DropdownMenuItem
  const targetsErrorId = useId()
  const rowErrorIdPrefix = useId()
  const dispatch = useAppDispatch()
  const slot = useAppSelector(s => s.dashboard.slots.find(x => x.key === slotKey))
  // No synthesized Slack row. The wire emits a Slack row on exactly the condition
  // it reports `slack_linked`, and the row is what carries `paused` — a row
  // invented here could not know the disconnect, so it rendered a disconnected
  // thread as connected. Trusting the wire is what keeps the two from disagreeing.
  const links: SessionLink[] = slot?.links ?? []

  const { data: targets, error: targetsError } = useQuery({
    queryKey: ['channel-targets'],
    queryFn: () => api.channelTargets().then(result => (
      Array.isArray(result) ? result as ConfiguredChannelTarget[] : []
    )),
    refetchInterval: 30_000,
  })
  const targetsErrorMessage = targetsError
    ? i18nT('components.linkedSurfacesSection.targets_load_failed')
    : null
  const rowErrorId = (channel: string) => (
    `${rowErrorIdPrefix}-${encodeURIComponent(channel)}`
  )

  const notify = (kind: 'success' | 'error', title: string) => {
    dispatch(addNotification({ ts: String(Date.now()), title, body: '', kind }))
  }
  // The bell-feed notification is kept as the durable record, but it was the
  // ONLY report of a write that did not persist — a toast-only failure is the
  // pattern `errors-use-error-notice` names as a violation. The failure is also
  // rendered in place, under the row it belongs to, and cleared when that row
  // is clicked again.
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})
  // Which rows' error is the STALE refusal (409 `mirror_changed`) — those dim
  // their Unlink item for as long as the notice shows. It lives and dies with
  // the notice: set beside it, cleared by the same `clearRow`, so the menu can
  // never say "nothing was unlinked; the menu was out of date" over a live
  // Unlink for the very row it just called out of date.
  const [staleRows, setStaleRows] = useState<Record<string, true>>({})
  const failRow = (channel: string, title: string, { stale = false } = {}) => {
    notify('error', title)
    setRowErrors(prev => ({ ...prev, [channel]: title }))
    if (stale) setStaleRows(prev => ({ ...prev, [channel]: true }))
  }
  const clearRow = (channel: string) => {
    setRowErrors(prev => {
      if (!(channel in prev)) return prev
      const next = { ...prev }
      delete next[channel]
      return next
    })
    setStaleRows(prev => {
      if (!(channel in prev)) return prev
      const next = { ...prev }
      delete next[channel]
      return next
    })
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
    onError: (e, paused) => failRow('slack', i18nT(
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
    onError: (e, { channel, paused }) => failRow(channel, i18nT(
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
    onError: (e) => failRow('slack', i18nT('components.linkedSurfacesSection.connect_failed', {
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
      failRow(target.channel_type, occupied
        ? i18nT('components.linkedSurfacesSection.held_elsewhere', { label: target.label })
        : i18nT('components.linkedSurfacesSection.connect_failed', {
          label: target.label, reason: failure(e),
        }))
    },
  })
  // Sever the explicit binding — the one action Disconnect does not perform. The
  // endpoints clear the session's outbound mirror (or its Slack thread) and leave a
  // channel-born slot's own conversation alone, so an origin row never offers this.
  // The request names the binding this row shows (channel + the row's opaque
  // `binding` token, a server-minted digest of the whole binding): the row can be
  // stale — a tab that missed a slots push while its socket reconnected still
  // draws the Discord row after another tab rebound the slot to Telegram, or
  // still draws the old Slack thread after a re-link landed a new one in the same
  // channel — and a key-only clear would delete the binding the clicker never
  // saw. The server compares and answers 409 `mirror_changed` without clearing;
  // that is reported as a stale menu, and the slots are refetched so the row
  // catches up. On success the rows that carried THAT binding leave the store at
  // once — keyed on the token the request named, not on the channel: between the
  // click and the response another tab can unlink this binding and link a fresh
  // one on the same channel, and the slots push for the fresh one can land here
  // first. The server deleted exactly the named binding, so exactly its rows go;
  // a same-channel row with a newer token stays, and the tab never reads as
  // disconnected from a binding the server still holds. The server pushes a
  // fresh slots frame too, but a menu that kept showing `Unlink` for a binding
  // already gone would be the same lie this action exists to end.
  const unlinkChannel = useMutation({
    // One endpoint for every row. Which store the binding lives in — the
    // session's mirror or its Slack thread — is the server's fact, not this
    // menu's: `mirror-link` refuses Slack on channel type, so a `slack` row can
    // only be the thread, and `mirror-unlink` hands such a body to the Slack
    // teardown itself. Routing on the channel name here would restate that
    // fact as a client-side assumption — the inference that, made for
    // `driven`, reads a paused Slack row as a one-way link.
    mutationFn: ({ channel, binding }: { channel: string; binding: string }) => (
      api.unlinkMirror(slotKey, { channel_type: channel, binding })
    ),
    // One write: the reducer drops the rows carrying `binding` and, for the
    // Slack thread, clears the slot's `slack_*` fields in the same pass — only
    // when a row actually matched, so the compare and the field clear cannot
    // disagree about which binding is current.
    onSuccess: (_r, { channel, binding }) => {
      dispatch(dropSlotLinks({ key: slotKey, channel, binding }))
    },
    onError: (e, { channel }) => {
      const stale = e instanceof ApiError
        && e.status === 409
        && parseErrorCode(e.body) === 'mirror_changed'
      if (stale) void dispatch(fetchSlots())
      failRow(channel, i18nT(
        stale
          ? 'components.linkedSurfacesSection.unlink_stale'
          : 'components.linkedSurfacesSection.unlink_failed',
        { label: labelFor(channel, channel), reason: failure(e) },
      ), { stale })
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
  // The Unlink item spins on its own: it is a separate control on the same
  // channel, and sharing the row's spinner would freeze the toggle for a click
  // it did not receive.
  const unlinkingChannel = unlinkChannel.isPending
    ? unlinkChannel.variables?.channel ?? null
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
    const explicit = group.find(link => link.direction !== 'origin')
    const primary = explicit ?? group[0]
    // Severable only when the session was NOT born on this channel. A channel-born
    // session carries TWO rows for its own conversation — the `origin` row and the
    // self-mirror the dispatcher binds on every inbound turn — and their targets
    // do not even agree on a DM (the origin row names the peer, the mirror the DM
    // channel), so the group is judged by its `origin` row alone: with one
    // present, the explicit row is that conversation's own delivery, and popping
    // it would leave dashboard-taken turns and the auto-compact notice reaching
    // nobody until the next inbound message silently rebound it.
    const severable = explicit !== undefined && !group.some(link => link.direction === 'origin')
    rows.push({
      key: `${channel}:${primary.target}`,
      channel,
      label: labelFor(channel, primary.label),
      connected,
      pending: pendingChannel === channel,
      disabledReason: '',
      // Only a severable binding needs the middle state said out loud: a born-in
      // conversation that is paused is simply muted, and its row's `Connect`
      // verb already tells the whole truth about it.
      stillLinked: severable && !connected,
      // Whether messages sent there land HERE, as the wire states it on the
      // explicit row. The projection owns the routing fact — a `both` mirror
      // drives, a one-way mirror does not, and a Slack thread drives although
      // its direction reads `out`, because Slack routes replies through its own
      // thread index rather than the mirror's inbound marker. Read here, not
      // inferred from `direction` or the channel name: that inference reads a
      // paused Slack row as a one-way link and understates what its Unlink
      // destroys. A row from a cached pre-field payload reads as not driving
      // until the next slots push.
      driven: explicit?.drives_session === true,
      unlink: severable
        ? () => {
          if (unlinkingChannel === channel) return
          clearRow(channel)
          // A row from a cached pre-`binding` payload sends '' and is refused as
          // stale; the refetch that follows redraws it with its token.
          unlinkChannel.mutate({ channel, binding: explicit.binding ?? '' })
        }
        : undefined,
      toggle: () => {
        // Guarded on THIS channel, not on any mutation: a disconnect in flight for
        // Discord must not swallow a click on the Slack row. Keying the guard on
        // `isPending` froze every sibling while one row was mid-flight, which
        // contradicts rows the design makes independently mutable.
        if (pendingChannel === channel) return
        clearRow(channel)
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
      stillLinked: false,
      driven: false,
      pending: pendingChannel === target.channel_type,
      disabledReason: target.available
        ? ''
        : target.unavailable_reason || i18nT('components.linkedSurfacesSection.unavailable'),
      toggle: () => {
        if (pendingChannel === target.channel_type) return
        clearRow(target.channel_type)
        if (target.channel_type === 'slack') connectSlack.mutate(target.target_id)
        else connectMirror.mutate(target)
      },
    })
  }

  return (
    <>
      {/* A failed target load must not look like an empty target list: the
          alert says the read failed. It stays passive inside the menu; its
          sibling item is the keyboard-reachable hand-off. */}
      {targetsErrorMessage && (
        <>
          <div className="px-2 py-1.5 max-w-[280px]">
            <ErrorNotice
              id={targetsErrorId}
              variant="inline"
              className="whitespace-normal"
              message={targetsErrorMessage}
              testId="linked-surfaces-targets-error"
            />
          </div>
          <ErrorNoticeMenuItem
            Item={Item}
            message={targetsErrorMessage}
            describedBy={targetsErrorId}
          />
        </>
      )}
      {rows.map(row => (
        <Fragment key={row.key}>
        <Item
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
                : row.stillLinked && !staleRows[row.channel]
                  // A paused row that still holds its binding names the state it
                  // ends: the same click as `Connect`, but a reader who has just
                  // read "the link stays" under a verb that says "Connect" could
                  // not tell whether they were about to resume or to link anew.
                  // A never-connected channel and a paused born-in one keep the
                  // plain verb: neither carries a sub-line to contradict. So does
                  // a row under its STALE notice — "Resume replies" asserts the
                  // very link the notice says is no longer there, exactly as the
                  // withheld consequence line would.
                  ? i18nT('components.linkedSurfacesSection.resume_replies_to', { label: row.label })
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
            ) : staleRows[row.channel] ? (
              // Nothing, while the row's STALE notice shows beneath it. Both
              // consequence lines assert the link is standing ("the link
              // stays"), and the notice directly under them says the menu was
              // out of date and nothing was unlinked — a row that says both
              // disagrees with itself. The verb stays; the notice is the
              // sub-line until it is dismissed or the row is clicked again.
              null
            ) : row.stillLinked ? (
              // The middle state, said out loud as a consequence rather than a
              // label: "still linked" collided with the neighbouring "Copy link"
              // item (same word, unrelated ideas), and "Disconnected" under a row
              // that reads `Connect` contradicted itself. So it leads with what
              // is paused. The verb above it reads `Resume replies` rather than
              // `Connect` for the same reason: under this line, `Connect` read as
              // a second link. The binding still counts as a mirror for session
              // control — and for a two-way binding still routes that
              // conversation's messages here. Same styling as the reason line so
              // the row keeps one visual grammar.
              <span className="truncate text-[11px] text-muted">
                {i18nT(row.driven
                  ? 'components.linkedSurfacesSection.still_linked'
                  : 'components.linkedSurfacesSection.still_linked_out')}
              </span>
            ) : row.connected ? (
              // The other half of the pair, under EVERY connected Disconnect —
              // not only where an Unlink sits beneath it. Disconnect and Unlink
              // stacked under each other read as near-synonyms, and a sub-line
              // under Unlink alone cannot separate them: with nothing under
              // Disconnect there is no second term to compare against, and a
              // reader who cannot tell the temporary one from the permanent one
              // clicks neither. A reader who has learned the line on one menu
              // then meets a bare Disconnect on a born-in channel and cannot tell
              // whether THAT one is the gentle pause — so the line rides on every
              // Disconnect, and it is true of every one: the conversation stays
              // bound and a reply there resumes it.
              <span className="truncate text-[11px] text-muted">
                {i18nT('components.linkedSurfacesSection.disconnect_outcome')}
              </span>
            ) : null}
          </span>
        </Item>
        {/* In place, under the row that failed. The alert stays passive inside
            Radix; the sibling item carries the keyboard-reachable hand-off. It sits
            directly under the toggle row, before the Unlink item, so the row's
            ArrowDown still reaches the hand-off first — the channel's two actions
            share this one error slot, since a failure of either is a failure of
            that channel's row. */}
        {rowErrors[row.channel] && (
          <>
            <div className="px-2 pb-1.5 max-w-[280px]">
              <ErrorNotice
                id={rowErrorId(row.channel)}
                variant="inline"
                className="whitespace-normal text-[11px]"
                message={rowErrors[row.channel]}
                onDismiss={() => clearRow(row.channel)}
                testId={`linked-surfaces-error-${row.channel}`}
              />
            </div>
            <ErrorNoticeMenuItem
              Item={Item}
              message={rowErrors[row.channel]}
              describedBy={rowErrorId(row.channel)}
            />
          </>
        )}
        {/* Sever, as distinct from mute. Its own item rather than a modifier on
          * the toggle: the two verbs mean different things and the user has to be
          * able to pick either without reading a tooltip. The verb alone does not
          * carry the difference, though — stacked under Disconnect, "Unlink" and
          * "Disconnect" read as near-synonyms and a reader who cannot tell the
          * temporary one from the permanent one clicks neither. So both items
          * name their outcome under the label, in one sub-line grammar: what
          * happens to the link, then what stops. The Unlink line also says that
          * reconnecting brings the link back: a bare removal verb reads as hard
          * to undo, and a reader who takes it that way does not dare click the
          * one control that ends the lock-out. The menu keeps that promise —
          * right after an unlink it offers the same destination as a fresh
          * `Connect` row — and the line says where: the session menu. Named
          * that way, not "this menu": the same rows also open under the paused
          * header chip, and an unlink from there removes the chip and its menu,
          * so "this menu" would point at a menu that is gone. The way back is its
          * own sentence ("Reconnect anytime from the session menu."): as a third
          * dash-clause the place attached to the nearest verb and read as
          * "removes the link from the session menu". And the line no longer names
          * the kind of link — "two-way link" made a reader pause on exactly this
          * control; the tail already carries the direction.
          *
          * Dimmed, not offered, while the row's STALE notice shows. The notice
          * says the menu was out of date and nothing was unlinked; a live Unlink
          * directly beneath it contradicted that, and a reader who could not
          * reconcile the two clicked nothing. The refetch the refusal triggers
          * redraws the row — often with the very same shape, when the binding
          * was re-linked on the same channel — so the dim rides on the notice
          * rather than on the refetch: it lifts when the notice is dismissed or
          * the row is clicked again, exactly when the notice goes. Same visual
          * grammar as an unavailable row (aria-disabled + dimming). */}
        {row.unlink && (
          <Item
            aria-disabled={staleRows[row.channel] ? true : undefined}
            aria-busy={unlinkingChannel === row.channel ? true : undefined}
            className={staleRows[row.channel] ? 'opacity-60' : undefined}
            onSelect={(event) => {
              // Same reason the toggle stays open: the row IS the state display,
              // and the binding disappearing from the menu is the confirmation.
              event.preventDefault()
              if (staleRows[row.channel]) return
              row.unlink?.()
            }}
          >
            {unlinkingChannel === row.channel
              ? <Loader2 size={13} className="shrink-0 animate-spin" />
              : <Unlink size={13} className="shrink-0" aria-hidden />}
            <span className="flex min-w-0 flex-col">
              <span className="truncate">
                {i18nT('components.linkedSurfacesSection.unlink_from', { label: row.label })}
              </span>
              <span className="truncate text-[11px] text-muted">
                {i18nT(row.driven
                  ? 'components.linkedSurfacesSection.unlink_outcome'
                  : 'components.linkedSurfacesSection.unlink_outcome_out', { label: row.label })}
              </span>
            </span>
          </Item>
        )}
        </Fragment>
      ))}
    </>
  )
}
