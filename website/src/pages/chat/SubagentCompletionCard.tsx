/**
 * SubagentCompletionCard — renders an injected sub-agent completion event as a
 * compact outcome row instead of the machine-facing prompt it actually is.
 *
 * The injected text (see subagentCompletion.ts for the shapes) is addressed to
 * the model: spawn-discipline instructions, per-agent result paths, and the full
 * output inline. Rendered as a chat bubble it is a wall of prompt prose. This
 * card states what happened — which agent, or how much of a wave landed — and
 * folds the payload behind a disclosure, with a button into the Subagents panel.
 *
 * Render-only: the underlying message content is untouched, so the parent agent
 * still receives the complete result as context.
 */
import { memo, useId } from 'react'
import { Bot, CheckCircle2, AlertCircle, Square, ChevronDown, CircleDashed } from 'lucide-react'
import { PanelRightSolid } from '../../components/icons/panels'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
import { useRowDisclosure } from './rowDisclosure'
import {
  parseSubagentCompletionMessage,
  isModelDowngrade,
  type ParsedSubagentCompletion,
  type SubagentOutcome,
} from './subagentCompletion'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { normalizeModelKey } from '../../lib/model'

function outcomeLabel(outcome: SubagentOutcome): string {
  if (outcome === 'failed') return i18nT('pages.chat.subagentCompletionCard.failed')
  if (outcome === 'stopped') return i18nT('pages.chat.subagentCompletionCard.stopped')
  if (outcome === 'interrupted') return i18nT('pages.chat.subagentCompletionCard.interrupted')
  return i18nT('pages.chat.subagentCompletionCard.completed')
}

/** Headline for the card: what happened, in the user's language. */
export function headline(parsed: ParsedSubagentCompletion): string {
  if (parsed.kind === 'single') {
    // The cap keeps one long task from pushing the chips and controls off the
    // row. CSS `truncate` cannot supply the cue here — it only fires when the
    // text overflows its box, and a 120-character slice usually fits — so the
    // ellipsis has to be added when the slice actually shortened the task.
    const task = sanitizeLlmOutput(parsed.task)
    if (task) return task.length > 120 ? `${task.slice(0, 120)}…` : task
    return i18nT('pages.chat.subagentCompletionCard.agent_id', { id: parsed.agentId })
  }
  // Only the final chunk knows the wave's outcome; earlier chunks report
  // progress, so claiming "finished" there would be false for the whole wave.
  // Neither states what is still running: a card is permanent scrollback, and a
  // live count read as fact months later is simply wrong. The ratio carries it —
  // "10 of 18 delivered" says what had landed without asserting a present tense.
  if (parsed.final) {
    return i18nT('pages.chat.subagentCompletionCard.n_of_n_subagents_finished', {
      done: parsed.total,
      total: parsed.total,
    })
  }
  return i18nT('pages.chat.subagentCompletionCard.n_of_n_results_delivered', {
    done: parsed.delivered,
    total: parsed.total,
  })
}

const CHIP = 'shrink-0 inline-flex items-center gap-1 text-[10px] leading-4 px-1.5 py-0.5 rounded border'

/**
 * Make a wave digest's per-agent outcomes readable without an emoji font.
 *
 * The gateway marks each digest row's outcome with an emoji, and on a host whose
 * fonts lack them (a plain Linux desktop, and the screenshot container) they
 * render as tofu boxes. A SUCCESS row is the case that actually loses
 * information: the gateway writes no status word beside its glyph, so the reader
 * cannot tell which agents succeeded. Substitute the word where the glyph stands
 * alone, and drop the glyph where the row already names the status.
 *
 * Display-only — the message content the model receives is untouched.
 */
