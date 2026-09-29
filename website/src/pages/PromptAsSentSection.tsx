import { useEffect, useMemo, useState } from 'react'
import { Check, ChevronDown, ChevronRight, Copy } from 'lucide-react'

import ErrorNotice from '../components/ErrorNotice'
import { Btn } from '../components/ui'
import { fmtNumber, fmtTime } from '../i18n/format'
import { i18nT } from '../i18n/t'
import { copyToClipboard } from '../utils/clipboard'
import { CATEGORY_FILL } from './contextSourceColors'

/** The backend's label for the user's own text (`context_blocks.USER_LABEL`). */
const USER_LABEL = 'your_message'

/** One recorded prompt, as GET /api/telemetry/prompt-trace returns it. */
export interface PromptSpan {
  start: number
  end: number
  label: string
}

export interface PromptRecord {
  ts: string
  /** Length of the prompt as sent, before any cut. */
  chars: number
  /** Length before the receipt substitution: the size the usage row was measured
   *  from. Differs from `chars` on a member session whose essentials receipt
   *  was already acknowledged (the envelope is dropped on the wire). */
  assembled_chars: number
  /** The prompt text, cut at the backend's per-turn cap when `truncated`. */
  text: string
  truncated: boolean
  /** True when the backend masked a credential or suspicious link in `text` before serving it. */
  redacted: boolean
  spans: PromptSpan[]
  /** The turn number the assembler composed this prompt for — the same number the
   *  usage row carries as `turn` — or null/absent for a prompt sent outside the
   *  dashboard runner, which is then joined by time. */
  turn?: number | null
}


export interface PromptTrace {
  slot: string
  turns: PromptRecord[]
  /** Prompts this session's own cap pushed out; said so a short list is not mistaken for a full one. */
  dropped: number
  /** The global budget evicted this session whole; empty `turns` is then not "never recorded". */
  evicted: boolean
}

/** One served span as a display row. `start`/`end` are in whatever unit the
 *  span came from; `chars` is always code points, the unit every other count on
 *  the panel uses. The backend coalesces adjacent same-label spans before
 *  serving (`prompt_trace._coalesce`), so one span is one row. */
export interface PromptSegment {
  label: string
  start: number
  end: number
  chars: number
}

/**
 * Re-express spans given in CODE POINTS (Python string indices, which is what
 * the backend's scan counts) as UTF-16 indices `String.prototype.slice` uses.
 * The two agree until the first character outside the Basic Multilingual Plane
 * (an emoji, some CJK extension characters), after which every JS index is
 * ahead of the code-point index by one per such character seen so far; slicing
 * with the raw offsets would then start every later block one or more
 * characters late and split a surrogate pair at the boundary. One pass over the
 * text, no per-span rescans.
 */
export function spansToUtf16(text: string, spans: readonly PromptSpan[]): PromptSpan[] {
  if (spans.length === 0) return []
  // Sorted, distinct code-point offsets we need answers for.
  const wanted = Array.from(new Set(spans.flatMap(s => [s.start, s.end]))).sort((a, b) => a - b)
  const map = new Map<number, number>()
  let cp = 0
  let u16 = 0
  let w = 0
  while (w < wanted.length && wanted[w] <= 0) map.set(wanted[w++], 0)
  for (const ch of text) {
    cp += 1
    u16 += ch.length
    while (w < wanted.length && wanted[w] === cp) map.set(wanted[w++], u16)
  }
  // Offsets past the end (a defensive backend row) clamp to the text's length.
  while (w < wanted.length) map.set(wanted[w++], text.length)
  return spans.map(s => ({ start: map.get(s.start) ?? 0, end: map.get(s.end) ?? text.length, label: s.label }))
}

/**
 * Segments sized in CODE POINTS (so `chars` matches every other count on the
 * panel), then re-addressed in UTF-16 for `slice`. One span, one segment: the
 * backend already coalesced adjacent same-label spans, so no merge happens here
 * (two spellings of one rule drifted once; the server's is the only one now).
 * Empty spans are dropped: a zero-width row has nothing to open.
 */
export function segmentsFor(text: string, spans: readonly PromptSpan[]): PromptSegment[] {
  const kept = spans.filter(s => s.end > s.start)
  const sliceable = spansToUtf16(text, kept)
  return kept.map((s, i) => ({ label: s.label, chars: s.end - s.start, start: sliceable[i].start, end: sliceable[i].end }))
}

/** An ISO timestamp as an instant; NaN when it does not parse. */
const instant = (ts: string): number => Date.parse(ts)

