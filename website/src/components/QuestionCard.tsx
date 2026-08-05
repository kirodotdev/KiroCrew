import { useState, memo } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { ChevronDown, ChevronRight, ChevronsDownUp, ChevronsUpDown, MessageSquare } from 'lucide-react'

import { i18nT } from '../i18n/t'
interface QuestionOption {
  label: string
  description?: string
}

interface Question {
  question: string
  header?: string
  options: QuestionOption[]
  multiSelect?: boolean
}

interface QuestionCardProps {
  questions: Question[]
  onSubmit: (answers: Record<string, string>) => void
  /** Unblock the agent with no answer, or — for a legacy card, where nothing is
   *  blocked — just take the card off screen. Always supplied by
   *  PendingQuestionCard: a card the user can neither answer nor remove sits on
   *  top of the composer forever. */
  onDismiss?: () => void
  /** True while a submission is in flight: both controls lock so a second
   *  click cannot produce a duplicate resolution or a duplicate chat turn. */
  busy?: boolean
}

function QuestionCard({ questions, onSubmit, onDismiss, busy = false }: QuestionCardProps) {
  const [selections, setSelections] = useState<Record<number, Set<string>>>({})
  const [customInputs, setCustomInputs] = useState<Record<number, string>>({})
  const reduceMotion = useReducedMotion()
  /* The fold is animated, not instant. Answering AUTO-folds the question, so an
     un-animated fold yanks the questions below it up under the pointer in the
     same frame as the click that caused it — the next click lands on whatever
     slid into place. Height is the animated property (opacity alone would leave
     the jump), and reduced motion collapses it to a short opacity settle. */
  const foldTransition = reduceMotion
    ? { duration: 0.12 }
    : { type: 'spring' as const, bounce: 0, duration: 0.28 }
  /* Which questions are folded shut. A 3-question card with four options each is
     taller than the viewport, so an un-foldable card buries the composer and the
     conversation above it. Answering also folds the question it answered, so a
     multi-question card walks DOWN towards Submit instead of growing past it. */
  const [collapsed, setCollapsed] = useState<Record<number, boolean>>({})
  /* All three state maps are keyed by question INDEX, which only holds while the
     question set does. PendingQuestionCard keys this component by `ask_id` — but
     a legacy (ask_id-less) card falls back to the slot key, so a second
     stateless card in the same slot does NOT remount and would inherit the
     previous card's folds and picks (index 0 of a different question).
     Compared on the whole serialized PAYLOAD, never on array identity and never
     on the prompts alone. Identity is wrong because a websocket reconnect
     re-dispatches the SAME still-pending card with a freshly parsed array
     (useWebSocket's syncPendingQuestions), and treating that as a new set would
     silently discard answers the user had already entered. Prompts alone are
     wrong because a card can reuse a prompt with DIFFERENT options, and a
     retained selection would then submit a label absent from the current card.
     The payload is small (a handful of questions, each with a handful of
     options, validated before broadcast), so serializing it per render is
     cheaper than the class of bug either shortcut admits. */
  const questionKey = JSON.stringify(questions)
  const [lastKey, setLastKey] = useState(questionKey)
  if (questionKey !== lastKey) {
    setLastKey(questionKey)
    setSelections({})
    setCustomInputs({})
    setCollapsed({})
  }

  const toggleCollapsed = (qIdx: number) =>
    setCollapsed(prev => ({ ...prev, [qIdx]: !prev[qIdx] }))

  const toggleOption = (qIdx: number, label: string, multi: boolean) => {
    const wasSelected = !!selections[qIdx]?.has(label)
    setSelections(prev => {
      const current = prev[qIdx] || new Set<string>()
      const next = new Set(current)
      if (multi) {
        if (next.has(label)) next.delete(label); else next.add(label)
      } else {
        next.clear()
        if (!current.has(label)) next.add(label)
      }
      return { ...prev, [qIdx]: next }
    })
    setCustomInputs(prev => ({ ...prev, [qIdx]: '' }))
    /* Auto-fold the question this pick just settled. Only for single-select — a
       multi-select is not finished after one click — only when the card holds
       more than one question, and never when the click DESELECTED (there is no
       answer to summarise and the user is still choosing). */
    if (!multi && !wasSelected && questions.length > 1) {
      setCollapsed(prev => ({ ...prev, [qIdx]: true }))
    }
  }

  /** The answer for question *i*: a typed custom answer wins over picks, mirroring
   *  the mutual exclusion the two inputs enforce. `''` when unanswered. */
  const answerOf = (i: number) => {
    const custom = customInputs[i]?.trim()
    if (custom) return custom
    const selected = selections[i]
    return selected?.size ? [...selected].join(', ') : ''
  }

  const handleSubmit = () => {
    const answers: Record<string, string> = {}
    questions.forEach((q, i) => {
      const answer = answerOf(i)
      if (answer) answers[q.question] = answer
    })
    onSubmit(answers)
  }

  /* Every question must be answered before Submit unlocks. The answer map is
     keyed by question text, so a partial submit resumes the blocked agent with
     a map missing entries it asked for -- it cannot tell "unanswered" from
     "never asked" and proceeds on incomplete input. A multi-question card is
     one atomic ask, so the gate is `every`, not `some`. */
  const isAnswered = (i: number) => !!answerOf(i)
  const allAnswered = questions.every((_, i) => isAnswered(i))
  const allCollapsed = questions.every((_, i) => collapsed[i])

  return (
    <div className="border border-accent/30 rounded-xl bg-card shadow-md overflow-hidden animate-scale-in">
      {questions.map((q, qIdx) => {
        const isCollapsed = !!collapsed[qIdx]
        const summary = answerOf(qIdx)
        return (
          <div key={qIdx} className={`px-4 py-2.5 ${qIdx > 0 ? 'border-t border-border' : ''}`}>
            <button
              type="button"
              onClick={() => toggleCollapsed(qIdx)}
              aria-expanded={!isCollapsed}
              /* No aria-label: it would REPLACE the accessible name, so every
                 folded row would announce identically ("Expand question") and
                 hide the question, the chosen answer and the unanswered cue.
                 The button's own content is the name; aria-expanded is the state. */
              className={`w-full flex gap-2 text-left bg-transparent border-none p-0 cursor-pointer ${isCollapsed ? 'items-center' : 'items-start'}`}
            >
              {isCollapsed
                ? <ChevronRight size={14} className="shrink-0 text-muted" />
                : <ChevronDown size={14} className="shrink-0 text-muted" />}
              {q.header && <span className="shrink-0 text-[11px] font-semibold uppercase tracking-wider text-accent bg-accent-subtle px-2 py-0.5 rounded">{q.header}</span>}
              {/* Folded rows are one line each, so a three-question card stays a
                  glanceable stack instead of three wrapped paragraphs. */}
              <span className={`flex-1 min-w-0 text-[13px] font-medium text-text ${isCollapsed ? 'truncate' : ''}`}>{q.question}</span>
              {/* Collapsed rows carry their own answer, so a folded card still
                  shows what will be submitted — and says so when it is the
                  reason Submit is still disabled. */}
              {isCollapsed && (
                <span className={`ml-auto pl-2 shrink-0 max-w-[45%] truncate text-[12px] ${summary ? 'text-accent' : 'text-muted'}`}>
                  {summary || i18nT('components.questionCard.not_answered_yet')}
                </span>
              )}
            </button>
            <AnimatePresence initial={false}>
              {!isCollapsed && (
                <motion.div
                  key="body"
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={foldTransition}
                  style={{ overflow: 'hidden' }}
                >
                  {/* The gap between the header and the options lives INSIDE the
                      animated box. As a margin on this element, or as extra row
                      padding, it would survive height:0 (border-box) and leave a
                      residual strip that jumps away when the animation ends. */}
                  <div className="pt-2.5 flex flex-col gap-1.5">
                  {q.options.map(opt => {
                    const isSelected = selections[qIdx]?.has(opt.label)
                    return (
                      <button
                        key={opt.label}
                        onClick={() => toggleOption(qIdx, opt.label, q.multiSelect ?? false)}
                        className={`text-left px-3 py-2 rounded-lg text-[13px] cursor-pointer transition-all border ${
                          isSelected
                            ? 'border-accent text-text bg-accent-subtle/60'
                            : 'border-border text-muted hover:text-text hover:border-accent/40 bg-bg'
                        }`}
                      >
                        <span className="font-medium">{opt.label}</span>
                        {opt.description && <span className="text-muted text-[12px] ml-2">{opt.description}</span>}
                      </button>
                    )
                  })}
                </div>
                <input
                  type="text"
                  aria-label={i18nT('components.questionCard.custom_answer')}
                  placeholder={i18nT('components.questionCard.or_type_a_custom_answer')}
                  maxLength={2000}
                  value={customInputs[qIdx] || ''}
                  onChange={e => {
                    setCustomInputs(prev => ({ ...prev, [qIdx]: e.target.value }))
                    setSelections(prev => ({ ...prev, [qIdx]: new Set() }))
                  }}
                  onKeyDown={e => { if (e.key === 'Enter' && allAnswered && !busy) handleSubmit() }}
                  className="mt-2 w-full px-3 py-2 rounded-lg border border-border bg-bg text-text text-[13px] placeholder:text-muted focus:border-accent focus:outline-none"
                />
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )
      })}
      <div className="px-4 py-3 border-t border-border flex justify-end items-center gap-2">
        {/* One click to get the whole card out of the way. Only for a card that
            actually stacks — on a single question the per-question chevron is
            the same gesture, so a second control would be noise. */}
        {questions.length > 1 && (
          <button
            type="button"
            onClick={() => setCollapsed(allCollapsed ? {} : Object.fromEntries(questions.map((_, i) => [i, true])))}
            className="mr-auto inline-flex items-center gap-1.5 px-2 py-1.5 rounded-md text-[12px] font-medium cursor-pointer transition-all bg-transparent text-muted hover:text-text border-none"
          >
            {allCollapsed
              ? <><ChevronsUpDown size={13} /> {i18nT('components.questionCard.expand_all')}</>
              : <><ChevronsDownUp size={13} /> {i18nT('components.questionCard.collapse_all')}</>}
          </button>
        )}
        {onDismiss && (
          <button
            onClick={onDismiss}
            disabled={busy}
            aria-label={i18nT('components.questionCard.dismiss_question_without_answering')}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium cursor-pointer transition-all disabled:opacity-30 disabled:cursor-not-allowed bg-transparent text-muted hover:text-text border border-border"
          >
            {i18nT('components.questionCard.dismiss')}
          </button>
        )}
        <button
          onClick={handleSubmit}
          disabled={!allAnswered || busy}
          className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-md text-[13px] font-medium cursor-pointer transition-all disabled:opacity-30 disabled:cursor-not-allowed bg-accent text-accent-fg hover:bg-accent-hover border-none"
        >
          <MessageSquare size={14} /> {i18nT('components.questionCard.submit')}
        </button>
      </div>
    </div>
  )
}

export default memo(QuestionCard)
