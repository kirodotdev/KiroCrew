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

export default function SpecStatePanel({ detail, sendMessage }: SpecStatePanelProps) {
  const [answering, setAnswering] = useState<string | null>(null)
  const st = detail?.state
  const ctx = detail?.context
  const decisions: SpecDecision[] = Array.isArray(st?.decisions) ? st!.decisions! : []

  const answer = async (d: SpecDecision, opt: string) => {
    setAnswering(d.id)
    try { await sendMessage(i18nT('apps.specBuilder.components.specStatePanel.decision_title', { title: d.title }) + ': ' + opt) }
    catch { /* surfaced by the parent mutation's onError */ } finally { setAnswering(null) }
  }

  if (!decisions.length && !st?.blocking && !ctx) return null

  const label = (t: string) => (
    <div className="text-[11px] font-bold text-muted mx-0.5 mt-3 mb-1.5" style={{ letterSpacing: '.08em' }}>{t}</div>
  )

  const ctxRows: [string, string][] = [
    ...(ctx?.worktree_branch ? ([[i18nT('apps.specBuilder.components.specStatePanel.worktree'), ctx.worktree_branch]] as [string, string][]) : []),
    ...(st?.context?.template ? ([[i18nT('apps.specBuilder.components.specStatePanel.template'), st.context.template]] as [string, string][]) : []),
    [i18nT('apps.specBuilder.components.specStatePanel.turns'), String(ctx?.turns ?? 0)],
    [i18nT('apps.specBuilder.components.specStatePanel.tool_calls'), String(ctx?.tool_calls ?? 0)],
  ]

  return (
    <div className="shrink-0 overflow-y-auto mt-0.5" style={{ maxHeight: '46%' }}>
      {decisions.length > 0 && (
        <>
          {label('DECISIONS')}
          {decisions.map((d) => (
            <div
              key={d.id}
              className="rounded-lg bg-card px-3 py-2.5 mb-1.5"
              style={{ border: '1px solid ' + (d.answer ? 'var(--border)' : 'color-mix(in srgb, var(--accent) 50%, transparent)') }}
            >
              <div className="flex items-center gap-2" style={{ marginBottom: d.answer ? 0 : '7px' }}>
                <span className="text-[12px] font-semibold text-text flex-1">{d.title}</span>
                <span
                  className="font-mono text-[11px] px-2 py-0.5 rounded-full"
                  style={{ background: d.answer ? 'color-mix(in srgb, var(--ok) 15%, transparent)' : SEL_BG, color: d.answer ? 'var(--ok)' : ACCENT }}
                >
                  {d.answer ? 'answered' : 'pending'}
                </span>
              </div>
              {d.answer ? (
                <div className="text-[12px] text-muted mt-1">→ {d.answer}</div>
              ) : (
                <div className="flex flex-col gap-1" role="group" aria-label={i18nT('apps.specBuilder.components.specStatePanel.options_for', { title: d.title })}>
                  {(d.options || []).map((opt) => (
                    <Clickable
                      key={opt}
                      onClick={() => { if (!answering) answer(d, opt) }}
                      disabled={!!answering}
                      aria-label={i18nT('apps.specBuilder.components.specStatePanel.answer_with', { title: d.title }) + opt + (opt === d.recommended ? ' (recommended)' : '')}
                      className="flex items-center gap-2 px-2.5 py-1.5 rounded-md border border-border focus-ring"
                      style={{ cursor: answering ? 'default' : 'pointer', opacity: answering && answering !== d.id ? 0.5 : 1 }}
                    >
                      <span
                        className="w-[11px] h-[11px] rounded-full shrink-0"
                        style={{ border: '2px solid ' + (opt === d.recommended ? ACCENT : 'var(--border)') }}
                      />
                      <span className="text-[12px] text-text flex-1">{opt}</span>
                      {opt === d.recommended && (
                        <span className="font-mono text-[11px]" style={{ color: ACCENT, letterSpacing: '.06em' }}>{i18nT('apps.specBuilder.components.specStatePanel.recommended')}</span>
                      )}
                    </Clickable>
                  ))}
                </div>
              )}
            </div>
          ))}
        </>
      )}

      {st?.blocking && (
        <>
          {label('BLOCKING')}
          <div
            className="rounded-lg bg-card px-3 py-2.5 text-[12px] leading-relaxed text-text"
            style={{ border: '1px solid color-mix(in srgb, var(--warn) 45%, transparent)' }}
          >
            {st.blocking}
          </div>
        </>
      )}

      {ctx && (
        <>
          {label('CONTEXT')}
          <div className="rounded-lg bg-card border border-border overflow-hidden">
            {ctxRows.map(([k, v]) => (
              <div key={k} className="flex justify-between gap-2.5 px-3 py-[7px] border-b border-border">
                <span className="text-[12px] text-muted">{k}</span>
                <span className="font-mono text-[12px] text-text overflow-hidden text-ellipsis whitespace-nowrap">{v}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