/**
 * The prompt record that produced a context-trace turn: the one carrying the
 * turn's number.
 *
 * Both sides are stamped by the same assembler with the same turn number
 * (`context/composed`'s `turn`; `announce_user_span(turn=)`), so the join is by
 * id — never by time. Of several records under one number (a turn recomposed or
 * retried writes one per attempt) the one stamped nearest at or after the row is
 * that attempt's own: the row is written as the prompt is assembled, the record
 * once the transport has taken it, a moment later. `null` when none carries the
 * number: the ring holds only the newest few turns and empties on a restart, a
 * refused prompt left no record, and a row without a number has nothing to join.
 * A prompt recorded outside the dashboard runner (the messaging dispatcher) has no
 * number and no row, so it is never shown here — the tab draws rows only for the
 * runner's own compositions.
 */
export function promptForTurn(prompts: readonly PromptRecord[], turn: { ts: string; turn?: number | null }): PromptRecord | null {
  if (typeof turn.turn !== 'number') return null
  const turnAt = instant(turn.ts)
  let best: PromptRecord | null = null
  let bestScore = Number.POSITIVE_INFINITY
  for (const p of prompts) {
    if (p.turn !== turn.turn) continue
    const at = instant(p.ts)
    // A record stamped after the row is its own attempt's; one stamped before is
    // an earlier attempt's and ranks behind every one stamped after.
    const score = Number.isNaN(at) || Number.isNaN(turnAt) ? 0 : at >= turnAt ? at - turnAt : 1e15 + (turnAt - at)
    if (!best || score < bestScore) {
      best = p
      bestScore = score
    }
  }
  return best
}

const fmtN = (n: number): string => fmtNumber(Math.round(n))

function SegmentRow({
  seg,
  text,
  fill,
  name,
  group,
}: {
  seg: PromptSegment
  text: string
  fill: string
  name: string
  /** The summary row above this block belongs to; omitted when it IS that row's name. */
  group?: string
}) {
  const [open, setOpen] = useState(false)
  const Chevron = open ? ChevronDown : ChevronRight
  return (
    <div className="border-b border-border last:border-b-0" data-prompt-segment={seg.label}>
      {/* The shared Btn (press feedback, disabled state, focus ring), restyled
          as a full-width disclosure row: no box of its own — the row's border
          below is the separator — and the hover reads on the label, not a fill. */}
      <Btn
        type="button"
        className="w-full flex justify-between gap-3 py-2 px-0 text-left border-0 rounded text-[12px] hover:bg-transparent hover:text-text-strong focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
      >
        <span className="flex items-center gap-2 min-w-0">
          <Chevron size={14} className="lucide-inline shrink-0 text-muted" aria-hidden="true" />
          <i className="w-2.5 h-2.5 rounded-[2px] shrink-0" style={{ background: fill }} aria-hidden="true" />
          {/* Wraps rather than truncates: the message-wrapper row's suffix ends
              in the arithmetic that places the message ("54 of its 96
              characters"), and an ellipsis at panel width cut exactly that off. */}
          <span className="min-w-0 break-words">
            {group ? <span className="text-muted">{group} · </span> : null}
            {name}
          </span>
        </span>
        <span className="font-mono text-[11px] text-muted tabular-nums shrink-0">{fmtN(seg.chars)}</span>
      </Btn>
      {open ? (
        <pre className="m-0 mb-2 p-2.5 max-h-60 overflow-auto rounded-md border border-border bg-[var(--bg)] text-[11px] leading-[1.5] text-text whitespace-pre-wrap break-words font-mono">
          {text.slice(seg.start, seg.end)}
        </pre>
      ) : null}
    </div>
  )
}

function CopyAllButton({ text, onFailed }: { text: string; onFailed: (failed: boolean) => void }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const id = window.setTimeout(() => setCopied(false), 1500)
    return () => window.clearTimeout(id)
  }, [copied])
  const Icon = copied ? Check : Copy
  return (
    <Btn
      type="button"
      className="text-[12px] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]"
      onClick={() => {
        // The shared helper tries the async clipboard and falls back to the
        // execCommand path a plain-HTTP remote gateway still has. A copy that
        // reached neither is an error the section shows as an ErrorNotice; the
        // button only ever says what succeeded.
        void copyToClipboard(text).then(ok => {
          onFailed(!ok)
          setCopied(ok)
        })
      }}
    >
      <Icon size={13} className="lucide-inline" aria-hidden="true" />
      {copied ? i18nT('pages.contextBreakdown.prompt_copied') : i18nT('pages.contextBreakdown.prompt_copy')}
    </Btn>
  )
}

/**
 * The exact text one turn handed the agent, under the turn's size breakdown.
 *
 * Developer-mode only by placement: the Context tab it lives in is itself gated
 * on Developer Mode. Reads nothing itself — the tab fetches the prompt trace
 * and the card passes the matched record (or `null`) down, so this stays a
 * pure view: a segment bar in reading order, then one disclosure row per block
 * with the block's raw text behind it.
 */
