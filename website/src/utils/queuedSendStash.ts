/** The queued-send stash, in a module of its own.
 *
 *  Deliberately NOT in `useQueuedMessageActions`: the slot-detail hydration path in the chat slice
 *  has to reach this store, and importing the hook module from the slice closes an import cycle
 *  (the hook imports the slice's action creators) which leaves the chat reducer undefined at
 *  module-init. No React, no store imports, so every writer and reader can share the one store. */

import { restoreQueuedContent } from './fileTokens'
import type { MessageQuote } from '../chat-core/composer/messageQuote'

/** Pre-serialization composer state of a send the server QUEUED, written by the
 *  host's send path when the `queued: true` receipt names the entry. */
export interface QueuedSendRecord {
  /** The text exactly as the user typed it. */
  raw: string
  /** The staged file paths at send time. */
  files: string[]
  /** The exact POSTed LLM-facing text — the edit guard: an entry edited after
   *  send keeps its queue id but fails this equality, so an edited card falls
   *  to the parser instead of clobbering the edit with pre-edit state. */
  sent: string
  /** The slot's recorded `@rel` alias map for the sent files, captured before
   *  the send-clear dropped it (fork GPT review): restoring text + files
   *  WITHOUT the aliases re-created the pre-fix stuck chip — the restored
   *  mention was invisible to reconciliation, so hand-deleting it left a
   *  stale chip that the next send silently re-attached. */
  aliases?: Record<string, string[]>
  /** The whole-message quote the send consumed (`chat-core/composer/messageQuote`),
   *  so a cancel puts it back as a staged card rather than leaving its `>` block
   *  in the restored text -- or, without a stash hit, losing it (fork GPT review). */
  quote?: MessageQuote
}

/** Queued-send stash, keyed by the `queue_id` the send receipt returns (the
 *  same id `queue_push` broadcasts and the card's cancel button carries).
 *  Queue identity is the ONLY sound key: the serialization is not injective
 *  (image @-tokens are erased from the LLM-facing text), so content-keyed
 *  records can collide across different captions, duplicate sends, and other
 *  tabs. Module-level so every host's send path (ChatPage, ChatPane) writes
 *  the one store this hook's cancel consumes — the same one-owner reasoning
 *  as the hook itself (#5891). Deliberately unevicted: an entry dies on the
 *  cancel that consumes it, and evicting a live entry would degrade that
 *  card's cancel to the parser fallback; orphans from normal delivery are
 *  three small strings bounded by queued sends per tab session. */
export const queuedSendStash = new Map<string, QueuedSendRecord>()

/** Write the receipt-path record, PRESERVING one `queue_push` already adopted.
 *
 *  `queue_push` can win the race against its own HTTP receipt. The adopted record's `sent` is the
 *  server's BROADCAST content, which is what the cancel guard compares against the card; the
 *  receipt's is the sender's un-redacted copy. Overwriting therefore broke cancel on a redacted
 *  entry, dropping the attachments the parser fallback cannot recover. */
export function stashQueuedSend(queueId: string | undefined, rec: QueuedSendRecord): void {
  if (!queueId || queuedSendStash.has(queueId)) return
  queuedSendStash.set(queueId, rec)
}

/** Whether `refs` mentions exactly this path, rather than merely containing it. A plain substring
 *  test kept `/tmp/report.pdf` alive on the strength of `/tmp/report.pdf.bak`, restoring a removed
 *  attachment onto a resend. A real mention ends the path: end of text, or a non-path character. */
function mentionsPath(refs: string, path: string): boolean {
  if (!path) return false
  let from = 0
  for (;;) {
    const at = refs.indexOf(path, from)
    if (at < 0) return false
    const before = at > 0 ? refs[at - 1] : undefined
    const after = refs[at + path.length]
    // Both ends must be a boundary: an absolute path is a contiguous SUFFIX of any path holding it,
    // so checking only the trailing side accepts a longer path that merely ends the same way.
    const opens = before === undefined || /[\s"'`,;:([{<]/.test(before)
    const closes = after === undefined || /[\s"'`,;:)\]}>]/.test(after)
    if (opens && closes) return true
    from = at + 1
  }
}

/** Re-point a stashed record at the EDITED entry. Dropping it instead sent a later cancel to the
 *  parser, which cannot recover an attachment path containing a space. */
export function restashEditedSend(
  queueId: string | undefined,
  submitted: string,
  display: string,
): void {
  if (!queueId) return
  const rec = queuedSendStash.get(queueId)
  if (!rec) return
  // An edit that DROPPED a marker prunes that attachment server-side, so carrying `files` through
  // unchanged re-staged a removed file on cancel and a blind retry would resend it.
  const refs = `${submitted}\n${display}`
  const declared = new Set([
    ...restoreQueuedContent(submitted).files,
    ...restoreQueuedContent(display).files,
  ])
  // Both texts are consulted so redaction cannot look like a REMOVED attachment. Containment also
  // covers what the parser will not CLAIM: its `(\S+)` capture truncates a path holding a space.
  const files = rec.files.filter(p => declared.has(p) || mentionsPath(refs, p))
  // `raw` is what the user SUBMITTED, `sent` what the card SHOWS -- redaction rewrites the display
  // form, so collapsing the two restored the server's text into the composer instead of the user's.
  queuedSendStash.set(queueId, { ...rec, raw: submitted, sent: display, files })
}
