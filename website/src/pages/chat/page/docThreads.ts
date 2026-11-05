/**
 * The doc pane's side of the conversation with its conductor (PoC): the turn a
 * highlight sends, and the transcript read back into per-highlight threads.
 *
 * A turn opens with `[Doc ask ID]` and the person's words, so the transcript alone
 * says which highlight every reply belongs to; nothing else is stored.
 */
import type { ChatMessage } from '../../../types'
import { splitReply, type DocActions, type ThreadMsg } from './docConductor'
import { isGoalWake } from './goalLoop'
import { GOAL_BEAT_COUNT, goalThread, isQuietReport, type GoalCheck } from './docGoals'

/** The page's main thread: the conversation about the page as a whole, which no
 *  marker or ask names. Its turns are asks under this id, read like any other. */
export const MAIN_THREAD = 'main'

const HEADER_RE = /^\[Doc ask ([A-Za-z0-9_-]+)\]\n([\s\S]*?)(?:\n\n---\n|$)/

const ASK_MARKER = /<!-- \/?ask:[A-Za-z0-9_-]+ -->/g

/** The doc's text as the conductor's copy compares it: without ask markers, so a
 *  new highlight alone is not a change that sends a fresh copy. */
export const docTextForCopy = (doc: string) => doc.replace(ASK_MARKER, '')

/** The line just above ask *id*'s marker, which an empty-spot ask writes after. */
function lineAbove(doc: string, id: string): string {
  const at = doc.indexOf(`<!-- ask:${id} -->`)
  if (at < 0) return ''
  const before = docTextForCopy(doc.slice(0, at)).split('\n').map(l => l.trim()).filter(Boolean)
  return before.at(-1) ?? ''
}

/**
 * The turn the doc slot's conductor receives for one highlight. Short by design: the
 * conventions are in the session's standing brief (`doc_session_brief.py`), so a
 * turn carries only what changed. *title* and *copy* are given only when they differ
 * from what the conductor was last sent; null leaves them out. *doc* is the doc as
 * it is now, markers included, read only to place an empty-spot ask.
 */
export function buildAskTurn(o: { id: string; request: string; quote: string; doc: string; title: string | null; copy: string | null }): string {
  const about = o.id === MAIN_THREAD
    ? 'About the document as a whole (its main thread, not a highlight). Your answer shows in the main thread.'
    : o.id.startsWith('goal-')
    ? `The goal's section:\n"""\n${o.quote}\n"""`
    : o.quote.trim()
    ? `Highlighted:\n"""\n${o.quote}\n"""`
    : lineAbove(o.doc, o.id)
    ? `At the spot after: "${lineAbove(o.doc, o.id)}"`
    : 'At the start of the document.'
  const parts = [`[Doc ask ${o.id}]\n${o.request}\n\n---\n${about}`]
  if (o.title !== null) parts.push(`[Doc title] "${o.title || 'Untitled doc'}"`)
  if (o.copy !== null) parts.push(`<<<DOC\n${o.copy}\nDOC>>>`)
  return parts.join('\n\n')
}

export interface ThreadRead {
  messages: ThreadMsg[]
  /** Actions in the order they were replied, keyed by the reply row they came from. */
  actions: { key: string; actions: DocActions }[]
  /** The last word is the person's: a reply is still owed. */
  awaiting: boolean
  /** A goal's thread only: its finished checks, oldest first. */
  checks?: GoalCheck[]
}

/** The thread every goal check's reply goes into. Not an ask id: no marker names it. */
export const GOALS_THREAD = '__goals__'

const GOAL_BLOCK = /^\[goal ([A-Za-z0-9_-]+)\][ \t]*$/m

/** A goal check's reply, cut at its `[goal <id>]` lines into one report per goal. */
export function splitGoalBlocks(prose: string): { blocks: { id: string; text: string }[]; rest: string } {
  const parts = prose.split(new RegExp(GOAL_BLOCK.source, 'gm'))
  const rest = parts[0].trim()
  const blocks: { id: string; text: string }[] = []
  for (let i = 1; i + 1 < parts.length; i += 2) {
    const text = parts[i + 1].trim()
    if (text) blocks.push({ id: parts[i], text })
  }
  return { blocks, rest }
}