/** Whether the user's message has a row of its own in these segments. It does
 *  when the backend re-found the span the assembler announced for the turn;
 *  a prompt recorded without one is scanned uncarved and the message then sits
 *  inside the block that physically holds it. */
export const hasUserRow = (segments: readonly PromptSegment[]): boolean => segments.some(s => s.label === USER_LABEL)

/**
 * Row names for the segments. A block that appears more than once in one
 * prompt (the reply-format rules sit at both ends by design) gets an ordinal,
 * so two rows with one label and different sizes do not read as a mistake.
 */
export function segmentNames(
  segments: readonly PromptSegment[],
  displayName: (label: string) => string,
  userChars?: number,
): string[] {
  const total = new Map<string, number>()
  for (const s of segments) total.set(s.label, (total.get(s.label) ?? 0) + 1)
  const seen = new Map<string, number>()
  // Only when the turn HAD a message to fold: a quick prompt (`/plain`, an
  // `@prompt` turn) sends no user text, so its record has no user span either,
  // and naming the header row "+ your message" there would claim a message the
  // turn does not have while the totals above credit the user 0 characters.
  const userInsideHeader = !!userChars && !hasUserRow(segments)
  return segments.map(s => {
    // When the user's text has no row of its own (see the section helper), the
    // row that carries it says so in its NAME ("Message wrapper (built-in) +
    // your message"): the row's larger total is then explained by what the row
    // is called, before any aside — a reader given the claim only as a trailing
    // clause still read 42 on carved turns and 96 here as "the same piece
    // measured two ways". The arithmetic against the row's own count ("54 of
    // its 96 characters are your message") follows as the aside when the usage
    // row knows the message's size. The counting statement ("counted once")
    // lives on the summary's "Your message" aside, not here: the two places
    // each say one thing, where saying both in both read as the same thing
    // listed twice.
    const base = displayName(s.label)
    let name = base
    if (userInsideHeader && s.label === 'request_header') {
      name = i18nT('pages.contextBreakdown.prompt_name_plus_your_message', { name: base })
      if (userChars) {
        name += ` · ${i18nT('pages.contextBreakdown.prompt_includes_your_message_n', {
          n: fmtN(userChars),
          total: fmtN(s.chars),
        })}`
      }
    }
    const n = total.get(s.label) ?? 1
    if (n < 2) return name
    const i = (seen.get(s.label) ?? 0) + 1
    seen.set(s.label, i)
    return `${name} · ${i18nT('pages.contextBreakdown.prompt_repeat', { i: fmtN(i), n: fmtN(n) })}`
  })
}

