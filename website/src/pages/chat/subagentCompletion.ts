/**
 * Parsing for injected sub-agent completion events.
 *
 * When a sub-agent finishes, the gateway injects its result into the parent
 * session as the next turn's input (see gateway._subagent_done). That text is
 * written for the model: a `[Subagent completion event]` header, spawn-discipline
 * instructions ("do NOT re-run completed agents"), and either one agent's full
 * output or a whole wave's digest. Two shapes exist — the per-agent event and
 * the batch digest that replaces N per-agent turns once a wave exceeds the
 * digest chunk size.
 *
 * Kept separate from SubagentCompletionCard so the transcript grouping pass can
 * ask "does this render as a card?" without pulling a React component (and its
 * markdown renderer) into a pure module.
 */
import type { ChatMessage } from '../../types'

const SINGLE_PREFIX = '[Subagent completion event]'
const BATCH_PREFIX = '[Subagent batch completion event]'

/** True when `content` opens with either sub-agent completion marker. Prefix-only:
 *  callers here classify a QUEUED entry, where nothing is being rendered yet and
 *  an unparseable header must still count as machine orchestration. */
export function hasSubagentCompletionPrefix(content: string): boolean {
  return content.startsWith(SINGLE_PREFIX) || content.startsWith(BATCH_PREFIX)
}

/**
 * Per-agent header. Five shapes reach the transcript, and the glyph is the only
 * element common to all of them:
 *
 *     Agent `53e3e5eb` (kirocrew) completed ✅
 *     Agent `53e3e5eb` ❌
 *     Agent `53e3e5eb` ⚠️ orphaned by gateway restart
 *     Agent `53e3e5eb` ❌ lost to gateway restart
 *     Agent `53e3e5eb` ❌ delivery timed out
 *
 * The last three are composed by the restart-recovery and delivery-timeout paths
 * in subagent.py, which put the glyph MID-line with an explanation after it — so
 * the glyph cannot be anchored to end-of-line. Everything after the id is
 * captured and scanned for the first glyph instead: the glyph decides the
 * outcome (language-independent, and it does not shift when the backend rewords
 * a status), and any words beside it are carried into the payload rather than
 * dropped, since on those three shapes they are the only explanation there is.
 */
