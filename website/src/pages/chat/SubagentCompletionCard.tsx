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
import { memo } from 'react'
import { Bot, CheckCircle2, AlertCircle, Square, ChevronDown, CircleDashed } from 'lucide-react'
import { PanelRightSolid } from '../../components/icons/panels'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import type { ChatMessage } from '../../types'

import { i18nT } from '../../i18n/t'
import { useRowDisclosure } from './rowDisclosure'
import {
  parseSubagentCompletionMessage,
  type ParsedSubagentCompletion,
  type SubagentOutcome,
} from './subagentCompletion'

function outcomeLabel(outcome: SubagentOutcome): string {
  if (outcome === 'failed') return i18nT('pages.chat.subagentCompletionCard.failed')
  if (outcome === 'stopped') return i18nT('pages.chat.subagentCompletionCard.stopped')
  if (outcome === 'interrupted') return i18nT('pages.chat.subagentCompletionCard.interrupted')
  return i18nT('pages.chat.subagentCompletionCard.completed')
}

/** Headline for the card: what happened, in the user's language. */
function headline(parsed: ParsedSubagentCompletion): string {
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

const CHIP = 'shrink-0 inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded border'

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
  disclosureKey,
  onOpenPanel,
}: {
  message: ChatMessage
  onFileOpen?: (path: string, opts?: { line?: number }) => void
  onFolderOpen?: (path: string) => void
  disclosureKey?: string
  /** Opens the Subagents side panel. Omitted by hosts that have no side panel
   *  (the embed SDK), which then render the card without the button. */
  onOpenPanel?: (parsed: ParsedSubagentCompletion) => void
}) {
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
  if (!parsed) return null

  const stopped = parsed.kind === 'single' && parsed.outcome === 'stopped'
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

  return (
    <div className="px-5 mx-auto w-full py-0.5" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div
        className="rounded-md bg-accent/10 border border-accent/20 overflow-hidden"
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
          <span className="truncate text-[13px] font-medium text-text-strong">{headline(parsed)}</span>
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
          {parsed.kind === 'single' ? (
            <span className="text-[10px] text-muted font-mono truncate hidden sm:inline">
              {parsed.agentId}
            </span>
          ) : parsed.chunks > 1 ? (
            // A one-chunk wave's "1/1" is noise; a multi-chunk one tells the
            // reader this card is a slice of a bigger wave, which the headline
            // alone does not. The label is spelled out rather than left as a bare
            // fraction: beside "10 of 18 results delivered" a second, smaller
            // "1/2" reads as a competing ratio, and a tooltip-only explanation is
            // invisible to touch and keyboard.
            <span className="text-[10px] text-muted truncate hidden sm:inline">
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
                className="pi-morph flex items-center gap-1 text-[11px] text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer px-1.5 py-1 rounded hover:bg-accent/10 transition-colors"
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
                className="flex items-center gap-1 text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer px-1.5 py-1 rounded hover:bg-bg-hover transition-colors"
              >
                {detailsLabel}
                <ChevronDown size={13} className={`transition-transform ${expanded ? 'rotate-180' : ''}`} />
              </button>
            )}
          </div>
        </div>
        {expanded && parsed.body && (
          <div className="px-3 pb-2 pt-1 border-t border-accent/10">
            {/* softBreaks: the payload is machine-composed plain text whose line
                structure carries meaning (one line per agent, an indented result
                path under it). Without hard breaks CommonMark collapses the
                digest into a single run-on paragraph. */}
            <MarkdownRenderer
              content={parsed.kind === 'batch' ? legibleDigest(parsed.body) : parsed.body}
              onFileOpen={onFileOpen}
              onFolderOpen={onFolderOpen}
              softBreaks
            />
          </div>
        )}
      </div>
    </div>
  )
})

export default SubagentCompletionCard