/** What the latest check said about goal *goalId*, or null before it has said anything. */
export function latestGoalReport(threads: Record<string, ThreadRead>, goalId: string): string | null {
  const t = threads[goalThread(goalId)]
  const last = t ? [...t.messages].reverse().find(m => m.who === 'conductor') : undefined
  return last ? last.text : null
}

/** Whether a goal check is waiting on its reply: every goal's pin shows it working. */
export const goalCheckPending = (threads: Record<string, ThreadRead>) => !!threads[GOALS_THREAD]?.awaiting

/**
 * What a transcript row does to the thread being read: `undefined` when it opens no
 * turn (a reply, a tool row), otherwise the thread the turn opens, or `null` for a
 * turn that belongs to no thread. A loop's wake arrives as a `nudge` row and a
 * person's ask as a `user` one; both open a turn, so a reply is read against
 * whichever came last. One rule, shared with the session view, so what that view
 * labels a row is what this reader does with it.
 */
export function openedThread(m: Pick<ChatMessage, 'role' | 'content'>): string | null | undefined {
  if (m.role !== 'user' && m.role !== 'nudge') return undefined
  if (isGoalWake(m.content ?? '')) return GOALS_THREAD
  return HEADER_RE.exec(m.content ?? '')?.[1] ?? null
}

/** Read the doc slot's transcript into one thread per highlight id. */
export function deriveThreads(messages: ChatMessage[]): Record<string, ThreadRead> {
  const threads: Record<string, ThreadRead> = {}
  let current: string | null = null
  messages.forEach((m, i) => {
    const opened = openedThread(m)
    if (opened !== undefined) {
      current = opened
      if (current === null) return
      const t = (threads[current] ??= { messages: [], actions: [], awaiting: false })
      t.awaiting = true
      // A goal check: its own thread, so its reply's writes land and the goals can
      // show what it said. The person never typed it, so it adds no message of theirs.
      if (current === GOALS_THREAD) return
      const h = HEADER_RE.exec(m.content ?? '')
      if (h) t.messages.push({ who: 'you', text: h[2].trim() })
      return
    }
    if (m.role !== 'assistant' || !current) return
    const t = threads[current]
    const { prose, actions } = splitReply(m.content ?? '')
    if (current === GOALS_THREAD) {
      // One check answers every goal: each `[goal <id>]` block goes to that goal's
      // own thread, and only what no block claimed stays in the shared one.
      const { blocks, rest } = splitGoalBlocks(prose)
      for (const b of blocks) {
        const g = (threads[goalThread(b.id)] ??= { messages: [], actions: [], awaiting: false })
        g.messages.push({ who: 'conductor', text: b.text })
        g.awaiting = false
        const at = Date.parse(m.ts ?? '')
        g.checks = [...(g.checks ?? []), { ts: Number.isFinite(at) ? at : undefined, acted: !isQuietReport(b.text) }].slice(-GOAL_BEAT_COUNT)
      }
      if (rest) t.messages.push({ who: 'conductor', text: rest })
      if (prose) t.awaiting = false
      if (actions) t.actions.push({ key: String((m.meta as { mid?: unknown } | undefined)?.mid ?? `${m.ts ?? ''}#${i}`), actions })
      return
    }
    if (prose) {
      const last = t.messages[t.messages.length - 1]
      if (last?.who === 'conductor') last.text = `${last.text}\n\n${prose}`
      else t.messages.push({ who: 'conductor', text: prose })
      t.awaiting = false
    }
    if (actions) {
      const key = String((m.meta as { mid?: unknown } | undefined)?.mid ?? `${m.ts ?? ''}#${i}`)
      t.actions.push({ key, actions })
    }
  })
  return threads
}
