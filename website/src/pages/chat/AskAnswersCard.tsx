import { useState } from 'react'
import { ChevronDown, MessageCircleQuestion } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { parseAskAnswers, type AskAnswer } from '../../utils/askQuestionAnswers'

/** Parses the tool output inside the lazy chunk and renders nothing when it is not an answered result. */
export function AskAnswersResult({ output, toolCallId }: { output: string; toolCallId?: string }) {
  const pairs = parseAskAnswers(output)
  return pairs ? <AskAnswersCard pairs={pairs} toolCallId={toolCallId} /> : null
}

/** Expansions survive a virtualizer unmount, keyed by tool_call_id, the same
 *  module-scope pattern ToolCallLine uses for its diff cards. */
const openedAnswerCards = new Set<string>()

/**
 * The record of what the user answered on a blocking `ask_question` card.
 *
 * The answers travel back to the agent as the tool's RESULT, so there is no
 * user chat bubble carrying them. This card is where the reader sees them: a
 * chip naming the count, which opens to each question and the answer given.
 * It is display-only and built from the persisted tool output, so it survives a
 * reload and adds nothing the model reads.
 */
export default function AskAnswersCard({ pairs, toolCallId }: { pairs: AskAnswer[]; toolCallId?: string }) {
  const [open, setOpen] = useState(() => !!(toolCallId && openedAnswerCards.has(toolCallId)))
  const toggle = () => {
    setOpen(prev => {
      const next = !prev
      if (toolCallId) {
        if (next) openedAnswerCards.add(toolCallId)
        else openedAnswerCards.delete(toolCallId)
      }
      return next
    })
  }
  return (
    <div className="ml-3" role="presentation" onClick={e => e.stopPropagation()} data-testid="ask-answers">
      <button
        type="button"
        className={`mt-1 inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md border text-[12px] leading-5 hover:text-text hover:border-border-strong cursor-pointer transition-colors focus-visible:ring-2 focus-visible:ring-accent/50 focus-visible:outline-hidden ${open ? 'text-text border-border-strong bg-bg-hover' : 'text-muted border-border bg-bg-elevated'}`}
        aria-expanded={open}
        data-testid="ask-answers-chip"
        onClick={toggle}
      >
        <MessageCircleQuestion size={12} className="shrink-0" aria-hidden />
        {/* One count-aware string ("You answered 3 questions"), so each locale places
            the number and chooses the plural form itself. */}
        <span>{i18nT('pages.chat.toolCallLine.ask_answered', { count: pairs.length })}</span>
        <ChevronDown size={12} aria-hidden className={`shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <dl className="mt-1.5 rounded-lg border border-border bg-bg-elevated text-text px-3 py-2 flex flex-col gap-2 text-[13px] max-w-[640px]">
          {pairs.map((pair, i) => (
            <div key={i} className="min-w-0">
              <dt className="text-muted text-[12px] break-words">{pair.question}</dt>
              <dd className="m-0 font-medium break-words whitespace-pre-wrap">{pair.answer}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  )
}
