// One finding inside a Focus Report row's expanded detail.
//
// Mirrors sage_lib/report.py's `_finding_html`. Layout, and why it is this shape:
//
//   severity + dimension eyebrow
//   HEADLINE            ← the conclusion, the one line a scanner reads
//   file:line
//   ────────────────────
//   OBSERVATION | evidence…      ← label column + value column, per row
//   CONSEQUENCE | …
//   SUGGESTION  | …
//   ────────────────────
//   [x] select        [ Draft this finding ]
//
// The previous layout stacked observation / consequence / suggestion as three
// full-width prose paragraphs separated only by font size (13px vs 12.5px) and a
// `↳` glyph, which read as one dense block: you had to finish the first
// paragraph before knowing what the finding claimed. Separation now comes from
// the fixed label column and the row rules — structure, not type size — and the
// headline states the claim up front.
//
// Two other deliberate moves: the snippet is demoted from a standalone <pre> to
// an evidence line INSIDE the observation row (it is support for that claim, not
// a section of its own), and the selection checkbox has moved out of the header
// to sit next to the draft button, because ticking a finding and drafting it are
// the same decision and used to be separated by the whole card body.
//
// All model-authored PROSE (headline / observation / consequence / suggestion)
// renders through MarkdownRenderer; the `snippet` is code from a private diff and
// renders verbatim in a monospace block — NEVER through the markdown renderer, so
// it cannot be reinterpreted as markup.
import { Check, Loader2, MessageSquarePlus, Wrench } from 'lucide-react'
import { useId, type ReactNode } from 'react'
import MarkdownRenderer from '../../../components/MarkdownRenderer'
import type { Finding } from '../lib/types'

import { i18nT } from '../../../i18n/t'
/** Severity → visual treatment. Red is a blocking finding, yellow a should-fix.
 * Anything unset defaults to yellow (the softer of the two). Static class
 * strings so Tailwind keeps them. */
function severityVisual(severity?: Finding['severity']) {
  if (severity === 'red') {
    return {
      text: 'text-danger',
      border: 'border-l-danger',
      word: i18nT('apps.codeReviewSage.components.findingCard.severity_must_fix'),
    }
  }
  return {
    text: 'text-warn',
    border: 'border-l-warn',
    word: i18nT('apps.codeReviewSage.components.findingCard.severity_should_fix'),
  }
}

/** A finding's text field as a trimmed string, or `''` when it is anything else.
 *
 * These records are written by the review worker and read back from disk, so the
 * `string` in `Finding` is a claim and not a runtime guarantee. `?? ''` covers a
 * missing field but not a wrongly-typed one: a numeric value survives it and
 * throws on `.trim()`, and a planted object reaches React as an unrenderable
 * child. Both take the entire report view down, so every text field the card
 * renders passes through here and a malformed record loses one row instead. */
function asText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/** A finding's CODE field, type-checked but byte-exact — never trimmed.
 *
 * Same runtime-type guard as `asText`, deliberately without the trim: the
 * snippet is rendered in a `whitespace-pre-wrap` <pre>, so its leading
 * indentation is data. Trimming it would show the reviewed line at a nesting
 * depth it does not have, which is a quieter failure than a crash and a worse
 * one -- the evidence would look right while misrepresenting the code. */
