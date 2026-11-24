/**
 * The Bot-first view of the crew that the XR page draws.
 *
 * The dashboard's own data is session-shaped: slots, approvals keyed by slot,
 * question cards keyed by slot. A person wearing glasses does not think in
 * sessions, so this module folds that data per Bot: each Bot has a state, one
 * line, up to three "things it is on", and the asks that wait on the person.
 * It is pure, so the same fold serves live data, the fixture and the tests.
 */
import type { MemberRosterRow } from '../../api/client'
import type { PendingQuestions } from '../../api/client/approvals'
import type { ChatSlot, CronJob, Notification, PendingApproval } from '../../types'

export type XrBotState = 'idle' | 'working' | 'needs' | 'failed'
export type XrThingState = 'idle' | 'working' | 'needs' | 'failed'

/** One thing a Bot is on: a topic, never a session name. */
export interface XrThing {
  /** The slot behind the thing. Used to open it, never shown. */
  slot: string
  /** The Bot's own thread, as opposed to work it handed out. */
  direct: boolean
  topic: string
  line: string
  state: XrThingState
  ts: number
}

/** How one read stands. A failed read is not an empty answer, and must not look like one. */
export type XrFeedState = 'loading' | 'ok' | 'failed'

export interface XrFeeds {
  roster: XrFeedState
  /** Approvals and question cards together: what feeds "needs you". */
  asks: XrFeedState
  /** The dashboard's live connection. Without it, states and counts go stale. */
  live: boolean
  /**
   * The store's list of sessions is loaded and whole. Left out by a caller that has no such list (the land's
   * sample crew): it then counts as whole.
   */
  rows?: boolean
}

export const FEEDS_OK: XrFeeds = { roster: 'ok', asks: 'ok', live: true, rows: true }

/** Whether every read that an ask comes from is in: the roster, the approvals and the questions, the live connection, and the whole list of sessions. */
export const wholeRead = (f: XrFeeds): boolean => f.roster === 'ok' && f.asks === 'ok' && f.live && f.rows !== false

export interface XrQuestion {
  header: string
  question: string
  options: string[]
  /** He may mark several of its options. */
  multi?: boolean
}

/** Something that waits on the person: "<Bot> asks, for <thing>: <action>". */
export interface XrNeed {
  id: string
  /** 'attention' is a thing flagged as waiting whose request no read carries yet. */
  kind: 'approval' | 'question' | 'attention'
  /**
   * Whether the Bot's own page draws a card for it: an approval that stands on a session's row, and a question
   * card. An approval that only the approvals list carries has none there, and neither has 'attention'.
   */
  card: boolean
  /** Slug of the Bot that asks; '' when no Bot owns the work behind it. */
  bot: string
  /** Display name of who asks. */
  who: string
  topic: string
  slot: string
  /** The short action line. */
  action: string
  tool: string
  /** The exact command or tool input, when the ask is a tool call. */
  command: string
  /** Every other field of the tool call, as `name: value` lines. An approval covers these too. */
  extra: string[]
  /** What a guarded press on this ask would decide: the request and its whole payload. */
  version: string
  purpose: string
  origin: 'native' | 'coordinator'
  requestId: string
  requestMid: string
  questions: XrQuestion[]
  /** A question that waits on a server-side ask (`POST /api/ask-question/{ask_id}/answer` answers it); '' else. */
  askId?: string
  ts: number
}

/** A cut session of a Bot (its own thread or a worker of it): the slot says `interrupted`. */
export interface XrCut {
  slot: string
  /** The session and its cut, told from the session's next cut and kept over a restart (`rimCutOf`). */
  id: string
  /** The gateway's read state calls the session unread (`dashboard.unreadSlots`). */
  unread: boolean
  /** The gateway calls the cut old (`interrupted_stale`): it is never red, only the dim mark. */
  stale: boolean
  /** The Bot's own thread, as opposed to a worker of it. */
  direct?: boolean
  /** The first of its todo tasks that is not done: what it was doing when it was cut (the failed sheet); '' for none. */
  doing?: string
  /** Its last activity, epoch seconds. */
  ts?: number
  /** Its rim is red for it (`rims.rimOf`): he has not pressed its Bot since, and it is unread or began while he was in. */
  red?: boolean
}