const AGENT_HEADER_RE = /^Agent `([^`\n]+)`(?: \(([^)\n]*)\))?([^\n]*)$/m
const OUTCOME_GLYPH_RE = /[✅❌⏹⚠]/
/** Status words this card already renders as a localized chip; repeating them in
 *  the payload would say the same thing twice in two languages. */
const REDUNDANT_STATUS_RE = /^(completed|failed|stopped by user)$/i
const TASK_RE = /^Task: (.*)$/m
/** Final digest of a wave: carries the terminal tallies. */
const WAVE_RE =
  /^Batch results (\d+)\/(\d+) — wave finished: (\d+) ✅ · (\d+) ❌ · (\d+) ⏹ of (\d+) agents\./m
/** Mid-wave digest chunk: carries delivered/running progress, no tallies. */
const CHUNK_RE = /^Batch results (\d+)\/(\d+) — (\d+) of (\d+) delivered, (\d+) still running\./m

export type SubagentOutcome = 'ok' | 'failed' | 'stopped' | 'interrupted'

const OUTCOME_BY_GLYPH: Record<string, SubagentOutcome> = {
  '✅': 'ok',
  '❌': 'failed',
  '⏹': 'stopped',
  // A restart orphan: the run did not finish, but its result WAS written to
  // disk, so this is neither a success nor a plain failure.
  '⚠': 'interrupted',
}

export interface ParsedSingleCompletion {
  kind: 'single'
  agentId: string
  /** Agent template the sub-agent ran as; empty when none was pinned. */
  agentName: string
  outcome: SubagentOutcome
  task: string
  /** Everything after the header block — the agent's own output. */
  body: string
}

export interface ParsedBatchCompletion {
  kind: 'batch'
  /** 1-based chunk index and the expected chunk count for this wave. */
  chunk: number
  chunks: number
  /** True for the wave's final chunk, which is the only one carrying tallies. */
  final: boolean
  ok: number
  failed: number
  stopped: number
  /** Wave size. Digests are only composed for waves of more than one agent. */
  total: number
  /** Results delivered so far (equals `total` on the final chunk). */
  delivered: number
  running: number
  /** The per-agent digest lines, failures first. */
  body: string
}

export type ParsedSubagentCompletion = ParsedSingleCompletion | ParsedBatchCompletion

/**
 * Split the machine-facing header block from the payload on the first blank line.
 *
 * A wave digest is composed as `header lines` + blank line + `digest`, so the
 * blank line is the structural boundary. Splitting there (rather than matching the
 * individual instruction sentences) drops the spawn-discipline guidance without
 * coupling this to its wording.
 */
function splitHeadBody(content: string): { head: string; body: string } {
  const i = content.indexOf('\n\n')
  if (i === -1) return { head: content, body: '' }
  return { head: content.slice(0, i), body: content.slice(i + 2).trim() }
}

/**
 * Split a PER-AGENT event into header and payload.
 *
 * The ordinary completion path separates them with a blank line, but the
 * restart-recovery and delivery-timeout paths in subagent.py do NOT — they run the
 * `Task:` line straight into the payload:
 *
 *     Agent `53e3e5eb` ⚠️ orphaned by gateway restart
 *     Task: …
 *     Result saved at: `/…/result.txt`
 *     Use the read tool to retrieve it.
 *
 * Anchoring only on the blank line therefore threw away exactly the lines those
 * three shapes exist to deliver — the location of an orphaned result, and whether
 * a result was captured at all. So when no blank line is present, the boundary is
 * the END OF THE HEADER instead: the marker line, the `Agent …` line, and the
 * optional `Task:` line. The blank-line split stays primary, because the ordinary
 * path emits one and a task string may itself wrap across lines.
 */
function splitAgentHeadBody(content: string): { head: string; body: string } {
  const blank = content.indexOf('\n\n')
  if (blank !== -1) return { head: content.slice(0, blank), body: content.slice(blank + 2).trim() }
  const lines = content.split('\n')
  let end = 2
  // Reuses TASK_RE so "what a Task line looks like" has one definition. It is a
  // pattern match on a gateway-composed wire line, not user-visible copy.
  if (TASK_RE.test(lines[end] ?? '')) end += 1
  return { head: lines.slice(0, end).join('\n'), body: lines.slice(end).join('\n').trim() }
}

/** Parse a completion event, or null when the header does not match the
 *  expected shape (the caller then falls back to normal rendering). */
export function parseSubagentCompletion(content: string): ParsedSubagentCompletion | null {
  if (content.startsWith(BATCH_PREFIX)) {
    const { head, body } = splitHeadBody(content)
    const wave = WAVE_RE.exec(head)
    if (wave) {
      const total = Number(wave[6])
      return {
        kind: 'batch',
        chunk: Number(wave[1]),
        chunks: Number(wave[2]),
        final: true,
        ok: Number(wave[3]),
        failed: Number(wave[4]),
        stopped: Number(wave[5]),
        total,
        delivered: total,
        running: 0,
        body,
      }
    }
    const chunk = CHUNK_RE.exec(head)
    if (chunk) {
      return {
        kind: 'batch',
        chunk: Number(chunk[1]),
        chunks: Number(chunk[2]),
        final: false,
        ok: 0,
        failed: 0,
        stopped: 0,
        total: Number(chunk[4]),
        delivered: Number(chunk[3]),
        running: Number(chunk[5]),
        body,
      }
    }
    return null
  }
  if (!content.startsWith(SINGLE_PREFIX)) return null
  const { head, body } = splitAgentHeadBody(content)
  const m = AGENT_HEADER_RE.exec(head)
  if (!m) return null
  const rest = m[3] || ''
  const glyph = OUTCOME_GLYPH_RE.exec(rest)
  // No glyph means this is not a shape this card understands; degrade to normal
  // rendering rather than guessing an outcome.
  if (!glyph) return null
  // Words beside the glyph. On the restart / timeout shapes this is the ONLY
  // explanation the message carries, so it joins the payload instead of being
  // discarded with the rest of the header.
  const note = rest.replace(OUTCOME_GLYPH_RE, ' ').replace(/\uFE0F/g, '').trim()
  const task = TASK_RE.exec(head)
  const keptNote = note && !REDUNDANT_STATUS_RE.test(note) ? note : ''
  return {
    kind: 'single',
    agentId: m[1],
    agentName: m[2] || '',
    outcome: OUTCOME_BY_GLYPH[glyph[0]] || 'ok',
    task: (task?.[1] || '').trim(),
    body: keptNote ? [keptNote, body].filter(Boolean).join('\n\n') : body,
  }
}

/** Per-message parse cache. The predicate below is called from ChatPage's render
 *  dispatch, from the transcript grouping pass (O(N) over every message on each
 *  update) and from TurnBlock's visibility test; a wave digest can reach 60 kB,
 *  so re-running the regexes on every render is real work. Keyed by the message
 *  object and invalidated by content identity. */
const parseCache = new WeakMap<
  ChatMessage,
  { content: string; parsed: ParsedSubagentCompletion | null }
>()

/** Cached `parseSubagentCompletion` for a message. */
export function parseSubagentCompletionMessage(message: ChatMessage): ParsedSubagentCompletion | null {
  const content = message.content || ''
  const cached = parseCache.get(message)
  if (cached && cached.content === content) return cached.parsed
  const parsed = parseSubagentCompletion(content)
  parseCache.set(message, { content, parsed })
  return parsed
}

/**
 * True when a message is an injected sub-agent completion event whose header
 * actually PARSES. Gating on a successful parse (not the prefix alone) is
 * deliberate: callers branch to the card on this predicate, and an unparseable
 * header must degrade to normal rendering rather than swallow the result the
 * user was waiting for.
 *
 * Role is checked loosely because the same event reaches the transcript under
 * three of them — `subagent` (the normal queue-drained injection), `assistant`
 * (the delivery-timeout variant), and `user` in scrollback persisted before
 * batch digests were classified as system injections.
 */
export function isSubagentCompletionMessage(message: ChatMessage): boolean {
  const role = message.role
  if (role !== 'subagent' && role !== 'assistant' && role !== 'user') return false
  const content = message.content || ''
  if (!hasSubagentCompletionPrefix(content)) return false
  return parseSubagentCompletionMessage(message) !== null
}
