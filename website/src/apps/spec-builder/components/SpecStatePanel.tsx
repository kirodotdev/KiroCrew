// SpecStatePanel — phase-2 structured state below the docs card: DECISIONS
// (clickable option cards that POST 'Decision — <title>: <option>'), a BLOCKING
// note, and a CONTEXT stats table (turns / tool calls / worktree / template).
import { useState } from 'react'
import { type SpecDetail, type SpecDecision } from '../api'
import { ACCENT, SEL_BG } from './shared'
import Clickable from '../../../components/Clickable'

import { i18nT } from '../../../i18n/t'
export interface SpecStatePanelProps {
  detail: SpecDetail | null
  /** Send a chat message through the parent's mutation, so the answer invalidates
   *  BOTH the detail and the specs-list queries. Answering a decision bumps
   *  updated_at, and a direct API write left the rail's ordering stale. */
  sendMessage: (msg: string) => Promise<unknown>
}

/** Identity of a decision as this panel rendered it. The id alone is not enough:
 *  the agent rewrites .spec-state.json wholesale, so an id can come back carrying
 *  a DIFFERENT question — and a local "sent" mark keyed on the id alone would
 *  then claim the new question was already answered. */
const decisionKey = (d: SpecDecision) => d.id + '\u0000' + d.title

