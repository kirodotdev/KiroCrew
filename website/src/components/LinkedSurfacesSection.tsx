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
 * The row has two states for every channel alike: `Pause replies to X` when
 * output is flowing there (the Disconnect action — labelled for what it does,
 * because "disconnect" everywhere else in life means sever, and a reader read
 * the row right only through its sub-line while "the wording pulled in
 * different directions"), `Connect to X` otherwise — except that a paused row
 * which still holds its binding reads `Resume replies to X`, the pause verb's
 * symmetric twin, because its sub-line says the link stands and a `Connect`
 * verb above that line read as a second link rather than a resume. Nothing
 * here explains the
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
 * define each other under their labels — `Pause replies to X` "The connection
 * stays", Unlink "Removes the connection — X stops driving this
 * session. Reconnect anytime from the session menu." — so neither is mistaken
 * for the other: a line under Unlink alone
 * leaves nothing to compare it against. A sever action is back in this menu on
 * purpose: without one, a session paused from here stays refused by session
 * control and still receives that conversation's messages, with no exit
 * anywhere on the dashboard. A paused row says so under its label ("Replies
 * paused — the connection stays."), so the middle state is
 * never mistaken for "never connected". X is the label the SERVER sends on the
 * row's link row — the same string the header chip shows for the binding — and
 * an offer names its destination with the target's own label. A
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
  /**
   * An OFFER's destination discriminator, rendered as the row's muted sub-line
   * rather than inside the verb: the server labels a configured target as
   * "<channel form> · <which one>" ("Slack · #eng", "Discord DM · 118273645"),
   * and the tail on the verb line read as a raw number on a button — "makes me
   * think something is broken or half-finished, and I don't know whose number
   * that is". The verb keeps the channel form ("Connect to Discord DM", the
   * same name the bound row and the chip use) and the tail moves under it in
   * the sub-line grammar every other row already has. A bare id under a
   * `user:` target is named for what it is ("Direct message · 118273645"); a
   * human tail ("#eng", "Direct Message", "yourself") is shown as sent. Empty
   * on every bound row and on an offer whose label carries no tail.
   */
  detail?: string
  connected: boolean
  /** A mutation for THIS row is in flight. Transient, not a third state. */
  pending: boolean
  disabledReason: string
  toggle: () => void
  /**
   * Disconnected, binding retained — the state the toggle alone cannot show,
   * because a paused channel and a never-connected one both read `Connect`.
   * True for every bound row that is paused, the born-in row included.
   */
  stillLinked: boolean
  /**
   * Messages sent there land in this session — the wire's `drives_session` on
   * the channel's link rows: a two-way (`both`) mirror, a Slack thread (which
   * the wire marks `out` although a reply in it resumes this session), or the
   * conversation the session was born in. A one-way `out` mirror only receives
   * replies. The Unlink sub-line names what a sever stops, and that differs
   * between the two (`unlink_outcome` / `_out`); the paused sub-line does not
   * carry the direction at all — the row's `direction` label does, so two rows
   * whose captions differ visibly differ in the thing that explains it.
   */
  driven: boolean
  /**
   * The explicit direction label on every BOUND row, rendered from `driven`:
   * "Two-way · X ↔ this session" or "One-way · this session → X". Absent on an
   * offer, which is bound to nothing yet. Without it two visually identical
   * rows carried captions that flipped between "messages there still reach
   * this session" and "don't reach" with nothing marking why, and a reader who
   * met both stopped trusting either.
   */
  direction?: 'two_way' | 'one_way'
  /**
   * The session was BORN in this conversation. Such a row has no Unlink — the
   * conversation IS the session's home, so its connection can be paused, not
   * removed — and says so in a sub-line, because a reader who meets a
   * mirrored channel's two controls and this channel's one cannot otherwise
   * tell why "this one only gets the pause".
   */
  bornIn: boolean
  /** Sever the binding. Absent for an origin-only channel and for offers. */
  unlink?: () => void
}

/** The separator every transport puts between a target's channel form and its discriminator. */
const OFFER_LABEL_SEPARATOR = ' · '

/**
 * Split a configured target's label into the offer row's verb-line head and its
 * sub-line detail. The server spells a target "<channel form> · <which one>"
 * ("Slack · #eng", "Discord DM · 118273645", "WhatsApp · yourself"); the head is
 * the name the bound row and the chip use for the same destination, and the
 * tail is what tells two offers on one channel apart. A tail that is nothing but
 * the id of a `user:` target — the transport had no name for the person — is
 * named for what it is, so the row never shows an unexplained number. A label
 * with no separator is all head.
 */
export function splitOfferLabel(target: ConfiguredChannelTarget): { head: string; detail: string } {
  const label = target.label ?? ''
  const at = label.indexOf(OFFER_LABEL_SEPARATOR)
  if (at < 0) return { head: label, detail: '' }
  const head = label.slice(0, at).trim()
  const tail = label.slice(at + OFFER_LABEL_SEPARATOR.length).trim()
  const id = target.target_id.startsWith('user:') ? target.target_id.slice('user:'.length) : ''
  const detail = id && tail === id
    ? i18nT('components.linkedSurfacesSection.direct_message_id', { id })
    : tail
  return { head, detail }
}

export default function LinkedSurfacesSection({ slotKey, variant, channel }: {
  slotKey: string
  variant: 'dropdown' | 'context'
  /**
   * Render ONE channel's rows only. The paused header chip's menu passes the
   * channel it names: its label reads "Driven from Discord DM · replies
   * paused" and its hint promises to resume or unlink Discord, so offers for
   * Slack or Telegram under it would be a menu that does not match its
   * trigger. The session menu leaves this unset and renders every channel and
   * the offers.
   */
  channel?: string
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
  const links: SessionLink[] = (slot?.links ?? []).filter(
    link => channel === undefined || link.channel === channel,
  )

  // Offers belong to the whole-session menu alone: a menu scoped to one bound
  // channel has nothing to offer, so the target list is not even fetched.
  const { data: targets, error: targetsError } = useQuery({
    queryKey: ['channel-targets'],
    queryFn: () => api.channelTargets().then(result => (
      Array.isArray(result) ? result as ConfiguredChannelTarget[] : []
    )),
    refetchInterval: 30_000,
    enabled: channel === undefined,
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
  // Which rows' error is the STALE refusal (409 `mirror_changed`) — those
  // withhold their Unlink item, their consequence line and their direction tag
  // for as long as the notice shows (the agent hand-off stays: it belongs to
  // the notice, whatever the notice says). It lives and dies with the notice:
  // set beside it, cleared by the same `clearRow`, so the menu can never say
  // "the connection changed while this menu was open; nothing was unlinked"
  // over an Unlink for the very row it just called out of date.
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
  // A bound channel's name is the label the SERVER sends on its link row — the
  // same string the header chip shows for the same binding ("Driven from
  // Discord DM"). One destination used to carry three names across the journey:
  // the chip said "Discord DM" (the wire), the rows said "Discord" (a brand
  // table kept here), and the offer said "Discord DM · 1234" (the target), and a
  // reader "could not tell why one is more specific than the other, or whether
  // they do different things". The rows now read the wire, so chip and rows
  // agree, and the offer's label is that same form with the destination on it —
  // the one thing an offer must add, since several destinations on one channel
  // can be offered at once. The brand table is only the fallback for a wire row
  // that carries no label (the row's own name, connected or paused, never
  // changes: both states read the same link row).
  const labelFor = (channel: string, fallback: string) => (
    links.find(link => link.channel === channel)?.label
      || channelBrandLabel(channel)
      || fallback
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
    // The whole target, not its id alone: the failure below is reported under
    // the label the OFFER row showed, exactly as `connectMirror` reports its
    // own — one destination, one name, on the row and in its failure line.
    mutationFn: (target: ConfiguredChannelTarget) => api.slackLink(slotKey, target.target_id),
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
    onError: (e, target) => failRow('slack', i18nT('components.linkedSurfacesSection.connect_failed', {
      label: target.label || labelFor('slack', 'Slack'), reason: failure(e),
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
    const bornIn = group.some(link => link.direction === 'origin')
    const severable = explicit !== undefined && !bornIn
    // Whether messages sent there land HERE, as the wire states it on the
    // channel's rows. The projection owns the routing fact — a `both` mirror
    // drives, a one-way mirror does not, a Slack thread drives although its
    // direction reads `out` (Slack routes replies through its own thread index
    // rather than the mirror's inbound marker), and so does the conversation
    // the session was born in. Read here, not inferred from `direction` or the
    // channel name: that inference reads a paused Slack row as a one-way link
    // and understates what its Unlink destroys. Judged over the whole group,
    // which for a severable row is its one explicit binding; a born-in
    // channel's group drives by its origin row. A row from a cached pre-field
    // payload reads as not driving until the next slots push.
    const drives = group.some(link => link.drives_session === true)
    rows.push({
      key: `${channel}:${primary.target}`,
      channel,
      label: labelFor(channel, primary.label),
      connected,
      pending: pendingChannel === channel,
      disabledReason: '',
      // Every BOUND row that is paused says the middle state out loud, the
      // born-in row included: its verb reads `Resume replies` and its tag says
      // the connection stands, but with no line saying "paused" a reader found
      // the menu itself said "paused" nowhere. The line sits above the born-in
      // note, so the row reads state first, then why it has no Unlink.
      stillLinked: (severable || bornIn) && !connected,
      driven: drives,
      direction: drives ? 'two_way' : 'one_way',
      bornIn,
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
  // conversation on the same channel is not offered. A section scoped to one
  // channel (the paused chip's menu) offers nothing: its channel is bound.
  const bound = new Set(links.map(link => link.channel))
  for (const target of (channel === undefined ? targets ?? [] : []).filter(t => !bound.has(t.channel_type))) {
    const { head, detail } = splitOfferLabel(target)
    rows.push({
      key: `${target.channel_type}:${target.target_id}`,
      channel: target.channel_type,
      // The DESTINATION's own label here, not the brand label. Several
      // destinations on one channel can be offered at once, and the brand label
      // collapses them into identical "Connect to Slack" rows — so a click would
      // backfill this session's transcript to a conversation the user was never
      // shown. An offer has to say where it sends — but not on the verb line:
      // the server spells a target "<channel form> · <which one>", the verb
      // keeps the channel form (the name the bound row and the chip use for the
      // same destination) and the discriminator is the row's sub-line, so a
      // raw id never sits inside a button label.
      label: head || labelFor(target.channel_type, target.channel_type),
      detail,
      connected: false,
      stillLinked: false,
      driven: false,
      bornIn: false,
      pending: pendingChannel === target.channel_type,
      disabledReason: target.available
        ? ''
        : target.unavailable_reason || i18nT('components.linkedSurfacesSection.unavailable'),
      toggle: () => {
        if (pendingChannel === target.channel_type) return
        clearRow(target.channel_type)
        if (target.channel_type === 'slack') connectSlack.mutate(target)
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
                : row.direction && !staleRows[row.channel]
                  // A paused row that still holds its binding names the state it
                  // ends: the same click as `Connect`, but a reader who has just
                  // read that the connection stands — the paused sub-line on a
                  // mirrored row, the direction tag on every bound row — under a
                  // verb that says "Connect" could not tell whether they were
                  // about to resume or to link anew. A born-in row is bound too
                  // and carries the tag, so it reads `Resume replies` as well.
                  // A never-connected offer keeps the plain verb: nothing on it
                  // asserts a connection. So does a row under its STALE notice —
                  // "Resume replies" asserts the very connection the notice says
                  // is no longer there, exactly as the withheld sub-line and tag
                  // would.
                  ? i18nT('components.linkedSurfacesSection.resume_replies_to', { label: row.label })
                  : i18nT('components.linkedSurfacesSection.connect_to', { label: row.label })}
            </span>
            {/* An OFFER's discriminator — which conversation on this channel the
              * row would connect — in the sub-line grammar the bound rows use, so
              * the verb line stays the channel's name and no raw id sits inside
              * a button label (see `ChannelRow.detail`). Ahead of the state
              * chain on purpose: an unavailable offer keeps it above its reason,
              * so two unavailable destinations on one channel stay told apart. */}
            {row.detail && (
              <span className="truncate text-[11px] text-muted">{row.detail}</span>
            )}
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
              // consequence lines assert the connection is standing ("the
              // connection stays"), and the notice directly under them says the
              // connection changed while this menu was open and nothing was
              // unlinked — a row that says both disagrees with itself. The verb
              // stays; the notice is the sub-line until it is dismissed or the
              // row is clicked again.
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
              // conversation's messages here, which the row's direction label
              // says; this line does not, so it cannot flip between two rows
              // that look alike. It says "the connection stays", not "the link
              // stays": "Copy link" two items up means a web address, and one
              // word for two things in one menu read as a collision. Same
              // styling as the reason line so the row keeps one visual grammar.
              <span className="truncate text-[11px] text-muted">
                {i18nT('components.linkedSurfacesSection.still_linked')}
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
            {/* Why this row has no Unlink, said on the row. A reader who meets a
              * mirrored channel's two controls and a born-in channel's one cannot
              * otherwise tell "why one connected chat gets both choices and this
              * one only gets the pause": the conversation IS the session's home,
              * so its connection can be paused, not removed. */}
            {row.bornIn && !row.disabledReason && (
              <span className="truncate text-[11px] text-muted">
                {i18nT('components.linkedSurfacesSection.born_in_note', { label: row.label })}
              </span>
            )}
          </span>
          {/* The direction, said once and visibly, on every bound row. Two rows
            * that look alike carried captions that flipped between "messages
            * there still reach this session" and "don't reach" with nothing
            * marking why, and a reader who met both stopped trusting either.
            * Rendered from the wire's `drives_session`, in the right-aligned tag
            * grammar the menus already use for a row's status. An offer carries
            * none: it is bound to nothing yet. Withheld under the row's STALE
            * notice for the same reason the consequence line is: a label that
            * asserts a standing link directly above "the connection changed
            * while this menu was open … nothing was unlinked" would have the
            * row disagree with itself. */}
          {row.direction && !staleRows[row.channel] && (
            <span className="ml-auto shrink-0 self-start text-[10px] text-muted">
              {i18nT(row.direction === 'two_way'
                ? 'components.linkedSurfacesSection.direction_two_way'
                : 'components.linkedSurfacesSection.direction_one_way', { label: row.label })}
            </span>
          )}
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
            {/* The keyboard-reachable hand-off to the agent, in the SAME branch
              * as the notice it describes — for the STALE refusal as well. Inside
              * Radix menu content the notice stays passive and this sibling item
              * is the only escalation the menu's roving focus can reach
              * (`errors-use-error-notice`: a notice with neither `askAgent` nor a
              * sibling item in its branch is a dead end). The stale case earned
              * a hand-off of its own once the notice said what happened rather
              * than "no longer linked": "the connection changed while this menu
              * was open" is a question the agent can answer — what it is linked
              * to now, and why the row moved — where a bare "menu out of date"
              * left the item reading as a control with no job. What the stale
              * state withholds is the Unlink item below, never the hand-off.
              * For the stale refusal the item also SAYS its job, in the sub-line
              * grammar every other item here uses: a bare sparkle item in a
              * menu whose every other action self-describes was the one control
              * a reader could not identify. An ordinary failure keeps the bare
              * item, as the hand-off reads everywhere else in the dashboard. */}
            <ErrorNoticeMenuItem
              Item={Item}
              message={rowErrors[row.channel]}
              describedBy={rowErrorId(row.channel)}
              outcome={staleRows[row.channel]
                ? i18nT('components.linkedSurfacesSection.ask_agent_stale_outcome')
                : undefined}
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
          * happens to the connection, then what stops. The Unlink line also says
          * that reconnecting brings it back: a bare removal verb reads as hard
          * to undo, and a reader who takes it that way does not dare click the
          * one control that ends the lock-out. The menu keeps that promise —
          * right after an unlink it offers the same destination as a fresh
          * `Connect` row — and the line says where: the session menu. Named
          * that way, not "this menu": the same rows also open under the paused
          * header chip, and an unlink from there removes the chip and its menu,
          * so "this menu" would point at a menu that is gone. The way back is its
          * own sentence ("Reconnect anytime from the session menu."): as a third
          * dash-clause the place attached to the nearest verb and read as
          * "removes the link from the session menu". The line no longer names
          * the kind of link — "two-way link" made a reader pause on exactly this
          * control; the tail carries what stops, and the row's direction label
          * above names which kind of link this is, so the two tails read as two
          * kinds of link rather than as one caption that contradicts itself. And
          * it says "the connection", not "the link": "Copy link" a few items up
          * means a web address, and one word for two things in one menu read as
          * a collision.
          *
          * Not rendered while the row's STALE notice shows. The notice says the
          * connection changed while this menu was open and nothing was unlinked;
          * an Unlink beneath it — live or dimmed — contradicted that, and a
          * reader who could not reconcile the two clicked nothing (a dimmed one
          * still read as "odd").
          * The refetch the refusal triggers redraws the row — often with the
          * very same shape, when the binding was re-linked on the same channel —
          * so the item follows the notice rather than the refetch: it returns
          * when the notice is dismissed or the row is clicked again, exactly
          * when the notice goes. */}
        {row.unlink && !staleRows[row.channel] && (
          <Item
            aria-busy={unlinkingChannel === row.channel ? true : undefined}
            onSelect={(event) => {
              // Same reason the toggle stays open: the row IS the state display,
              // and the binding disappearing from the menu is the confirmation.
              event.preventDefault()
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
