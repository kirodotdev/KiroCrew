import { useState, memo } from 'react'
import { MessageSquare } from 'lucide-react'

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
  /** Unblock the agent with no answer. Omitted for legacy cards, which have
   *  nothing blocked on them and so have nothing to dismiss. */
  onDismiss?: () => void
  /** True while a submission is in flight: both controls lock so a second
   *  click cannot produce a duplicate resolution or a duplicate chat turn. */
  busy?: boolean
}

function QuestionCard({ questions, onSubmit, onDismiss, busy = false }: QuestionCardProps) {
  const [selections, setSelections] = useState<Record<number, Set<string>>>({})
  const [customInputs, setCustomInputs] = useState<Record<number, string>>({})

  const toggleOption = (qIdx: number, label: string, multi: boolean) => {
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
  }

  const handleSubmit = () => {
    const answers: Record<string, string> = {}
    questions.forEach((q, i) => {
      const selected = selections[i]
      const custom = customInputs[i]?.trim()
      if (custom) {
        answers[q.question] = custom
      } else if (selected?.size) {
        answers[q.question] = [...selected].join(', ')
      }
    })
    onSubmit(answers)
  }

  /* Every question must be answered before Submit unlocks. The answer map is
     keyed by question text, so a partial submit resumes the blocked agent with
     a map missing entries it asked for -- it cannot tell "unanswered" from
     "never asked" and proceeds on incomplete input. A multi-question card is
     one atomic ask, so the gate is `every`, not `some`. */
  const isAnswered = (i: number) => (selections[i]?.size ?? 0) > 0 || !!customInputs[i]?.trim()
  const allAnswered = questions.every((_, i) => isAnswered(i))

  return (
    <div className="border border-accent/30 rounded-xl bg-card shadow-md overflow-hidden animate-scale-in">
      {questions.map((q, qIdx) => (
        <div key={qIdx} className={`p-4 ${qIdx > 0 ? 'border-t border-border' : ''}`}>
          <div className="flex items-center gap-2 mb-2.5">
            {q.header && <span className="text-[11px] font-semibold uppercase tracking-wider text-accent bg-accent-subtle px-2 py-0.5 rounded">{q.header}</span>}
            <span className="text-[13px] font-medium text-text">{q.question}</span>
          </div>
          <div className="flex flex-col gap-1.5">
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
            aria-label="Custom answer"
            placeholder="Or type a custom answer..."
            maxLength={2000}
            value={customInputs[qIdx] || ''}
            onChange={e => {
              setCustomInputs(prev => ({ ...prev, [qIdx]: e.target.value }))
              setSelections(prev => ({ ...prev, [qIdx]: new Set() }))
            }}
            onKeyDown={e => { if (e.key === 'Enter' && allAnswered && !busy) handleSubmit() }}
            className="mt-2 w-full px-3 py-2 rounded-lg border border-border bg-bg text-text text-[13px] placeholder:text-muted focus:border-accent focus:outline-none"
          />
        </div>
      ))}
      <div className="px-4 py-3 border-t border-border flex justify-end items-center gap-2">
        {onDismiss && (
          <button
            onClick={onDismiss}
            disabled={busy}
            aria-label="Dismiss question without answering"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-[13px] font-medium cursor-pointer transition-all disabled:opacity-30 disabled:cursor-not-allowed bg-transparent text-muted hover:text-text border border-border"
          >
            Dismiss
          </button>
        )}
        <button
          onClick={handleSubmit}
          disabled={!allAnswered || busy}
          className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-md text-[13px] font-medium cursor-pointer transition-all disabled:opacity-30 disabled:cursor-not-allowed bg-accent text-accent-fg hover:bg-accent-hover border-none"
        >
          <MessageSquare size={14} /> Submit
        </button>
      </div>
    </div>
  )
}

export default memo(QuestionCard)