export default function SpecStatePanel({ detail, sendMessage }: SpecStatePanelProps) {
  const [answering, setAnswering] = useState<string | null>(null)
  // Options this panel has already sent, per decision identity. The agent has to
  // finish its turn and rewrite .spec-state.json before ``d.answer`` appears, which
  // is up to a minute — without a local mark the card sat on "pending" with an
  // empty radio the whole time and the click looked lost.
  const [sent, setSent] = useState<Record<string, string>>({})
  const st = detail?.state
  const ctx = detail?.context
  const decisions: SpecDecision[] = Array.isArray(st?.decisions) ? st!.decisions! : []

  const answer = async (d: SpecDecision, opt: string) => {
    const key = decisionKey(d)
    setAnswering(d.id)
    setSent((s) => ({ ...s, [key]: opt }))
    try { await sendMessage(i18nT('apps.specBuilder.components.specStatePanel.decision_title', { title: d.title }) + ': ' + opt) }
    catch {
      // Surfaced by the parent mutation's onError. Drop the local mark too: the
      // instruction never reached the agent, so the question is still open and
      // must stay clickable.
      setSent((s) => { const next = { ...s }; delete next[key]; return next })
    } finally { setAnswering(null) }
  }

  if (!decisions.length && !st?.blocking && !ctx) return null

  // Sticky, and a FIXED height (12 + 16 + 6 = 34px): the decision cards' own
  // sticky headers park directly below it, and an inherited line-height would
  // make that offset drift.
  const label = (t: string) => (
    <div
      className="sticky top-0 z-[2] text-[11px] leading-4 font-bold text-muted bg-bg pt-3 pb-1.5"
      style={{ letterSpacing: '.08em' }}
    >
      {t}
    </div>
  )

  const ctxRows: [string, string][] = [
    ...(ctx?.worktree_branch ? ([[i18nT('apps.specBuilder.components.specStatePanel.worktree'), ctx.worktree_branch]] as [string, string][]) : []),
    ...(st?.context?.template ? ([[i18nT('apps.specBuilder.components.specStatePanel.template'), st.context.template]] as [string, string][]) : []),
    [i18nT('apps.specBuilder.components.specStatePanel.turns'), String(ctx?.turns ?? 0)],
    [i18nT('apps.specBuilder.components.specStatePanel.tool_calls'), String(ctx?.tool_calls ?? 0)],
  ]

  return (
    // A bounded tray, not a continuation of the document: its own top border and
    // page background separate it from the prose above, and the horizontal
    // padding keeps the cards off the column edges. Without both, the first
    // decision card's accent border read as part of the document and the
    // question-answer area had no shape of its own.
    <div
      className="shrink-0 flex flex-col border-t border-border bg-bg"
      style={{ maxHeight: '46%' }}
    >
      <div className="overflow-y-auto px-3.5 pb-3.5">
        {decisions.length > 0 && (
          <>
            {label(i18nT('apps.specBuilder.components.specStatePanel.decisions'))}
            {decisions.map((d) => {
              // The server's answer wins; the local mark only covers the window
              // before the agent has written one.
              const pending = !d.answer ? sent[decisionKey(d)] : undefined
              const settled = d.answer || pending
              return (
                <div
                  key={d.id}
                  className="rounded-lg bg-card mb-2"
                  style={{ border: '1px solid ' + (settled ? 'var(--border)' : 'color-mix(in srgb, var(--accent) 50%, transparent)') }}
                >
                  {/* Sticky so the question stays visible while its options
                      scroll — a list of bare options with the question scrolled
                      out of the tray is unanswerable. */}
                  <div
                    className="sticky top-[34px] z-[1] flex items-center gap-2 px-3.5 py-2.5 rounded-t-lg bg-card"
                    style={{ borderBottom: settled ? 'none' : '1px solid var(--border)' }}
                  >
                    <span className="text-[12.5px] font-semibold text-text flex-1">{d.title}</span>
                    <span
                      className="font-mono text-[11px] px-2 py-0.5 rounded-full shrink-0"
                      style={{ background: d.answer ? 'color-mix(in srgb, var(--ok) 15%, transparent)' : SEL_BG, color: d.answer ? 'var(--ok)' : ACCENT }}
                    >
                      {d.answer
                        ? i18nT('apps.specBuilder.components.specStatePanel.answered')
                        : pending
                          ? i18nT('apps.specBuilder.components.specStatePanel.sending')
                          : i18nT('apps.specBuilder.components.specStatePanel.pending')}
                    </span>
                  </div>
                  {settled ? (
                    <div className="text-[12px] text-muted px-3.5 pb-2.5">→ {d.answer || pending}</div>
                  ) : (
                    <div className="flex flex-col gap-1.5 px-3 py-3" role="group" aria-label={i18nT('apps.specBuilder.components.specStatePanel.options_for', { title: d.title })}>
                      {(d.options || []).map((opt) => (
                        <Clickable
                          key={opt}
                          onClick={() => { if (!answering) answer(d, opt) }}
                          disabled={!!answering}
                          aria-label={i18nT('apps.specBuilder.components.specStatePanel.answer_with', { title: d.title }) + opt + (opt === d.recommended ? ' (recommended)' : '')}
                          className="flex items-center gap-2.5 px-3 py-2 rounded-md border border-border focus-ring"
                          style={{ cursor: answering ? 'default' : 'pointer', opacity: answering && answering !== d.id ? 0.5 : 1 }}
                        >
                          <span
                            className="w-[11px] h-[11px] rounded-full shrink-0"
                            style={{ border: '2px solid ' + (opt === d.recommended ? ACCENT : 'var(--border)') }}
                          />
                          <span className="text-[12.5px] text-text flex-1 leading-snug">{opt}</span>
                          {opt === d.recommended && (
                            <span className="font-mono text-[11px] shrink-0" style={{ color: ACCENT, letterSpacing: '.06em' }}>{i18nT('apps.specBuilder.components.specStatePanel.recommended')}</span>
                          )}
                        </Clickable>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </>
        )}

        {st?.blocking && (
          <>
            {label(i18nT('apps.specBuilder.components.specStatePanel.blocking'))}
            <div
              className="rounded-lg bg-card px-3.5 py-2.5 text-[12.5px] leading-relaxed text-text"
              style={{ border: '1px solid color-mix(in srgb, var(--warn) 45%, transparent)' }}
            >
              {st.blocking}
            </div>
          </>
        )}

        {ctx && (
          <>
            {label(i18nT('apps.specBuilder.components.specStatePanel.context'))}
            <div className="rounded-lg bg-card border border-border overflow-hidden">
              {ctxRows.map(([k, v], i) => (
                <div
                  key={k}
                  className={'flex justify-between gap-2.5 px-3.5 py-[7px]' + (i < ctxRows.length - 1 ? ' border-b border-border' : '')}
                >
                  <span className="text-[12px] text-muted">{k}</span>
                  <span className="font-mono text-[12px] text-text overflow-hidden text-ellipsis whitespace-nowrap">{v}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
