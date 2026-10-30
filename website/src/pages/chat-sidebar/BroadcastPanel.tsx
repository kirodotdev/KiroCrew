/** The header menu's "Broadcast message" panel: one message sent to every
 *  open session a quick pick (or the custom criteria) reaches.
 *
 *  Every target gets the same `/api/chat` send. A session's own running turn
 *  gets it as a steer, taken in once the current tool call ends; any other
 *  session gets a plain send, so the server's hold behind live sub-agents and
 *  queued messages still applies. The row label says which, so the count on
 *  the Send button can be reconciled with what each session will do. */
import { useMemo, useState } from 'react'
import { useAppSelector } from '../../store'
import { withLiveLastTs } from '../../store/slotLastTs'
import { Megaphone } from 'lucide-react'
import { isCrewmateChat } from '../../components/AgentDropdownList'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import { sendTurn } from '../../chat-core/transport/sendTurn'
import { i18nT } from '../../i18n/t'
import { fmtRelative } from '../../i18n/format'
import {
  BROADCAST_STATES, PRESET_STATES, broadcastTargets, deliveryFor, sendSteers,
  type BroadcastContext, type BroadcastCriteria, type BroadcastPreset, type BroadcastSlot, type BroadcastState, type CrewmateMode,
} from '../../lib/broadcastTargets'
import type { ChatFolder, ChatTag } from '../../types'
import { laneExtrasFor, type LaneSignals } from './board'
import { DisclosureChevron } from './chrome'
import type { AgentInfo, Slot } from './types'

const PRESETS: readonly Exclude<BroadcastPreset, 'custom'>[] = ['loop', 'running', 'waiting', 'all']
const CREW_MODES: readonly CrewmateMode[] = ['only', 'include', 'exclude']
const DAY_MS = 24 * 3600_000
/** Sends in flight at once: enough to finish a wide broadcast quickly without
 *  opening one POST per session against the gateway in the same instant. */
const SEND_CONCURRENCY = 4

type RowResult = 'sending' | 'sent' | 'failed' | 'unknown'

const PRESET_KEYS: Record<Exclude<BroadcastPreset, 'custom'>, string> = {
  loop: 'pages.chatSidebar.broadcast_preset_loop',
  // The board's own lane name, so the pick and its state chip read the same.
  running: 'pages.chatSidebar.lane_working',
  waiting: 'pages.chatSidebar.broadcast_preset_waiting',
  all: 'pages.chatSidebar.broadcast_preset_all',
}
/** The turn states use the board lanes' own labels, so a state reads the same
 *  here as in the board view. */
const STATE_KEYS: Record<BroadcastState, string> = {
  loop: 'pages.chatSidebar.broadcast_state_loop',
  needs_approval: 'pages.chatSidebar.lane_needs_approval',
  waiting: 'pages.chatSidebar.lane_waiting',
  working: 'pages.chatSidebar.lane_working',
  idle: 'pages.chatSidebar.lane_idle',
}
const CREW_KEYS: Record<CrewmateMode, string> = {
  only: 'pages.chatSidebar.broadcast_crew_only',
  include: 'pages.chatSidebar.broadcast_crew_include',
  exclude: 'pages.chatSidebar.broadcast_crew_exclude',
}
const RESULT_KEYS: Record<RowResult, string> = {
  sending: 'pages.chatSidebar.broadcast_row_sending',
  sent: 'pages.chatSidebar.broadcast_row_sent',
  failed: 'pages.chatSidebar.broadcast_row_failed',
  unknown: 'pages.chatSidebar.broadcast_row_unknown',
}

const DELIVERY_KEYS = {
  steer: 'pages.chatSidebar.broadcast_row_steer',
  'after-answer': 'pages.chatSidebar.broadcast_row_after_answer',
  'after-work': 'pages.chatSidebar.broadcast_row_after_work',
  new: 'pages.chatSidebar.broadcast_row_new',
} as const