export function PromptAsSentSection({
  record,
  userChars,
  dropped = 0,
  evicted = false,
  categoryOf,
  categoryName,
  displayName,
}: {
  record: PromptRecord | null
  /** The selected turn's own count of the user's text, named on the request-header row when the record was not carved. */
  userChars?: number
  /** How many of this session's earlier prompts the ring has pushed out. */
  dropped?: number
  /** Whether the whole session was evicted to make room. */
  evicted?: boolean
  categoryOf: (label: string) => keyof typeof CATEGORY_FILL
  /** The summary row's name for a category, so a block row can name the row it belongs to. */
  categoryName: (cat: keyof typeof CATEGORY_FILL) => string
  displayName: (label: string) => string
}) {
  // Backend spans count code points; slice() counts UTF-16 units. Convert once
  // per record, not per row.
  const segments = useMemo(() => (record ? segmentsFor(record.text, record.spans) : []), [record])
  // Code points, the unit every count on the panel uses — never `text.length`,
  // which is UTF-16 units and overshoots on any emoji in the kept prefix.
  const keptChars = segments.reduce((acc, s) => acc + s.chars, 0)
  const names = segmentNames(segments, displayName, userChars)
  const [copyFailed, setCopyFailed] = useState(false)
  // The section is re-rendered in place when the selected turn changes (no
  // remount), so a copy failure belongs to the record it happened on: clear it
  // when the record changes rather than leaving it over another turn's text.
  useEffect(() => setCopyFailed(false), [record])
  return (
    <div className="mx-4 mt-4 pt-4 border-t border-border" data-testid="prompt-as-sent">
      <div className="flex items-center justify-between gap-3">
        <strong className="text-[14px] text-text-strong">{i18nT('pages.contextBreakdown.prompt_heading')}</strong>
        {record ? <CopyAllButton text={record.text} onFailed={setCopyFailed} /> : null}
      </div>
      {/* No hand-off: this section sits in the side panel beside a chat whose
          composer may hold an unsent draft; the hand-off navigates and would
          discard it. A refused clipboard write is recoverable in place anyway
          (open a segment, select the text, copy). */}
      {copyFailed ? (
        <ErrorNotice
          variant="inline"
          className="mt-2"
          message={i18nT('pages.contextBreakdown.prompt_copy_failed')}
          onDismiss={() => setCopyFailed(false)}
        />
      ) : null}
      {record ? (
        <>
          {/* One helper for both scans. Where the user's text sits when it was
              not carved is said on the request-header row's suffix and on the
              summary's "Your message" aside, the two places a reader looks for
              it; a third telling here read as repetition. */}
          <p className="m-0 mt-1 text-[12px] text-muted">{i18nT('pages.contextBreakdown.prompt_helper')}</p>
          {record.redacted ? (
            // The ring is verbatim; the read is the boundary. Said here so a
            // masked value is not read as what the model saw.
            <p className="m-0 mt-1 text-[11px] text-muted" data-testid="prompt-redacted">
              {i18nT('pages.contextBreakdown.prompt_redacted')}
            </p>
          ) : null}
          {record.truncated ? (
            // Said BEFORE the rows: the rows below sum to the kept text, the
            // heading names the whole prompt, and a reader who meets the rows
            // first files the difference under "could not tell".
            <p className="m-0 mt-1 text-[11px] text-muted" data-testid="prompt-truncated">
              {i18nT('pages.contextBreakdown.prompt_truncated', { n: fmtN(keptChars) })}
            </p>
          ) : null}
          {record.chars !== record.assembled_chars ? (
            // Said, not left for the reader to reconcile: the heading and the
            // totals count the assembled prompt, the rows sum to what went on
            // the wire. BEFORE the rows, like the truncated note: a reader who
            // met "Rules you set 1,220" above and a 408 row below filed the gap
            // under "could not tell" before reaching a note placed after them.
            <p className="m-0 mt-1 text-[11px] text-muted" data-testid="prompt-assembled-vs-sent">
              {i18nT('pages.contextBreakdown.prompt_assembled_vs_sent', {
                assembled: fmtN(record.assembled_chars),
                sent: fmtN(record.chars),
              })}
            </p>
          ) : null}
          {/* A thin bar with no label read as decoration, so the helper line
              above says what it is ("the bar shows them to scale") — in text a
              reader meets, where a hover-only title never fires for one who
              will not touch an unknown object. Still hidden from the tree: the
              rows carry the same information in accessible form. A 1px gap
              between the segments: two neighbouring purples (your message,
              rules you set) were not tellable apart in a bar this thin without
              one. */}
          <div
            className="flex gap-px h-2 rounded-sm overflow-hidden mt-2.5 mb-2"
            aria-hidden="true"
            data-testid="prompt-segment-bar"
          >
            {segments.map((seg, i) => (
              // Each segment names its piece on hover: two of the kinds are
              // neighbouring purples, and a reader could not tell the bands
              // apart by colour alone — the bar-level title names the bar, not
              // the pieces. The rows below carry the same names in the tree.
              <i
                key={`${seg.start}-${seg.label}`}
                className="min-w-[2px]"
                style={{ flexGrow: seg.chars, background: CATEGORY_FILL[categoryOf(seg.label)] }}
                title={`${names[i]} · ${fmtN(seg.chars)}`}
              />
            ))}
          </div>
          <div className="ml-1 pl-3 border-l-2 border-border">
            {segments.map((seg, i) => {
              const cat = categoryOf(seg.label)
              const group = categoryName(cat)
              return (
                <SegmentRow
                  key={`${seg.start}-${seg.label}`}
                  seg={seg}
                  text={record.text}
                  fill={CATEGORY_FILL[cat]}
                  name={names[i]}
                  // A block that is its category's only member carries the
                  // category's own name (your message), so the prefix would
                  // just repeat it.
                  group={group === displayName(seg.label) ? undefined : group}
                />
              )
            })}
          </div>
          <p className="m-0 mt-2 text-[11px] text-muted" data-testid="prompt-matched-at">
            {/* No size figure in the footer: the heading carries the turn's
                size when the join is right and nothing was substituted, the
                assembled-vs-sent note carries the sent size when something was,
                the truncated note carries the kept size — so a footer figure was
                always a repeat, and a repeat under a different word ("sent"
                beside "built" or "kept") read as a fourth quantity the reader
                had to reconcile. */}
            {i18nT('pages.contextBreakdown.prompt_matched_by_turn', { time: fmtTime(record.ts) })} ·{' '}
            {i18nT('pages.contextBreakdown.prompt_note')}
          </p>
        </>
      ) : (
        <p className="m-0 mt-2 text-[12px] text-muted">
          {evicted ? i18nT('pages.contextBreakdown.prompt_evicted') : i18nT('pages.contextBreakdown.prompt_none')}
        </p>
      )}
      {dropped > 0 ? (
        <p className="m-0 mt-1 text-[11px] text-muted" data-testid="prompt-dropped">
          {i18nT('pages.contextBreakdown.prompt_dropped', { count: dropped })}
        </p>
      ) : null}
    </div>
  )
}