function legibleDigest(body: string): string {
  return body
    .replace(
      /^(— `[^`\n]+`) ✅ /gm,
      (_match, head: string) => `${head} ${i18nT('pages.chat.subagentCompletionCard.completed')} · `,
    )
    .replace(/ [❌⏹] · /g, ' · ')
}

const SubagentCompletionCard = memo(function SubagentCompletionCard({
  message,
  onFileOpen,
  onFolderOpen,
  onSessionOpen,
  sessions,
  activeSession,
  disclosureKey,
  onOpenPanel,
}: {
  message: ChatMessage
  onFileOpen?: (path: string, opts?: { line?: number }) => void
  onFolderOpen?: (path: string) => void
  /** Session switching for a `/chat?sid=` link in the payload, same triple the
   *  assistant row passes. Omitted by hosts with no slot roster. */
  onSessionOpen?: (key: string) => void
  sessions?: ReadonlyMap<string, string>
  activeSession?: string
  disclosureKey?: string
  /** Opens the Subagents side panel. Omitted by hosts that have no side panel
   *  (the embed SDK), which then render the card without the button. */
  onOpenPanel?: (parsed: ParsedSubagentCompletion) => void
}) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const parsed = parseSubagentCompletionMessage(message)
  const failed = parsed !== null && (parsed.kind === 'single' ? parsed.outcome === 'failed' : parsed.failed > 0)
  // A restart orphan: the run was cut short but its result survived on disk, so
  // it warns rather than alarming (failure) or reassuring (success).
  const interrupted = parsed !== null && parsed.kind === 'single' && parsed.outcome === 'interrupted'
  // Anything that did not simply succeed opens expanded. The header can only say
  // THAT it failed or was cut short; the reason — an error, or where the orphaned
  // result was saved — is the reader's next question, and a digest already orders
  // failures first for the same reason. Successes stay folded: their payload is a
  // result path, not something to read.
  const [expanded, setExpanded] = useRowDisclosure(disclosureKey, failed || interrupted)
  // Names the expanded body's scroll region after the headline. useId keeps it
  // unique when a transcript renders many cards.
  const headlineId = useId()
  if (!parsed) return null

  const stopped = parsed.kind === 'single' && parsed.outcome === 'stopped'
  // The model the run actually served (issue #3582), shown as a chip on the
  // single-agent card. When the spawn pinned a model AND the served id differs,
  // it is a downgrade (routing/config/availability) — flag it so a model-pinned
  // review's real model is not silently misread.
  const resolvedModel = parsed.kind === 'single' ? parsed.resolvedModel : ''
  const requestedModel = parsed.kind === 'single' ? parsed.requestedModel : ''
  // Namespace-aware: a short alias and the canonical provider id name the same
  // model, so compare normalized (see isModelDowngrade) rather than raw !==,
  // which falsely flagged aliases and the "auto" sentinel (GPT review on #3582).
  const modelDowngraded = isModelDowngrade(requestedModel, resolvedModel)
  // A digest chunk that is not the wave's last one reports a PARTIAL delivery.
  // Neither a success tick nor an in-progress spinner is honest about it: the
  // first reads "wave done" while siblings are still running, and the second
  // asserts a live state that a permanent scrollback row cannot know is still
  // true. A muted incomplete glyph says only what stays true — some of the wave
  // landed here, not all of it — and the headline's ratio carries the rest.
  const partial = parsed.kind === 'batch' && !parsed.final
  const detailsLabel = expanded
    ? i18nT('pages.chat.subagentCompletionCard.hide_details')
    : i18nT('pages.chat.subagentCompletionCard.show_details')

  // Row geometry -- the px-4 gutter and the --mc-content-width clamp -- belongs to
  // the HOST row wrapper, never to this card. ChatPage wraps every renderMessage
  // result, and the shared registries wrap this card through ctx.row. Re-applying
  // it here nested one clamp inside another and inset the card by a second full
  // gutter, so it sat 20px right of every sibling row and 40px narrower.
  return (
    <div
      className="rounded-md bg-accent/10 ring-1 ring-inset forced-colors:border ring-accent/20 overflow-hidden"
      data-testid="subagent-completion-card"
    >
      <div className="flex items-center gap-2 px-3 py-2">
        <span className="shrink-0">
          {failed ? (
            <AlertCircle size={15} className="text-danger" />
          ) : interrupted ? (
            <AlertCircle size={15} className="text-warn" data-testid="glyph-interrupted" />
          ) : partial ? (
            <CircleDashed size={15} className="text-muted" data-testid="glyph-partial" />
          ) : stopped ? (
            <Square size={15} className="text-muted" />
          ) : (
            <CheckCircle2 size={15} className="text-ok" />
          )}
        </span>
        <Bot size={12} className="text-accent/70 shrink-0" aria-hidden />
        <span id={headlineId} className="truncate text-[13px] leading-5 font-medium text-text-strong">{headline(parsed)}</span>
        {parsed.kind === 'single' ? (
          <span
            className={`${CHIP} ${
              failed
                ? 'bg-danger-subtle border-danger/20 text-danger'
                : interrupted
                  ? 'bg-warn-subtle border-warn/20 text-warn'
                  : stopped
                    ? 'bg-muted/15 border-border text-muted'
                    : 'bg-ok-subtle border-ok/20 text-ok'
            }`}
          >
            {outcomeLabel(parsed.outcome)}
          </span>
        ) : (
          <>
            {parsed.ok > 0 && (
              <span className={`${CHIP} bg-ok-subtle border-ok/20 text-ok`} data-testid="chip-ok">
                <CheckCircle2 size={10} aria-hidden /> {parsed.ok}
              </span>
            )}
            {parsed.failed > 0 && (
              <span className={`${CHIP} bg-danger-subtle border-danger/20 text-danger`} data-testid="chip-failed">
                <AlertCircle size={10} aria-hidden /> {parsed.failed}
              </span>
            )}
            {parsed.stopped > 0 && (
              <span className={`${CHIP} bg-muted/15 border-border text-muted`} data-testid="chip-stopped">
                <Square size={10} aria-hidden /> {parsed.stopped}
              </span>
            )}
          </>
        )}
        {(resolvedModel || requestedModel) && (() => {
          const resolvedKnown = !!resolvedModel
          const displayModel = resolvedModel || requestedModel
          // Requested-only (model not yet resolved): render a muted chip only
          // for the 'auto' sentinel. For a concrete pinned id, render nothing —
          // the chip appears once the model resolves. See ActivityViewer.tsx for
          // the matching guard.
          if (!resolvedKnown && !modelDowngraded && normalizeModelKey(displayModel) !== 'auto') return null
          return (
            <code
              className={`${CHIP} font-mono max-w-[8rem] ${
                modelDowngraded
                  ? 'bg-warn-subtle border-warn/20 text-warn'
                  : resolvedKnown
                    ? 'bg-accent/10 border-accent/20 text-accent/80'
                    : 'bg-bg-hover border-border text-muted/60'
              }`}
              data-testid="subagent-completion-model"
              title={
                modelDowngraded
                  ? i18nT('pages.chat.activityViewer.model_downgraded', {
                      requested: requestedModel,
                      resolved: resolvedModel,
                    })
                  : resolvedKnown
                    ? i18nT('pages.chat.activityViewer.model_label', { model: resolvedModel })
                    : i18nT('pages.chat.activityViewer.model_effective', { model: displayModel })
              }
            >
              {modelDowngraded && <AlertCircle size={10} aria-hidden />}
              {/* Left-truncate: long ids share a provider prefix
                  (us.anthropic.claude-…), so clipping the END hides the one part
                  that says WHICH model. rtl+plaintext keeps the glyphs in logical
                  LTR order while the ellipsis falls on the left (UX review #3582). */}
              <span className="truncate inline-block max-w-full [direction:rtl] [unicode-bidi:plaintext] text-left align-bottom">{displayModel}</span>
            </code>
          )
        })()}
        {parsed.kind === 'single' ? (
          <span className="text-[10px] leading-4 text-muted font-mono truncate hidden sm:inline">
            {parsed.agentId}
          </span>
        ) : parsed.chunks > 1 ? (
          // A one-chunk wave's "1/1" is noise; a multi-chunk one tells the
          // reader this card is a slice of a bigger wave, which the headline
          // alone does not. The label is spelled out rather than left as a bare
          // fraction: beside "10 of 18 results delivered" a second, smaller
          // "1/2" reads as a competing ratio, and a tooltip-only explanation is
          // invisible to touch and keyboard.
          <span className="text-[10px] leading-4 text-muted truncate hidden sm:inline">
            {i18nT('pages.chat.subagentCompletionCard.digest_chunk_n_of_n', {
              chunk: parsed.chunk,
              chunks: parsed.chunks,
            })}
          </span>
        ) : null}
        <div className="ml-auto flex items-center gap-1 shrink-0">
          {onOpenPanel && (
            <button
              type="button"
              onClick={() => onOpenPanel(parsed)}
              title={i18nT('pages.chat.subagentCompletionCard.open_in_the_subagents_panel')}
              aria-label={i18nT('pages.chat.subagentCompletionCard.open_in_the_subagents_panel')}
              className="pi-morph flex items-center gap-1 text-[11px] leading-4 text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer px-1.5 py-1 rounded hover:bg-accent/10 transition-colors"
            >
              <PanelRightSolid size={13} />
              <span className="hidden sm:inline">{i18nT('pages.chat.subagentCompletionCard.panel')}</span>
            </button>
          )}
          {parsed.body && (
            <button
              type="button"
              onClick={() => setExpanded(e => !e)}
              aria-expanded={expanded}
              title={detailsLabel}
              className="flex items-center gap-1 text-[11px] leading-4 text-muted hover:text-text bg-transparent border-none cursor-pointer px-1.5 py-1 rounded hover:bg-bg-hover transition-colors"
            >
              {detailsLabel}
              <ChevronDown size={13} className={`transition-transform ${expanded ? 'rotate-180' : ''}`} />
            </button>
          )}
        </div>
      </div>
      {modelDowngraded && (
        // Visible (not hover-only) requested-vs-served text. The chip's tooltip
        // is invisible to touch / keyboard / screen-reader users, but the
        // requested-vs-served fact IS the audit point of this feature, so it is
        // rendered as persistent text here too (UX review #3582). role=status so
        // AT announces it; the amber matches the chip.
        <div
          className="flex items-start gap-1.5 px-3 py-1.5 border-t border-warn/20 bg-warn-subtle/50 text-[11px] leading-4 text-warn"
          role="status"
          data-testid="subagent-completion-downgrade"
        >
          <AlertCircle size={12} className="shrink-0 mt-0.5" aria-hidden />
          <span className="min-w-0 break-words">
            {i18nT('pages.chat.activityViewer.model_downgraded', {
              requested: requestedModel,
              resolved: resolvedModel,
            })}
          </span>
        </div>
      )}
      {expanded && parsed.body && (
        // max-h + overflow-y-auto: a wave digest grows one block per agent, so a
        // 7+-agent batch renders taller than the viewport. The body scrolls
        // internally past 24rem, so its height cannot displace the rows below.
        // overflow-x-hidden is explicit because a non-visible y-axis computes
        // x's `visible` to `auto`: without it this becomes a two-axis scroller.
        // Nothing legitimately overflows x — inline code breaks (index.css
        // word-break:break-all) and body text wraps (break-words).
        // tabIndex + region role: a scroll region with no focusable descendant
        // is unreachable to a keyboard, and a failure digest opens expanded, so
        // this is a scroller a keyboard user meets without asking for it.
        // The ring is INSET because the card root's overflow-hidden clips an
        // outward ring where the body is flush with the card (left/right/
        // bottom) — pre-fix, the only indicator was the UA :focus-visible
        // outline reduced to a hairline on the top edge alone (WCAG 2.4.7).
        <div
          className="px-3 pb-2 pt-1 border-t border-accent/10 max-h-[24rem] overflow-y-auto overflow-x-hidden focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
          data-testid="subagent-completion-body"
          role="region"
          aria-labelledby={headlineId}
          // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
          tabIndex={0}
        >
          {/* softBreaks: the payload is machine-composed plain text whose line
              structure carries meaning (one line per agent, an indented result
              path under it). Without hard breaks CommonMark collapses the
              digest into a single run-on paragraph. */}
          <MarkdownRenderer
            content={parsed.kind === 'batch' ? legibleDigest(parsed.body) : parsed.body}
            onFileOpen={onFileOpen}
            onFolderOpen={onFolderOpen}
            onSessionOpen={onSessionOpen}
            sessions={sessions}
            activeSession={activeSession}
            softBreaks
          />
        </div>
      )}
    </div>
  )
})

export default SubagentCompletionCard
