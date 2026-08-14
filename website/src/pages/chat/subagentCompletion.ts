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
 * The header facts a card needs — outcome, tallies, chunk index, agent id — are
 * stamped as structured fields on the message's `meta.subagentCompletion` at
 * composition time (gateway._subagent_done, subagent.py). This module reads
 * those first (`fromMeta`); the header regexes below are a LEGACY-SCROLLBACK
 * fallback for rows persisted before the meta was stamped. That inversion is
 * the point of #1792: a reword of the gateway's prose can no longer silently
 * break card rendering, because no live consumer parses the prose — the regexes
 * only run when the structured meta is absent.
 *
 * Kept separate from SubagentCompletionCard so the transcript grouping pass can
 * ask "does this render as a card?" without pulling a React component (and its
 * markdown renderer) into a pure module.
 */
import type { ChatMessage } from '../../types'

const SINGLE_PREFIX = '[Subagent completion event]'
const BATCH_PREFIX = '[Subagent batch completion event]'

/** Key under `message.meta` where the gateway stamps the structured header
 *  facts. Mirrors `SUBAGENT_COMPLETION_META_KEY` in src/kiro_crew/constants.py —
 *  the two names are one wire contract and must stay in lockstep. */
const META_KEY = 'subagentCompletion'

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

const OUTCOMES: ReadonlySet<string> = new Set<SubagentOutcome>(['ok', 'failed', 'stopped', 'interrupted'])
const isInt = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const isStr = (v: unknown): v is string => typeof v === 'string'

/**
 * Reconstruct a parsed completion from the structured `meta.subagentCompletion`
 * the gateway stamps at composition time, or null when the meta is absent or
 * malformed (the caller then falls back to the header regexes).
 *
 * Only the fragile part — the header FACTS (outcome, tallies, indices, ids) —
 * comes from meta. The `body` still comes from the same structural blank-line
 * split the regex path uses, because that boundary is not prose: it is where
 * the gateway joins its machine-facing header block to the payload, and reusing
 * it keeps orphan/timeout result-path lines intact without re-parsing them.
 *
 * Every field is type-checked; a single wrong type collapses the whole parse to
 * null rather than rendering a card with a NaN tally or an undefined outcome. A
 * tampered or version-skewed meta therefore degrades to the legacy prose path,
 * never to a broken card.
 */
function fromMeta(content: string, meta: Record<string, unknown> | undefined): ParsedSubagentCompletion | null {
  const raw = meta?.[META_KEY]
  if (!raw || typeof raw !== 'object') return null
  const d = raw as Record<string, unknown>

  if (d.kind === 'single' && content.startsWith(SINGLE_PREFIX)) {
    if (!isStr(d.agentId) || !d.agentId) return null
    if (!isStr(d.outcome) || !OUTCOMES.has(d.outcome)) return null
    const { body } = splitAgentHeadBody(content)
    // Words beside the glyph (orphan/timeout explanation) live in meta.note; on
    // the ordinary path it is empty. Same redundant-status guard as the regex
    // path so a status word the chip already renders is not repeated in-body.
    const note = isStr(d.note) ? d.note.trim() : ''
    const keptNote = note && !REDUNDANT_STATUS_RE.test(note) ? note : ''
    return {
      kind: 'single',
      agentId: d.agentId,
      agentName: isStr(d.agentName) ? d.agentName : '',
      outcome: d.outcome as SubagentOutcome,
      task: isStr(d.task) ? d.task.trim() : '',
      body: keptNote ? [keptNote, body].filter(Boolean).join('\n\n') : body,
    }
  }

  if (d.kind === 'batch' && content.startsWith(BATCH_PREFIX)) {
    if (!isInt(d.chunk) || !isInt(d.chunks) || !isInt(d.total)) return null
    if (typeof d.final !== 'boolean') return null
    const { body } = splitHeadBody(content)
    if (d.final) {
      if (!isInt(d.ok) || !isInt(d.failed) || !isInt(d.stopped)) return null
      return {
        kind: 'batch',
        chunk: d.chunk,
        chunks: d.chunks,
        final: true,
        ok: d.ok,
        failed: d.failed,
        stopped: d.stopped,
        total: d.total,
        delivered: d.total,
        running: 0,
        body,
      }
    }
    if (!isInt(d.delivered) || !isInt(d.running)) return null
    return {
      kind: 'batch',
      chunk: d.chunk,
      chunks: d.chunks,
      final: false,
      ok: 0,
      failed: 0,
      stopped: 0,
      total: d.total,
      delivered: d.delivered,
      running: d.running,
      body,
    }
  }

  return null
}

/** Parse a completion event, or null when neither the structured meta nor the
 *  header matches the expected shape (the caller then falls back to normal
 *  rendering). `meta` is read FIRST; the regexes below are a legacy fallback for
 *  rows persisted before the gateway stamped the structured fields. */
export function parseSubagentCompletion(
  content: string,
  meta?: Record<string, unknown>,
): ParsedSubagentCompletion | null {
  const viaMeta = fromMeta(content, meta)
  if (viaMeta) return viaMeta
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

/** Cached `parseSubagentCompletion` for a message. Reads the structured
 *  `message.meta` first (see `fromMeta`) and falls back to the header regexes.
 *  Cache identity is the content string: a message's `meta` is set once at
 *  append and never mutated in place, so it does not need to key the cache. */
export function parseSubagentCompletionMessage(message: ChatMessage): ParsedSubagentCompletion | null {
  const content = message.content || ''
  const cached = parseCache.get(message)
  if (cached && cached.content === content) return cached.parsed
  const parsed = parseSubagentCompletion(content, message.meta)
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
