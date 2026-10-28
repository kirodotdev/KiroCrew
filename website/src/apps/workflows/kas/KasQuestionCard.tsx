/**
 * A step's question-tool ask (`/state` `questions`), answered in place: each
 * question with its options (one choice, or several with `multiSelect`), a
 * free-text field when `freeText` allows one, Send and Dismiss.
 *
 * A question Kiro Agent asks over `_kiro/userInput` (gap 13) also carries,
 * per option, `recommended` (pre-selected, with a "Recommended" badge) and
 * `subOptions` toggles, all on by default and shown under the picked option.
 * An option with sub-options is answered as `[label, ...picked sub-options]`,
 * which the backend turns into Kiro Agent's `"Title [a, b]"` answer text.
 *
 * Shown under the run's in-chat card and in the run detail, so a step that
 * asks the person something is answered where the run is read.
 */
import { useState } from 'react'
import { MessageCircleQuestionMark, Send } from 'lucide-react'

import type { KasQuestion, KasQuestionAnswers, KasQuestionItem } from '../../../api/client'
import { Btn } from '../../../components/ui'
import { i18nT } from '../../../i18n/t'
import { questionAnswered } from './kasModel'
import { useKasParityActions } from './useKasWorkflows'

/** One answer as `_kiro/userInput` describes it; every field past `label` is optional. */
export interface KasQuestionOption {
  label: string
  description?: string | null
  recommended?: boolean
  subOptionsLabel?: string | null
  subOptions?: { label: string; description?: string | null }[]
}

interface Draft {
  picked: string[]
  text: string
  /** Per option label, the sub-options switched OFF (all start on). */
  off?: Record<string, string[]>
}

function optionsOf(q: KasQuestionItem): KasQuestionOption[] {
  return (q.options ?? []) as KasQuestionOption[]
}

/** The draft a question opens with: its recommended answer picked (single choice only). */
export function initialDraft(q: KasQuestionItem): Draft {
  const recommended = q.multiSelect ? undefined : optionsOf(q).find(o => o.recommended)
  return { picked: recommended ? [recommended.label] : [], text: '' }
}

/** One question's answer from its draft: a label or the typed text; several labels (plus text) when multi;
 *  `[label, ...sub-options left on]` for a picked option that has sub-options. */
export function answerOf(q: KasQuestionItem, draft: Draft | undefined): string | string[] | undefined {
  if (!draft) return undefined
  const text = draft.text.trim()
  if (q.multiSelect) {
    const all = text ? [...draft.picked, text] : [...draft.picked]
    return all.length ? all : undefined
  }
  if (text) return text
  const label = draft.picked[0]
  if (label === undefined) return undefined
  const subs = optionsOf(q).find(o => o.label === label)?.subOptions ?? []
  if (subs.length === 0) return label
  const off = new Set(draft.off?.[label] ?? [])
  return [label, ...subs.map(s => s.label).filter(s => !off.has(s))]
}

function QuestionField({
  q,
  qIndex,
  cardKey,
  draft,
  onChange,
  disabled,
}: {
  q: KasQuestionItem
  qIndex: number
  cardKey: string
  draft: Draft
  onChange: (draft: Draft) => void
  disabled: boolean
}) {
  const name = `kas-q-${cardKey}-${qIndex}`
  const options = optionsOf(q)
  const toggle = (label: string, on: boolean) => {
    if (q.multiSelect) {
      onChange({ ...draft, picked: on ? [...draft.picked, label] : draft.picked.filter(l => l !== label) })
    } else {
      // One choice: picking an option replaces any typed answer.
      onChange({ picked: on ? [label] : [], text: '', off: draft.off })
    }
  }
  const toggleSub = (label: string, sub: string, on: boolean) => {
    const off = new Set(draft.off?.[label] ?? [])
    if (on) off.delete(sub)
    else off.add(sub)
    onChange({ ...draft, off: { ...(draft.off ?? {}), [label]: [...off] } })
  }
  const freeText = q.freeText || options.length === 0
  return (
    <fieldset className="m-0 p-0 border-none flex flex-col gap-1.5 min-w-0" data-testid="kas-question-item">
      <legend className="p-0 mb-1 text-[12.5px] font-medium text-text-strong break-words">{q.prompt}</legend>
      {q.multiSelect && options.length > 0 ? (
        <span className="text-[11px] text-muted">{i18nT('apps.workflows.kas.question_pick_any')}</span>
      ) : null}
      {options.map((option, i) => {
        const id = `${name}-o${i}`
        const checked = draft.picked.includes(option.label)
        const subs = !q.multiSelect && checked ? option.subOptions ?? [] : []
        const off = new Set(draft.off?.[option.label] ?? [])
        return (
          <div key={id} className="flex flex-col gap-1 min-w-0">
            <label htmlFor={id} className="flex items-start gap-2 text-[12.5px] text-text cursor-pointer">
              <input
                id={id}
                type={q.multiSelect ? 'checkbox' : 'radio'}
                name={name}
                className="mt-[3px]"
                checked={checked}
                disabled={disabled}
                aria-labelledby={`${id}-label`}
                aria-describedby={option.description ? `${id}-desc` : undefined}
                onChange={e => toggle(option.label, e.target.checked)}
              />
              <span className="min-w-0">
                <span id={`${id}-label`} className="block">
                  {option.label}
                  {option.recommended ? (
                    <span className="ml-1.5 align-middle rounded border border-accent/50 px-1 text-[10.5px] text-accent" data-testid="kas-question-recommended">
                      {i18nT('apps.workflows.kas.question_recommended')}
                    </span>
                  ) : null}
                </span>
                {option.description ? (
                  <span id={`${id}-desc`} className="block text-[11.5px] text-muted">
                    {option.description}
                  </span>
                ) : null}
              </span>
            </label>
            {subs.length > 0 ? (
              <div
                role="group"
                aria-label={option.subOptionsLabel || i18nT('apps.workflows.kas.question_sub_options', { option: option.label })}
                className="ml-6 flex flex-col gap-1"
                data-testid="kas-question-sub-options"
              >
                {option.subOptionsLabel ? <span className="text-[11.5px] text-muted">{option.subOptionsLabel}</span> : null}
                {subs.map((sub, j) => {
                  const subId = `${id}-s${j}`
                  return (
                    <label key={subId} htmlFor={subId} className="flex items-start gap-2 text-[12px] text-text cursor-pointer">
                      <input
                        id={subId}
                        type="checkbox"
                        className="mt-[3px]"
                        checked={!off.has(sub.label)}
                        disabled={disabled}
                        aria-labelledby={`${subId}-label`}
                        aria-describedby={sub.description ? `${subId}-desc` : undefined}
                        onChange={e => toggleSub(option.label, sub.label, e.target.checked)}
                      />
                      <span className="min-w-0">
                        <span id={`${subId}-label`} className="block">{sub.label}</span>
                        {sub.description ? (
                          <span id={`${subId}-desc`} className="block text-[11px] text-muted">
                            {sub.description}
                          </span>
                        ) : null}
                      </span>
                    </label>
                  )
                })}
              </div>
            ) : null}
          </div>
        )
      })}
      {freeText ? (
        <>
          <label id={`${name}-text-label`} htmlFor={`${name}-text`} className="sr-only">
            {options.length > 0 ? i18nT('apps.workflows.kas.question_other') : q.prompt}
          </label>
          <textarea
            id={`${name}-text`}
            aria-labelledby={`${name}-text-label`}
            className="w-full min-h-[2.5rem] resize-y rounded border border-border bg-bg text-text text-[12.5px] px-2 py-1.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
            placeholder={options.length > 0 ? i18nT('apps.workflows.kas.question_other') : i18nT('apps.workflows.kas.question_type_answer')}
            value={draft.text}
            disabled={disabled}
            onChange={e => onChange(q.multiSelect ? { ...draft, text: e.target.value } : { picked: [], text: e.target.value })}
          />
        </>
      ) : null}
    </fieldset>
  )
}

