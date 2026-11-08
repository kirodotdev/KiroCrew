// Timeline threads: replies drawn under the comment they answer.
//
// A forge thread (a GitLab discussion, a GitHub inline review thread, an Azure
// DevOps PR thread) is a set of timeline comments sharing one `thread_id`. Drawn
// as flat timeline rows the replies lose what they answer, so the timeline nests
// them: the thread sits at its ROOT's place, with every reply under the root.
//
// That alone would bury new activity: a reply made today to a two-month-old
// comment would show up two months down a newest-first timeline. So each reply
// also leaves a compact MARKER at its own place in time, which names the thread
// and jumps to the reply. Nothing is shown twice in full, and nothing moves out
// of chronological order.

import type { TimelineEvent } from '../api'
import { anchorKeyOf } from './commentBatch'

export type TimelineItem =
  | { type: 'event'; ev: TimelineEvent }
  /** A thread with at least one reply. `replies` run oldest first, like a thread reads. */
  | { type: 'thread'; root: TimelineEvent; replies: TimelineEvent[] }
  /** Where a threaded reply happened in time; its full text is in its thread. */
  | { type: 'reply-marker'; ev: TimelineEvent; root: TimelineEvent }

function isComment(ev: TimelineEvent): boolean {
  return ev.kind === 'comment' || ev.kind === 'review_comment'
}

function byTime(a: TimelineEvent, b: TimelineEvent): number {
  return Date.parse(a.created_at) - Date.parse(b.created_at)
}

/**
 * Turn a newest-first timeline into display items. Comments without a thread, and
 * threads of one comment, stay plain events; a thread with replies becomes one
 * `thread` item at its root's position plus one `reply-marker` per reply.
 */
export function groupThreads(
  desc: readonly TimelineEvent[],
  {
    rootIsThreadId = false,
  }: {
    /** The thread handle is the root comment's own id. True on GitHub only: an
     * Azure DevOps thread numbers its comments from 1, so a reply's id can equal
     * the thread's id by coincidence. */
    rootIsThreadId?: boolean
  } = {},
): TimelineItem[] {
  const members = new Map<string, TimelineEvent[]>()
  for (const ev of desc) {
    if (!isComment(ev) || !ev.thread_id) continue
    const list = members.get(ev.thread_id)
    if (list) list.push(ev)
    else members.set(ev.thread_id, [ev])
  }
  const threads = new Map<string, { root: TimelineEvent; replies: TimelineEvent[] }>()
  for (const [id, list] of members) {
    if (list.length < 2) continue
    // `list` is newest first, so reversed it is in the forge's own order; the
    // stable sort keeps that order between comments with the same timestamp.
    const ordered = [...list].reverse().sort(byTime)
    // Where the thread handle IS the root comment's id, that names the root even
    // when a reply shares its timestamp. Elsewhere the oldest comment is it.
    const named = rootIsThreadId
      ? ordered.find((ev) => ev.id !== undefined && ev.id !== null && String(ev.id) === id)
      : undefined
    const root = named ?? ordered[0]
    threads.set(id, { root, replies: ordered.filter((ev) => ev !== root) })
  }

  return desc.map((ev): TimelineItem => {
    const thread = isComment(ev) && ev.thread_id ? threads.get(ev.thread_id) : undefined
    if (!thread) return { type: 'event', ev }
    if (ev === thread.root) return { type: 'thread', root: thread.root, replies: thread.replies }
    return { type: 'reply-marker', ev, root: thread.root }
  })
}

/** How close two replies by one author must be to share a card. */
export const RUN_WINDOW_MS = 30 * 60 * 1000

/**
 * Split a thread's replies (oldest first) into RUNS: consecutive replies by the
 * same author, each within RUN_WINDOW_MS of the one before. A run is drawn as one
 * card with a single header, the way chat apps stack a person's messages. A reply
 * with no known author never joins a run: "ghost" is not one person.
 */
export function groupRuns(replies: readonly TimelineEvent[]): TimelineEvent[][] {
  const runs: TimelineEvent[][] = []
  for (const ev of replies) {
    const run = runs[runs.length - 1]
    const prev = run?.[run.length - 1]
    const gap = prev ? Date.parse(ev.created_at) - Date.parse(prev.created_at) : Infinity
    if (run && prev && ev.actor && ev.actor === prev.actor && gap >= 0 && gap <= RUN_WINDOW_MS) {
      run.push(ev)
    } else {
      runs.push([ev])
    }
  }
  return runs
}

/** The DOM attribute a reply card carries, so a marker can find and flash it. */
export const REPLY_ATTR = 'data-thread-reply'

export function replyDomKey(ev: TimelineEvent): string {
  return anchorKeyOf(ev)
}