/** A cut slot's session and turn: the key, the gateway process that ran the turn, and the turn. */
export const cutOf = (s: ChatSlot): string => `${s.key}#${s.turn_gen ?? ''}:${s.turn ?? s.last_turn_ts ?? ''}`

/**
 * A cut slot's session and the time of its newest turn, for the rim: a restart of the gateway keeps that time and a
 * new cut of the session changes it (`rows/stamp.json`), while `turn_gen` and `turn` change at a restart.
 */
export const rimCutOf = (s: ChatSlot): string => `${s.key}@${s.last_turn_ts ?? ''}`

export interface XrBot {
  slug: string
  /** The crew name every per-Bot route is keyed on. */
  name: string
  /** The name to show. */
  label: string
  avatar: unknown
  state: XrBotState
  line: string
  /** Everything the Bot is on, most urgent first. A glance shows a few; the full layer shows all. */
  things: XrThing[]
  needs: XrNeed[]
  /** The Bot's own thread; '' until it was first opened. */
  thread: string
  /**
   * The Bot's own thread was cut: the slot says `interrupted` (its last turn has no reply and no turn is in flight),
   * also while a sub-agent of it still runs. The thread and the turn that was cut, so that each cut turn is told
   * from the next; '' when it is not cut. A worker's cut is not here but in `cuts`: its Resume is in the worker's
   * own chat, which brings no page.
   */
  cut: string
  /** Every cut session of the Bot, its own thread's and its workers'. */
  cuts: readonly XrCut[]
  /** Every session of the Bot as the slots list has it, its own thread and its workers, cut or not. */
  sessions: readonly string[]
  /**
   * `failed` here says only that a session of the Bot is cut. What the rim draws of it, red or the dim mark, is the
   * XR page's own call (`rims.ts`): `dim` is set there, and is false as the model folds the Bot.
   */
  dim: boolean
  lastTs: number
  unread: boolean
  /** The gateway's read state calls the Bot's own thread unread: it said something he has not read there. */
  ownUnread: boolean
  hasGoal: boolean
  /** What the Bot does on a schedule, soonest first. */
  jobs: XrJob[]
  /** Its sessions whose turn ended and that wait on nobody (P3, the done sheet), its own thread's and its workers'. */
  rest: XrRest[]
}

/**
 * A session of a Bot at rest: no turn runs in it, nothing in it waits on him, and it was not cut. What the done sheet
 * says comes from these fields only (`iterations/iter-15/DONE-PLAN.md`): no summary is written for it.
 */
export interface XrRest {
  slot: string
  /** The Bot's own thread, as opposed to work it handed out. */
  direct: boolean
  /** The work's title when it says something, else ''. */
  topic: string
  /** The first sentence of its last message. */
  line: string
  /** Its last activity, epoch seconds. */
  ts: number
  /** The gateway's read state calls it unread. */
  unread: boolean
  /** The changes and issues it links (`source_links`): a line each on the sheet, in the state's colour. */
  made: XrMade[]
  /** Its todo tasks that are not done, in their order. */
  left: string[]
}

/** A change or an issue that a session links, as the gateway reports it. */
export interface XrMade { kind: 'change' | 'issue'; number: number; state: string; ci: string }

/** One scheduled job of a Bot: its name and when it runs next (epoch seconds, 0 when unknown or switched off). */
export interface XrJob { id: string; name: string; next: number; on: boolean }

export interface XrCrew {
  bots: XrBot[]
  /** Every ask, oldest first: the order "next" walks. */
  needs: XrNeed[]
  /** Work no Bot owns. Counted, opened on the monitor, never listed in XR. */
  unowned: number
  feeds: XrFeeds
}

export interface XrCrewInput {
  members: readonly MemberRosterRow[]
  slots: readonly ChatSlot[]
  approvals: readonly XrCoordinatorApproval[]
  questions: readonly PendingQuestions[number][]
  unread: readonly string[]
  feeds?: XrFeeds
  /** The scheduled jobs. Left out, no Bot shows a schedule. */
  jobs?: readonly CronJob[]
}

/** One row of `GET /api/approvals`. */
export interface XrCoordinatorApproval {
  id: string
  instance?: string
  source?: string
  tool?: string
  tool_input?: string
  tool_purpose?: string
  slot?: string
  ts?: number
}

export const XR_MAX_THINGS = 3
const TOPIC_MAX = 44
const LINE_MAX = 140
/** A task left open is one line of a sheet: what fits on it at a sheet's body size. */
const LEFT_MAX = 44