/**
 * The presentational question form: drafts, options / multi-select / free text,
 * Send and Dismiss. Submission is the CALLER's — a workflow step answers through
 * the KAS step-question route (`KasQuestionCard`), while the chat's OWN question
 * answers through the chat approve route (`KasChatQuestionCard`) — so the one
 * form renders both. Test ids are kept here so both callers share them.
 */
export function KasQuestionForm({
  question,
  title,
  pending,
  error,
  onSubmit,
  onDismiss,
}: {
  question: { key: string; questions: KasQuestionItem[] }
  title: string
  pending: boolean
  error?: unknown
  onSubmit: (answers: KasQuestionAnswers) => void
  onDismiss: () => void
}) {
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  // A question nobody touched yet reads as its initial draft (the recommended
  // answer picked), so Send is ready the moment the card opens on one.
  const draftOf = (q: KasQuestionItem): Draft => drafts[q.id] ?? initialDraft(q)
  const answers: KasQuestionAnswers = {}
  let complete = question.questions.length > 0
  for (const q of question.questions) {
    const value = answerOf(q, draftOf(q))
    if (value !== undefined && questionAnswered(value)) answers[q.id] = value
    else complete = false
  }
  return (
    <form
      className="rounded-md border border-accent/40 bg-bg-elevated px-3 py-2 flex flex-col gap-2.5"
      data-testid="kas-question-card"
      aria-label={title}
      onSubmit={e => {
        e.preventDefault()
        if (complete && !pending) onSubmit(answers)
      }}
    >
      <div className="flex items-center gap-1.5 text-[13px] font-semibold text-text-strong">
        <MessageCircleQuestionMark size={14} className="text-accent shrink-0" aria-hidden="true" />
        <span className="min-w-0 break-words">{title}</span>
      </div>
      {question.questions.map((q, i) => (
        <QuestionField
          key={q.id}
          q={q}
          qIndex={i}
          cardKey={question.key}
          draft={draftOf(q)}
          disabled={pending}
          onChange={d => setDrafts(cur => ({ ...cur, [q.id]: d }))}
        />
      ))}
      {error ? (
        <p className="m-0 text-[11.5px] text-danger" role="alert">
          {error instanceof Error ? error.message : String(error)}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center justify-end gap-1.5">
        <Btn type="button" onClick={onDismiss} disabled={pending} data-testid="kas-question-dismiss">
          {i18nT('apps.workflows.kas.question_dismiss')}
        </Btn>
        <Btn primary type="submit" disabled={!complete || pending} data-testid="kas-question-send">
          <Send className="lucide-inline" /> {i18nT('apps.workflows.kas.question_send')}
        </Btn>
      </div>
    </form>
  )
}

export function KasQuestionCard({ question, stepLabel }: { question: KasQuestion; stepLabel?: string | null }) {
  const { question: mutation } = useKasParityActions()
  const pending = mutation.isPending && mutation.variables?.key === question.key
  const title = question.title || (stepLabel ? i18nT('apps.workflows.kas.question_from_step', { step: stepLabel }) : i18nT('apps.workflows.kas.question_title'))
  const failure = mutation.error && mutation.variables?.key === question.key ? mutation.error : null
  return (
    <KasQuestionForm
      question={question}
      title={title}
      pending={pending}
      error={failure}
      onSubmit={answers => mutation.mutate({ key: question.key, answers })}
      onDismiss={() => mutation.mutate({ key: question.key, dismiss: true })}
    />
  )
}
