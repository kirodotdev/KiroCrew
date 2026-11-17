/**
 * Which composers hold dictated text, and the note that tells the agent so
 * (`stt.dictation_note`, Settings → Voice → Fine-tuning).
 *
 * A transcript lands in the composer as ordinary draft text, so by send time the
 * text alone cannot say it was spoken. The Voice atom therefore records the fact
 * when it writes, keyed by the session whose composer (or parked draft) received
 * the words, and each send path asks once, for the slot it is sending from.
 *
 * The record is per SESSION, not per page, because one composer serves many
 * slots: dictating in A and switching to B must not mark B's typed message, and
 * coming back to A must still find A marked. `null` is the composer of a session
 * that does not exist yet (a fresh chat before its first send).
 *
 * Lifetime:
 *   - set by every dictation write (streaming partial, final, batch transcript,
 *     an off-screen delivery into a parked draft, a retried recording);
 *   - kept through the user's edits, since fixing a misheard word does not make
 *     the rest of the message typed. The rule is per composer, not per word: a
 *     composer counts as dictated from the first dictated word until it is sent
 *     or emptied, even if a later edit removes every dictated word;
 *   - kept through a reload of the tab. When a new chat takes over what was
 *     entered while it was being created, the record never moves to it: the
 *     old chat keeps its record only if it was marked before the create and
 *     still holds its draft, and the new chat starts unmarked. Words dictated
 *     in that short window therefore go out without the note, which is the
 *     safe side: a typed message is never marked;
 *   - not set by a streaming hypothesis that retracts to nothing, which puts
 *     the earlier draft back and dictates no words;
 *   - cleared when a send takes the text (`takeDictated`), and when the user's
 *     own edit empties the composer, so a fresh message typed after deleting a
 *     dictation is not marked;
 *   - put back by a send that hands its text back (a refused or failed send, a
 *     steer returned to the composer, a cancelled queued card), because the
 *     restored words are still the dictated ones. The sender carries the fact
 *     and the note-free text it restores; neither is ever inferred from the
 *     words, which could be ones the user typed.
 *
 * The note itself is part of the message text, the way the gateway's own
 * provenance lines are (`[sent by session …]`, `[auto-nudge cycle N]`): it
 * reaches the agent on every path a message can take (dispatch, queue, steer,
 * regenerate, resume) without each of them carrying a flag, and the transcript
 * shows exactly what the agent was told. Its text lives in
 * `dictationNote.prompt.ts`, English on purpose because the model reads it.
 */
import { queryClient } from '../../api/queryClient'
import { safeGetSessionItem, safeSetSessionItem } from '../../utils/safeStorage'
import { dictationNoteText } from './dictationNote.prompt'

/** The line appended to a dictated message. Exactly this text, once, at the end. */
export const DICTATION_NOTE = dictationNoteText()

/** Between the message and the note: a paragraph break, so the note never reads
 *  as the last words of the user's own sentence. */
const NOTE_SEPARATOR = '\n\n'

/**
 * The scope a split pane (`ChatPane`) keys its records under. A pane keeps its
 * own draft for a session, apart from the main chat page's draft for the same
 * session (`utils/chatPaneDrafts`), so the two composers need two records: words
 * dictated into the pane must not mark a message typed on the page.
 */
export const PANE_DICTATION_SCOPE = 'pane'

/**
 * Where the record survives a reload of this tab. Session storage, the same
 * lifetime as the shortest-lived draft it describes (pane and crew-window
 * drafts): a record kept longer than its draft could mark a message typed into
 * the empty composer that replaced it, while one kept shorter only drops a note.
 */
export const DICTATION_STORAGE_KEY = 'mc-dictated-drafts'
/** Records kept at most. A record lives until its composer sends or empties,
 *  so this is a backstop against keys whose sessions were closed unsent. */
const MAX_RECORDS = 200