/** A title that is only a machine name says nothing about the work. */
const MACHINE_TITLE = /^(chat|session|slot|worker|subagent|task)[-_ ]?[0-9a-f-]{4,}$|^[0-9a-f-]{12,}$/i

function clip(s: string, max: number): string {
  const one = s.replace(/\s+/g, ' ').trim()
  if ([...one].length <= max) return one
  return [...one].slice(0, max - 1).join('').trimEnd() + '…'
}

/** The first sentence of a message, without markdown marks. */
export function firstLine(text: string | undefined, max = LINE_MAX): string {
  if (!text) return ''
  const plain = text
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/\[OPTIONS:[^\]]*\]/g, ' ')
    .replace(/[*`#>]+/g, '')
    // An underscore inside a word is the word's own (`work_report`); at a word's edge it is a mark.
    .replace(/(?<![\p{L}\p{N}])_+|_+(?![\p{L}\p{N}])/gu, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
  const first = plain.split(/(?<=[.!?。！？])\s+|\n+/).map(p => p.trim()).find(Boolean) ?? ''
  return clip(first, max)
}

/** A topic line for the work behind a slot, or '' when its title says nothing. */
export function topicOf(slot: Pick<ChatSlot, 'title' | 'key'>): string {
  const title = (slot.title ?? '').trim()
  if (!title || MACHINE_TITLE.test(title) || title === slot.key) return ''
  return clip(title, TOPIC_MAX)
}

function tsOf(slot: ChatSlot): number {
  const raw = slot.last_activity_ts ?? slot.last_ts ?? slot.last_turn_ts ?? slot.created ?? ''
  const n = Date.parse(raw)
  return Number.isFinite(n) ? Math.floor(n / 1000) : 0
}

function thingState(slot: ChatSlot): XrThingState {
  if (slot.pending_approval || slot.needs_input) return 'needs'
  if (slot.running || slot.subagents_running) return 'working'
  if (slot.interrupted) return 'failed'
  return 'idle'
}

const STATE_RANK: Record<XrThingState, number> = { needs: 0, failed: 1, working: 2, idle: 3 }

/** The command inside a tool call's input, or the input itself. */
export function commandOf(toolInput: string | undefined): string {
  if (!toolInput) return ''
  try {
    const parsed: unknown = JSON.parse(toolInput)
    if (parsed && typeof parsed === 'object') {
      const o = parsed as Record<string, unknown>
      for (const k of ['command', 'cmd', 'script', 'query', 'path', 'url']) {
        if (typeof o[k] === 'string' && o[k]) return o[k] as string
      }
    }
    if (typeof parsed === 'string') return parsed
  } catch {
    return toolInput
  }
  return toolInput
}

/** The fields of a tool call other than its command, so nothing an approval covers is hidden. */
export function extrasOf(toolInput: string | undefined): string[] {
  if (!toolInput) return []
  try {
    const parsed: unknown = JSON.parse(toolInput)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return []
    const o = parsed as Record<string, unknown>
    const main = ['command', 'cmd', 'script', 'query', 'path', 'url'].find(k => typeof o[k] === 'string' && o[k])
    return Object.entries(o)
      .filter(([k, v]) => k !== main && v !== null && v !== undefined && v !== '')
      .map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`)
  } catch {
    return []
  }
}

function approvalNeed(
  who: string, bot: string, topic: string, slot: string, ts: number,
  a: { id: string; mid: string; tool: string; input: string; purpose: string; origin: 'native' | 'coordinator' },
  card: boolean,
): XrNeed {
  const command = commandOf(a.input)
  return {
    id: `a:${slot}:${a.id}`,
    kind: 'approval',
    card,
    bot, who, topic, slot,
    action: clip(a.purpose || command || a.tool, LINE_MAX),
    tool: a.tool,
    command,
    extra: extrasOf(a.input),
    version: `${a.id}|${a.mid}|${a.input}`,
    purpose: a.purpose,
    origin: a.origin,
    requestId: a.id,
    requestMid: a.mid,
    questions: [],
    ts,
  }
}

function nativeApproval(info: PendingApproval): { id: string; mid: string; tool: string; input: string; purpose: string; origin: 'native' | 'coordinator' } {
  return {
    id: info.request_id,
    mid: info.request_mid ?? '',
    tool: info.tool,
    input: info.tool_input,
    purpose: info.tool_purpose ?? '',
    origin: info.origin === 'coordinator' ? 'coordinator' : 'native',
  }
}