const chipCls = (on: boolean) =>
  `px-2.5 py-1 rounded-full text-[12px] ${on ? 'bg-accent text-accent-fg border-accent hover:bg-accent-hover' : ''}`

/** Run *task* over *items* with at most *limit* in flight. */
async function runPool<T>(items: readonly T[], limit: number, task: (item: T) => Promise<void>): Promise<void> {
  let next = 0
  const worker = async () => {
    while (next < items.length) {
      const item = items[next++]
      await task(item)
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker))
}

/** The first quick pick that reaches at least one session under the default
 *  crewmate setting; `all` when none does, so an empty list says "nobody". */
export function firstNonEmptyPreset(slots: readonly BroadcastSlot[], ctx: BroadcastContext): Exclude<BroadcastPreset, 'custom'> {
  const base: BroadcastCriteria = { preset: 'all', crewmates: 'exclude', states: [] }
  return PRESETS.find(p => broadcastTargets(slots, { ...base, preset: p }, ctx).length > 0) ?? 'all'
}

export default function BroadcastPanel({ slots: rowSlots, folders, tags, agents, signals, onClose, onOpenSession }: {
  slots: readonly Slot[]
  /** Opens one session, for the rows whose receipt could not be confirmed. */
  onOpenSession: (key: string) => void
  /** The board's live lane signals, so each session is classed as the board files it. */
  signals: LaneSignals
  /** The agent roster, so a crewmate chat with no kind stamp is still recognised. */
  agents: readonly AgentInfo[]
  folders: readonly ChatFolder[]
  tags: readonly ChatTag[]
  onClose: () => void
}) {
  // The "active within" filter and the row times read message recency, which
  // the sidebar's rows do not carry while a turn streams.
  const lastTsOverlay = useAppSelector(s => s.dashboard.slotLastTs)
  const slots = useMemo(() => withLiveLastTs(rowSlots, lastTsOverlay), [rowSlots, lastTsOverlay])
  const now = Date.now()
  const isCrewmate = (s: BroadcastSlot) => isCrewmateChat(s.agent_kind, s.agent ?? '', agents)
  const extrasFor = (s: BroadcastSlot) => laneExtrasFor(s.key, signals)
  const ctx = { folders, now, isCrewmate, extrasFor }

  const [message, setMessage] = useState('')
  // Opens on the first quick pick that reaches anyone (under the default
  // crewmate setting), so the panel never opens on an empty list for no
  // visible reason. Chosen once, at open: a pick that moved while the user
  // types would change who receives the message.
  const [preset, setPreset] = useState<BroadcastPreset>(() => firstNonEmptyPreset(slots, ctx))
  const [crewmates, setCrewmates] = useState<CrewmateMode>('exclude')
  const [customOpen, setCustomOpen] = useState(false)
  // Seeded from the default pick and re-seeded on every pick, so the custom
  // checkboxes always show what the highlighted pick means.
  const [states, setStates] = useState<readonly BroadcastState[]>(() => PRESET_STATES[preset === 'custom' ? 'loop' : preset])
  const [folderId, setFolderId] = useState('')
  const [tagId, setTagId] = useState('')
  const [pinnedOnly, setPinnedOnly] = useState(false)
  const [recentOnly, setRecentOnly] = useState(false)
  // Rows the user unticked, and rows a send already settled (so a second
  // press retries only what did not go out).
  const [unticked, setUnticked] = useState<ReadonlySet<string>>(new Set())
  const [results, setResults] = useState<Record<string, RowResult>>({})
  // The server's own reason per refused row, shown under that row.
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  // Set once a press leaves nothing failed or unknown; the line then counts
  // every row this broadcast reached, earlier presses included.
  const [sentCount, setSentCount] = useState(0)

  const criteria: BroadcastCriteria = {
    preset, crewmates, states,
    folderId: folderId || undefined,
    tagId: tagId || undefined,
    pinnedOnly,
    activeWithinMs: recentOnly ? DAY_MS : undefined,
  }
  const matched = broadcastTargets(slots, criteria, ctx)
  // Outcomes of the rows still matched. A row a different pick or filter has
  // since left out keeps its outcome (so it is never resent), but no longer
  // counts in the tally or the confirmation shown for this list.
  const shown: Record<string, RowResult> = Object.fromEntries(
    matched.filter(s => results[s.key]).map(s => [s.key, results[s.key]]),
  )
  const countOf = (r: RowResult) => Object.values(shown).filter(x => x === r).length
  // Refused rows, then unconfirmed ones (each with its way to the session),
  // then the rest, so nothing that needs a look is scrolled out of view.
  const rank = (s: BroadcastSlot) => (results[s.key] === 'failed' ? 0 : results[s.key] === 'unknown' ? 1 : 2)
  const targets = [...matched].sort((a, b) => rank(a) - rank(b))
  const presetCount = (p: Exclude<BroadcastPreset, 'custom'>) =>
    broadcastTargets(slots, { ...criteria, preset: p }, ctx).length
  const settled = (key: string) => results[key] === 'sent' || results[key] === 'unknown'
  const chosen = targets.filter(s => !unticked.has(s.key) && !settled(s.key))
  const text = message.trim()

  const toggleState = (s: BroadcastState) =>
    setStates(prev => (prev.includes(s) ? prev.filter(x => x !== s) : [...prev, s]))
  const toggleRow = (key: string) =>
    setUnticked(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  const pickPreset = (p: BroadcastPreset) => {
    setPreset(p)
    if (p === 'custom') setCustomOpen(true)
    else setStates(PRESET_STATES[p])
  }
  const folderName = (id?: string) => (id ? folders.find(f => f.id === id)?.name : undefined)

  const send = async () => {
    if (!text || chosen.length === 0 || sending) return
    setSending(true)
    setError('')
    setSentCount(0)
    const batch = chosen.map(s => s.key)
    const steers = new Map(chosen.map(s => [s.key, sendSteers(s)]))
    setResults(prev => ({ ...prev, ...Object.fromEntries(batch.map(k => [k, 'sending' as const])) }))
    const outcome: Record<string, RowResult> = {}
    await runPool(batch, SEND_CONCURRENCY, async (slot) => {
      const receipt = await sendTurn({ message: text, slot, ...(steers.get(slot) ? { steer: true } : {}) })
      // Only an explicit refusal is safe to retry. A late, unreadable or
      // dropped-connection receipt may have reached the server, which
      // dispatches before it answers, so a resend could deliver twice.
      const r: RowResult = receipt.status === 'dispatched' || receipt.status === 'queued'
        ? 'sent'
        : receipt.status === 'refused' ? 'failed' : 'unknown'
      outcome[slot] = r
      setResults(prev => ({ ...prev, [slot]: r }))
      if (receipt.status === 'refused' && receipt.reason) {
        const reason = receipt.reason
        setReasons(prev => ({ ...prev, [slot]: reason }))
      }
    })
    setSending(false)
    // Judged over the whole broadcast, not this press: an unconfirmed row from
    // an earlier press is still unconfirmed after a retry clears the refusals.
    // Rows outside the current list are left out, as in the tally.
    const all = Object.values({ ...shown, ...outcome })
    const failed = all.filter(r => r === 'failed').length
    const unknown = all.filter(r => r === 'unknown').length
    // A full success keeps the panel open on a confirmation, so the user can
    // see where the message went; each row already says Sent.
    if (failed === 0 && unknown === 0) {
      setSentCount(1)
      return
    }
    const parts: string[] = []
    if (failed > 0) parts.push(i18nT('pages.chatSidebar.broadcast_failed_some', { n: failed }))
    if (unknown > 0) parts.push(i18nT('pages.chatSidebar.broadcast_unknown_some', { n: unknown }))
    setError(parts.join(' '))
  }

  return (
    <div data-testid="broadcast-panel" className="mx-2 mb-2 p-3 min-h-0 overflow-y-auto rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
      <div className="font-medium text-text-strong mb-2"><Megaphone size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.broadcast_title')}</div>
      <textarea
        aria-label={i18nT('pages.chatSidebar.broadcast_message_label')}
        data-testid="broadcast-message"
        value={message}
        onChange={e => {
          setMessage(e.target.value)
          // New text is a new broadcast: every matched row gets it, including
          // the ones that already received the old text.
          if (Object.keys(results).length) { setResults({}); setReasons({}); setError(''); setSentCount(0) }
        }}
        disabled={sending}
        placeholder={i18nT('pages.chatSidebar.broadcast_placeholder')}
        rows={3}
        className="w-full resize-y rounded-md border border-border bg-bg-elevated text-text p-2 text-[13px] outline-hidden focus-visible:border-accent mb-2"
      />

      <div className="text-[11px] text-muted mb-1">{i18nT('pages.chatSidebar.broadcast_send_to')}</div>
      <div className="flex flex-wrap gap-1 mb-2" role="group" aria-label={i18nT('pages.chatSidebar.broadcast_send_to')}>
        {PRESETS.map(p => (
          <Btn key={p} data-testid={`broadcast-preset-${p}`} aria-pressed={preset === p} className={chipCls(preset === p)} onClick={() => pickPreset(p)}>
            {i18nT(PRESET_KEYS[p])} · {presetCount(p)}
          </Btn>
        ))}
        <Btn data-testid="broadcast-preset-custom" aria-pressed={preset === 'custom'} className={chipCls(preset === 'custom')} onClick={() => pickPreset('custom')}>
          {i18nT('pages.chatSidebar.broadcast_preset_custom')}
        </Btn>
      </div>

      <div className="text-[11px] text-muted mb-1">{i18nT('pages.chatSidebar.broadcast_crewmates')}</div>
      {/* One segmented control: three equal cells inside a shared border, so
          the row never grows past the panel and a long label truncates
          instead of wrapping onto a second line. */}
      <div className="flex mb-2 rounded-md border border-border overflow-hidden" role="group" aria-label={i18nT('pages.chatSidebar.broadcast_crewmates')}>
        {CREW_MODES.map(m => (
          <Btn
            key={m}
            data-testid={`broadcast-crew-${m}`}
            aria-pressed={crewmates === m}
            title={i18nT(CREW_KEYS[m])}
            className={`flex-1 min-w-0 justify-center rounded-none border-0 border-e border-border last:border-e-0 px-1 py-1 text-[12px] ${crewmates === m ? 'bg-accent text-accent-fg hover:bg-accent-hover' : ''}`}
            onClick={() => setCrewmates(m)}
          >
            <span className="truncate">{i18nT(CREW_KEYS[m])}</span>
          </Btn>
        ))}
      </div>

      <Btn
        data-testid="broadcast-custom-toggle"
        aria-expanded={customOpen}
        className="w-full justify-between text-[12px] mb-1"
        onClick={() => setCustomOpen(o => !o)}
      >
        <span>{i18nT('pages.chatSidebar.broadcast_custom_criteria')}</span>
        <DisclosureChevron open={customOpen} size={14} />
      </Btn>
      {customOpen && (
        <div data-testid="broadcast-custom" className="rounded-md border border-border p-2 mb-2">
          {preset !== 'custom' && (
            <div className="text-[12px] text-muted mb-1">{i18nT('pages.chatSidebar.broadcast_custom_hint')}</div>
          )}
          <div className="text-[11px] text-muted mb-1">{i18nT('pages.chatSidebar.broadcast_states_any')}</div>
          <div className="flex flex-wrap gap-1 mb-2" role="group" aria-label={i18nT('pages.chatSidebar.broadcast_states_any')}>
            {BROADCAST_STATES.map(s => (
              <Btn key={s} data-testid={`broadcast-state-${s}`} aria-pressed={states.includes(s)} className={chipCls(states.includes(s))} onClick={() => { setPreset('custom'); toggleState(s) }}>
                {i18nT(STATE_KEYS[s])}
              </Btn>
            ))}
          </div>
          <div className="text-[11px] text-muted mb-1">{i18nT('pages.chatSidebar.broadcast_scope_all')}</div>
          <div className="flex flex-col gap-1.5">
            {/* SimpleSelect: the codebase's select, which already sizes itself
                for the platform (a native list on touch, the compact popup on
                a mouse), so no call-site restyle is needed. */}
            <SimpleSelect
              aria-label={i18nT('pages.chatSidebar.broadcast_folder')}
              options={['', ...folders.map(f => f.id)]}
              optionLabels={[i18nT('pages.chatSidebar.broadcast_any_folder'), ...folders.map(f => f.name)]}
              value={folderId}
              onChange={v => { setPreset('custom'); setFolderId(v) }}
            />
            <SimpleSelect
              aria-label={i18nT('pages.chatSidebar.broadcast_tag')}
              options={['', ...tags.map(t => t.id)]}
              optionLabels={[i18nT('pages.chatSidebar.broadcast_any_tag'), ...tags.map(t => t.name)]}
              value={tagId}
              onChange={v => { setPreset('custom'); setTagId(v) }}
            />
            <label className="flex items-center gap-2 text-[12px] text-text cursor-pointer">
              <input type="checkbox" aria-label={i18nT('pages.chatSidebar.broadcast_pinned_only')} checked={pinnedOnly} onChange={e => { setPreset('custom'); setPinnedOnly(e.target.checked) }} />
              {i18nT('pages.chatSidebar.broadcast_pinned_only')}
            </label>
            <label className="flex items-center gap-2 text-[12px] text-text cursor-pointer">
              <input type="checkbox" aria-label={i18nT('pages.chatSidebar.broadcast_active_24h')} checked={recentOnly} onChange={e => { setPreset('custom'); setRecentOnly(e.target.checked) }} />
              {i18nT('pages.chatSidebar.broadcast_active_24h')}
            </label>
          </div>
        </div>
      )}

      {/* Before a send it is how many match; after one, the outcome tally, so
          the numbers above the list never disagree with the Send count. */}
      <div data-testid="broadcast-count" role="status" className="text-[11px] text-muted mt-2 mb-1">
        {Object.keys(shown).length
          ? i18nT('pages.chatSidebar.broadcast_tally', {
            sent: countOf('sent'),
            failed: countOf('failed'),
            unknown: countOf('unknown'),
          })
          : i18nT('pages.chatSidebar.broadcast_matched', { n: targets.length })}
      </div>
      {targets.length > 0 && (<>
        {/* The right column says how a row WILL receive the message until the
            list has been sent to, then what happened; its header follows. */}
        <div className="flex justify-between px-2 text-[11px] text-muted" aria-hidden="true">
          <span>{i18nT('pages.chatSidebar.broadcast_col_session')}</span>
          <span data-testid="broadcast-col-right">
            {Object.keys(shown).length ? i18nT('pages.chatSidebar.broadcast_col_result') : i18nT('pages.chatSidebar.broadcast_col_delivery')}
          </span>
        </div>
        {!Object.keys(shown).length && (
          <div data-testid="broadcast-delivery-hint" className="px-2 mb-1 text-[11px] text-muted">{i18nT('pages.chatSidebar.broadcast_delivery_hint')}</div>
        )}
        <ul className="max-h-[200px] overflow-y-auto rounded-md border border-border bg-bg-elevated px-2 mb-2" aria-label={i18nT('pages.chatSidebar.broadcast_targets')}>
          {targets.map(s => {
            const result = results[s.key]
            const ticked = !unticked.has(s.key)
            const folder = folderName(s.folder_id)
            const title = s.title || i18nT('pages.chatSidebar.broadcast_untitled')
            const meta = folder ?? (s.last_ts ? fmtRelative(s.last_ts, { now }) : '') // last-ts: withLiveLastTs rows
            return (
              <li key={s.key} data-testid={`broadcast-row-${s.key}`} className="py-1.5 border-b border-border last:border-b-0 text-[12px]">
                <div className="flex items-center justify-between gap-2">
                  <label className="flex flex-1 items-start gap-2 min-w-0 cursor-pointer">
                    {/* A tick means "part of this broadcast" before and after a send; a
                        row that already went out stays ticked and locked. */}
                    <input type="checkbox" className="mt-0.5" aria-label={title} checked={ticked} disabled={sending || settled(s.key)} onChange={() => toggleRow(s.key)} />
                    {/* Folder, else last activity, on its own line under the
                        title: it tells same-named rows apart without ever
                        crowding the right column out of a narrow panel. */}
                    <span className="flex flex-col min-w-0">
                      <span className="truncate text-text">{title}</span>
                      {meta && <span className="truncate text-[11px] text-muted">{meta}</span>}
                    </span>
                  </label>
                  {result === 'unknown'
                    ? (
                      // The confirmation line says to check these sessions;
                      // the label is the way there.
                      <Btn data-testid={`broadcast-open-${s.key}`} className="min-w-0 max-w-[45%] text-[12px] px-1.5 py-0.5 text-end whitespace-normal" onClick={() => onOpenSession(s.key)}>
                        {i18nT('pages.chatSidebar.broadcast_open_unknown')}
                      </Btn>
                    )
                    : (
                      // At most 45% of the row and free to wrap, so in a narrow pane the
                      // title keeps the larger share and stays readable.
                      <span className={`min-w-0 max-w-[45%] text-end ${result === 'failed' ? 'text-danger' : 'text-muted'}`}>
                        {result
                          ? i18nT(RESULT_KEYS[result])
                          : i18nT(DELIVERY_KEYS[deliveryFor(s, extrasFor(s))])}
                      </span>
                    )}
                </div>
                {/* No hand-off: the broadcast text above is not saved anywhere,
                    and the navigation a hand-off makes would unmount it. */}
                {result === 'failed' && reasons[s.key] && (
                  <ErrorNotice message={reasons[s.key]} className="mt-1" testId={`broadcast-reason-${s.key}`} />
                )}
              </li>
            )
          })}
        </ul>
      </>)}

      {/* No hand-off: the broadcast text above is not saved anywhere, and the
          navigation a hand-off makes would unmount this panel and lose it. */}
      <ErrorNotice message={error} className="mb-2" testId="broadcast-error" />
      {sentCount > 0 && (
        <div data-testid="broadcast-sent" role="status" className="text-[12px] text-ok mb-2">
          {i18nT('pages.chatSidebar.broadcast_sent_all', { n: countOf('sent') })}
        </div>
      )}
      <div className="flex items-center gap-2 justify-end">
        {/* Disabled while sending: closing would not stop the sends already
            under way, and the batch would close a panel reopened meanwhile. */}
        <Btn data-testid="broadcast-close" className="text-[12px] px-3 py-1" disabled={sending} onClick={onClose}>
          {Object.keys(results).length ? i18nT('pages.chatSidebar.close') : i18nT('pages.chatSidebar.cancel')}
        </Btn>
        <Btn
          primary
          data-testid="broadcast-send"
          className="text-[12px] px-3 py-1"
          disabled={!text || chosen.length === 0 || sending}
          onClick={() => { void send() }}
        >
          {/* After a send, what is left to press are the refused rows, so the
              button names that: a bare count read as the unconfirmed one. */}
          {sending
            ? i18nT('pages.chatSidebar.broadcast_sending')
            : Object.keys(shown).length
              ? i18nT('pages.chatSidebar.broadcast_retry', { n: chosen.length })
              : i18nT('pages.chatSidebar.broadcast_send', { n: chosen.length })}
        </Btn>
      </div>
    </div>
  )
}