let dictated: Set<string> | null = null
function records(): Set<string> {
  if (dictated) return dictated
  dictated = new Set()
  try {
    const parsed: unknown = JSON.parse(safeGetSessionItem(DICTATION_STORAGE_KEY) || '[]')
    if (Array.isArray(parsed)) for (const k of parsed) if (typeof k === 'string') dictated.add(k)
  } catch { /* unreadable: start empty, which only drops notes */ }
  return dictated
}
function persist(): void {
  const all = records()
  // Oldest first: a Set iterates in insertion order.
  while (all.size > MAX_RECORDS) all.delete(all.values().next().value as string)
  safeSetSessionItem(DICTATION_STORAGE_KEY, JSON.stringify([...all]))
}
/** One record per (composer scope, session). The main chat page uses no scope. */
const keyOf = (session: string | null | undefined, scope?: string): string =>
  (scope ? scope + '\u0000' : '') + (session ?? '')

/** Record that dictated words were written into `session`'s composer or draft. */
export function markDictated(session: string | null | undefined, scope?: string): void {
  const k = keyOf(session, scope)
  const all = records()
  if (all.has(k)) return
  all.add(k)
  persist()
}

/** Forget it: the composer no longer holds the dictated words. */
export function clearDictated(session: string | null | undefined, scope?: string): void {
  if (records().delete(keyOf(session, scope))) persist()
}

export function isDictated(session: string | null | undefined, scope?: string): boolean {
  return records().has(keyOf(session, scope))
}

/**
 * A send is taking `session`'s composer text: answer whether it held dictation
 * and clear the record, since the composer is about to be emptied. A send path
 * that hands the text back calls `markDictated` again with this answer.
 */
export function takeDictated(session: string | null | undefined, scope?: string): boolean {
  const was = isDictated(session, scope)
  clearDictated(session, scope)
  return was
}

/**
 * The user's own edit left `text` in `session`'s composer. An empty composer
 * holds no dictated words, so the next message starts unmarked. Any other edit
 * keeps the record: editing a transcript does not make it typed.
 */
export function noteComposerEdit(session: string | null | undefined, text: string, scope?: string): void {
  if (!text.trim()) clearDictated(session, scope)
}

/** Whether the setting is on, read from the same `['sttConfig']` query the Voice
 *  atom and the Voice settings panel share, so a toggle applies to the next send
 *  without a reload. Unknown (not loaded yet) reads as off, the default. */
export function dictationNoteEnabled(): boolean {
  const cfg = queryClient.getQueryData<{ dictation_note?: boolean }>(['sttConfig'])
  return cfg?.dictation_note === true
}

/** Whether the gateway reads `text`, as sent, as a command: the same test as
 *  its own (`message.lstrip().startswith("/")`). */
export function isCommandText(text: string): boolean {
  return text.trimStart().startsWith('/')
}

/** Whether a send that consumed its composer's provenance as `wasDictated`
 *  carries the note. Never for a command, whose arguments the note would become
 *  part of (`/goal clear` plus the note arms a goal instead of clearing it).
 *  `text` is the text exactly as it will be sent, after any quote or knowledge
 *  block is put in front of it; `buildOutgoingTurn` makes the same check on
 *  its own finished wire. */
export function wantsDictationNote(wasDictated: boolean, text = ''): boolean {
  return wasDictated && !isCommandText(text) && dictationNoteEnabled()
}

/**
 * `text` ending in exactly one note. Idempotent, so text that already carries
 * it (a message edited and sent again) never gets a second one.
 */
export function withDictationNote(text: string): string {
  const body = text.trimEnd()
  if (body.endsWith(DICTATION_NOTE)) return body
  return body ? body + NOTE_SEPARATOR + DICTATION_NOTE : DICTATION_NOTE
}

/** Test seam: forget every record, in memory and in storage, so the next read
 *  loads from storage again. */
export function _resetDictationProvenance(): void {
  dictated = null
  safeSetSessionItem(DICTATION_STORAGE_KEY, '[]')
}