/** A session at rest as the done sheet reads it (`XrRest`). */
function restOf(s: ChatSlot, direct: boolean, unread: boolean): XrRest {
  const made = (s.source_links ?? [])
    .filter(l => Number.isFinite(l.number))
    .map(l => ({ kind: l.kind === 'issue' ? 'issue' as const : 'change' as const, number: l.number, state: l.state ?? '', ci: l.ci ?? '' }))
  const left = (s.todo?.tasks ?? []).filter(t => !t.completed).map(t => clip(t.text, LEFT_MAX)).filter(Boolean)
  // A Bot's own thread is titled with the Bot's name: it says nothing of the work, as for a thing (`buildXrCrew`).
  return { slot: s.key, direct, topic: direct ? '' : topicOf(s), line: firstLine(s.last_message), ts: tsOf(s), unread, made, left }
}

/** Fold the dashboard's session-shaped data into Bots, things and asks. */
export function buildXrCrew(input: XrCrewInput): XrCrew {
  const slotsByKey = new Map(input.slots.map(s => [s.key, s]))
  const unread = new Set(input.unread)
  const owned = new Set<string>()
  const seenApproval = new Set<string>()
  const questions = [...input.questions]

  const needsOf = (slot: ChatSlot, who: string, bot: string, topic: string): XrNeed[] => {
    const out: XrNeed[] = []
    const ts = tsOf(slot)
    if (slot.pending_approval && slot.pending_approval_info?.request_id) {
      const a = nativeApproval(slot.pending_approval_info)
      seenApproval.add(a.id)
      out.push(approvalNeed(who, bot, topic, slot.key, ts, a, true))
    }
    for (const c of input.approvals) {
      if (c.slot !== slot.key || seenApproval.has(c.id)) continue
      seenApproval.add(c.id)
      out.push(approvalNeed(who, bot, topic, slot.key, c.ts ?? ts, {
        id: c.id, mid: c.instance ?? '', tool: c.tool ?? '', input: c.tool_input ?? '', purpose: c.tool_purpose ?? '', origin: 'coordinator',
      }, false))
    }
    for (const q of questions) {
      if (q.slot !== slot.key) continue
      const first = q.questions[0]
      out.push({
        id: `q:${slot.key}:${q.ask_id ?? q.card_id ?? ''}`,
        kind: 'question', card: true,
        bot, who, topic, slot: slot.key,
        action: clip(first?.question ?? '', LINE_MAX),
        tool: '', command: '', extra: [], version: `${q.ask_id ?? q.card_id ?? ''}|${JSON.stringify(q.questions)}`,
        purpose: '', origin: 'native', requestId: q.ask_id ?? q.card_id ?? '', requestMid: '',
        questions: q.questions.map(x => ({ header: x.header ?? '', question: x.question, options: x.options.map(o => o.label), multi: !!x.multiSelect })),
        askId: q.ask_id ?? '',
        ts: q.ts ?? ts,
      })
    }
    // The slot says it waits on the person, but no read carries the request itself.
    // It still counts, so "needs you" never under-reports.
    if (!out.length && (slot.pending_approval || slot.needs_input)) {
      out.push({
        id: `x:${slot.key}`, kind: 'attention', card: false, bot, who, topic, slot: slot.key,
        action: firstLine(slot.last_message), tool: '', command: '', extra: [], version: `x:${slot.key}`,
        purpose: '', origin: 'native', requestId: '', requestMid: '', questions: [], ts,
      })
    }
    return out
  }

  const bots: XrBot[] = input.members.map(m => {
    const label = (m.display_name ?? '').trim() || m.name
    const thread = m.slot_key || input.slots.find(s => s.mode === 'member' && s.agent === m.name)?.key || ''
    const mine = input.slots.filter(s => s.key === thread || (thread !== '' && s.mate_root === thread))
    for (const s of mine) owned.add(s.key)
    const things: XrThing[] = []
    const needs: XrNeed[] = []
    for (const s of mine) {
      const direct = s.key === thread
      const topic = direct ? '' : topicOf(s)
      const asks = needsOf(s, label, m.slug, topic)
      // An ask that only the approvals list carries still makes the thing wait on the person.
      things.push({ slot: s.key, direct, topic, line: firstLine(s.last_message), state: asks.length ? 'needs' : thingState(s), ts: tsOf(s) })
      needs.push(...asks)
    }
    // The Bot's own thread counts as a thing only when it is doing something.
    const shown = things
      .filter(t => !t.direct || t.state !== 'idle')
      .sort((a, b) => STATE_RANK[a.state] - STATE_RANK[b.state] || b.ts - a.ts)
    const home = slotsByKey.get(thread)
    const working = mine.some(s => s.running || s.subagents_running) || (!home && m.running)
    const state: XrBotState = needs.length ? 'needs' : working ? 'working' : mine.some(s => s.interrupted) ? 'failed' : 'idle'
    return {
      slug: m.slug,
      name: m.name,
      label,
      avatar: m.avatar,
      state,
      line: firstLine(m.last_message ?? home?.last_message) || shown[0]?.line || '',
      things: shown,
      needs,
      thread,
      cut: home?.interrupted ? cutOf(home) : '',
      cuts: mine.filter(s => s.interrupted).map(s => ({ slot: s.key, id: rimCutOf(s), unread: unread.has(s.key), stale: !!s.interrupted_stale, direct: s.key === thread, doing: (s.todo?.tasks ?? []).find(t => !t.completed)?.text ?? '', ts: tsOf(s) })),
      sessions: mine.map(s => s.key),
      dim: false,
      lastTs: Math.max(m.last_active_ts ?? 0, ...mine.map(tsOf)),
      unread: mine.some(s => unread.has(s.key)),
      ownUnread: thread !== '' && unread.has(thread),
      hasGoal: !!m.has_goal,
      // A job belongs to the Bot it runs as. A sequence job names several; the first one owns it here.
      jobs: (input.jobs ?? [])
        .filter(j => (j.agent_sequence?.[0] ?? j.agent) === m.name)
        .map(j => ({ id: j.id, name: clip(j.name, 60), next: j.enabled ? j.next_run_ts ?? 0 : 0, on: j.enabled }))
        .sort((a, b) => (a.next || Infinity) - (b.next || Infinity) || (a.id < b.id ? -1 : 1)),
      rest: mine.filter(s => !s.running && !s.subagents_running && !s.interrupted && !s.pending_approval && !s.needs_input && !needs.some(n => n.slot === s.key)).map(s => restOf(s, s.key === thread, unread.has(s.key))),
    }
  })

  // Work no Bot owns still counts when it waits on the person: the count must be true.
  const stray: XrNeed[] = []
  let unowned = 0
  for (const s of input.slots) {
    if (owned.has(s.key)) continue
    unowned += 1
    stray.push(...needsOf(s, s.agent ?? '', '', topicOf(s)))
  }
  const needs = [...bots.flatMap(b => b.needs), ...stray].sort((a, b) => a.ts - b.ts)
  return { bots, needs, unowned, feeds: input.feeds ?? FEEDS_OK }
}