function asCode(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** One label+value row. Rendered only when it has something to say, so a finding
 * missing a field collapses the row instead of leaving a labelled blank. */
function DetailRow({ label, value, accent = false, children }: {
  label: string
  value?: unknown
  /** The suggestion row is the actionable one, so its label carries the accent. */
  accent?: boolean
  /** Extra content under the value (the observation row's evidence line). */
  children?: ReactNode
}) {
  const text = asText(value)
  if (!text && !children) return null
  return (
    <div className="flex gap-3 border-t border-border px-3.5 py-2.5">
      <div
        className={`w-[84px] flex-shrink-0 pt-0.5 text-[10px] font-semibold uppercase tracking-wider ${
          accent ? 'text-accent' : 'text-muted'
        }`}
      >
        {label}
      </div>
      <div className="min-w-0 flex-1 text-[12.5px] leading-relaxed text-text">
        {text && <MarkdownRenderer content={text} />}
        {children}
      </div>
    </div>
  )
}

export default function FindingCard({
  finding, onPost, posted = false, posting = false,
  selectable = false, selected = false, onToggle, label = '',
  fixSelectable = false, fixSelected = false, onToggleFix, onFix,
}: {
  finding: Finding
  /** Post THIS finding as a single inline comment. Omitted when the run cannot
   *  post (no records left, or a review still running). */
  onPost?: () => void
  posted?: boolean
  posting?: boolean
  /** Batch selection: ticking several and posting them together puts ONE pending
   *  review on the pull request instead of one per comment. */
  selectable?: boolean
  selected?: boolean
  onToggle?: () => void
  /** Accessible name for the checkbox — the card's own text is long. */
  label?: string
  /** Fix selection is intentionally separate from the posting selection. */
  fixSelectable?: boolean
  fixSelected?: boolean
  onToggleFix?: () => void
  onFix?: () => void
 }) {
  const actionId = useId()
  const { text, border, word } = severityVisual(finding.severity)
  const location = [finding.file, finding.line != null && finding.line !== '' ? String(finding.line) : null]
    .filter(Boolean)
    .join(':')
  const headline = asText(finding.headline)
  const snippet = asCode(finding.snippet)
  const dimension = asText(finding.dimension)
  const eyebrow = dimension ? `${word} · ${dimension}` : word
  // A record predating the `headline` field has none, so the dimension eyebrow
  // carries the card alone and the observation stays the lead — old reviews keep
  // rendering rather than showing an empty heading.
  const showFixActions = fixSelectable && !posted && (Boolean(onFix) || Boolean(onToggleFix))
  const showActions = posted || Boolean(onPost) || (selectable && !posted) || showFixActions

  return (
    <div className={`rounded-lg border border-border bg-bg-elevated overflow-hidden border-l-[3px] ${border} my-2`}>
      <div className="px-3.5 pb-2 pt-2.5">
        <div className={`text-[10px] font-semibold uppercase tracking-wider ${text}`}>
          {eyebrow}
        </div>
        {headline && (
          <div className="mt-1 text-[13.5px] font-semibold leading-snug text-text-strong">
            <MarkdownRenderer content={headline} />
          </div>
        )}
        {location && (
          <div className="mt-0.5 truncate font-mono text-[10.5px] text-muted" title={location}>
            {location}
          </div>
        )}
      </div>

      <DetailRow
        label={i18nT('apps.codeReviewSage.components.findingCard.observation')}
        value={finding.observation}
      >
        {snippet.trim() && (
          // Code from a private diff — rendered verbatim in a <pre>, deliberately
          // NOT through MarkdownRenderer (see file header). It stays a <pre>
          // rather than a styled div: it is preformatted text, and the element
          // choice is what carries that to assistive tech and to selection.
          <div className="mt-1.5 flex items-baseline gap-2 overflow-auto rounded border border-border bg-bg px-2 py-1">
            <span className="flex-shrink-0 font-mono text-[10.5px] text-muted">
              {i18nT('apps.codeReviewSage.components.findingCard.evidence')}
            </span>
            <pre className="min-w-0 whitespace-pre-wrap font-mono text-[10.5px] leading-relaxed">
              {snippet}
            </pre>
          </div>
        )}
      </DetailRow>
      <DetailRow
        label={i18nT('apps.codeReviewSage.components.findingCard.consequence')}
        value={finding.consequence}
      />
      <DetailRow
        label={i18nT('apps.codeReviewSage.components.findingCard.suggestion')}
        value={finding.suggestion}
        accent
      />

      {/* Per-finding posting: you rarely agree with every finding, so sending
          them one at a time is the normal case rather than an escape hatch.
          Each post is its own pending review on the pull request. The checkbox
          lives here rather than in the header so that choosing a finding and
          drafting it read as one decision. */}
      {showActions && (
        <div className="flex items-center gap-2 border-t border-border-strong bg-bg-elevated px-3.5 py-2">
          {selectable && !posted && (
            <label htmlFor={`${actionId}-select`} className="flex items-center gap-2 text-[11.5px] text-muted cursor-pointer">
              <input
                id={`${actionId}-select`}
                type="checkbox"
                checked={selected}
                onChange={onToggle}
                aria-label={i18nT('apps.codeReviewSage.components.findingCard.select_to_post',
        { label: label || i18nT('apps.codeReviewSage.components.findingCard.this_finding') })}
                className="flex-shrink-0 accent-accent cursor-pointer"
              />
              {i18nT('apps.codeReviewSage.components.findingCard.select_to_draft_together')}
            </label>
          )}
          {fixSelectable && !posted && (
            <label htmlFor={`${actionId}-fix`} className="flex items-center gap-2 text-[11.5px] text-muted cursor-pointer">
              <input
                id={`${actionId}-fix`}
                type="checkbox"
                checked={fixSelected}
                onChange={onToggleFix}
                aria-label={i18nT('apps.codeReviewSage.components.findingCard.select_to_fix',
                  { label: label || i18nT('apps.codeReviewSage.components.findingCard.this_finding') })}
                className="flex-shrink-0 accent-accent cursor-pointer"
              />
              {i18nT('apps.codeReviewSage.components.findingCard.select_for_fix')}
            </label>
          )}
          <span className="flex-1" />
          {posted ? (
            <span className="inline-flex items-center gap-1.5 text-[11.5px] text-ok">
              <Check size={11} aria-hidden="true" /> {i18nT('apps.codeReviewSage.components.findingCard.posted_to_the_pull_request')}
            </span>
          ) : posting ? (
            <span className="inline-flex items-center gap-1.5 text-[11.5px] text-muted">
              <Loader2
                size={11}
                className="animate-spin motion-reduce:animate-none"
                aria-hidden="true"
              />
              {i18nT('apps.codeReviewSage.components.findingCard.posting')}
            </span>
          ) : (
            <>
              {onFix && (
                <button
                  type="button"
                  onClick={onFix}
                  aria-label={finding.file
                    ? i18nT('apps.codeReviewSage.components.findingCard.ask_to_fix_on',
                      { target: finding.file })
                    : i18nT('apps.codeReviewSage.components.findingCard.ask_to_fix')}
                  className="inline-flex items-center gap-1.5 rounded-md border border-accent bg-accent-subtle px-2 py-1 text-[11.5px] text-accent hover:bg-accent/20 cursor-pointer"
                >
                  <Wrench size={11} aria-hidden="true" />
                  {i18nT('apps.codeReviewSage.components.findingCard.ask_to_fix')}
                </button>
              )}
              {onPost && (
                <button
                  type="button"
                  onClick={onPost}
                  aria-label={finding.file
                    ? i18nT('apps.codeReviewSage.components.findingCard.post_finding_on',
                      { target: finding.file })
                    : i18nT('apps.codeReviewSage.components.findingCard.post_finding')}
                  className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 text-[11.5px] text-text hover:text-accent hover:border-accent cursor-pointer"
                >
                  <MessageSquarePlus size={11} aria-hidden="true" />
                  {i18nT('apps.codeReviewSage.components.findingCard.post_this_comment')}
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}