/**
 * The line a Bot has just said, or '' when it said nothing new.
 *
 * The roster's last line is the last thing said in the Bot's chat, and the roster does not say by whom: it can
 * be the person's own message. So a new line counts as the Bot's only when the Bot's turn has just ended. A
 * turn that ends with no word from the Bot leaves the line as it was, and nothing is put in its mouth.
 * shortcut: a roster that showed the turn's start before the person's message would let that message pass as
 * a reply; the real fix is an author on the roster's last line, which is the gateway's to add.
 */
export function replyOf(before: Pick<XrBot, 'line' | 'state'>, now: Pick<XrBot, 'line' | 'state'>): string {
  if (before.state !== 'working' || now.state === 'working' || now.state === 'needs') return ''
  return now.line !== before.line ? now.line : ''
}

/** A notification as the hub lists it: a title and one line, nothing to run. */
export interface XrNotice { key: string; title: string; body: string; ts: number; slot: string }

export function noticesFrom(items: readonly Notification[], max = 8): XrNotice[] {
  return items
    .filter(n => !n.acked)
    .slice(0, max)
    .map(n => ({ key: String(n.key ?? n.ts), title: clip(n.title ?? '', 60), body: firstLine(n.body, 100), ts: Math.floor(Date.parse(n.ts) / 1000) || 0, slot: n.slot ?? '' }))
}
