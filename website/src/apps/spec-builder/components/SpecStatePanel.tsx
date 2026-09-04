// SpecStatePanel — phase-2 structured state below the docs card: DECISIONS
// (clickable option cards that POST 'Decision — <title>: <option>'), a BLOCKING
// note, and a CONTEXT stats table (turns / tool calls / worktree / template).
import { useEffect, useRef, useState } from 'react'
import { type SpecDetail, type SpecDecision, type ApiError } from '../api'
import { ACCENT, SEL_BG } from './shared'
import Clickable from '../../../components/Clickable'

import { i18nT } from '../../../i18n/t'

/** Refusal codes the backend emits BEFORE recording anything, so the optimistic
 *  lock can be released and the user can answer again. Every other failure is
 *  ambiguous — the write may have committed — and keeps the card locked until a
 *  refetch says otherwise. */
const DEFINITELY_NOT_RECORDED = new Set([
  'decision_agent_busy',
  // Another view of this spec is working (a running turn or an armed build). The backend
  // refuses this BEFORE the claim writes anything, exactly like the busy code above, so
  // the card must re-open — leaving it locked would strand the user on "sending" for an
  // answer no one holds.
  'spec_busy_elsewhere',
  'decision_option_required',
  'decision_record_unreadable',
  'decision_record_write_failed',
  'decision_ledger_full',
  'stale_client',
  'slot_owned_by_another_app',
  'not_found',
  'text_required',
  'app_disabled',
  'unauthorized',
])

export interface SpecStatePanelProps {
  detail: SpecDetail | null
  /** Send a decision's answer through the parent's mutation, so the answer
   *  invalidates BOTH the detail and the specs-list queries. Answering a decision
   *  bumps updated_at, and a direct API write left the rail's ordering stale.
   *  Carries the decision id and the bare option: the backend records them and
   *  refuses a second answer for the same decision. */
  answerDecision: (decisionId: string, option: string, msg: string) => Promise<unknown>
}

/** Identity of a decision as this panel rendered it. The id alone is not enough:
 *  the agent rewrites .spec-state.json wholesale, so an id can come back carrying
 *  a DIFFERENT question — and a local "sent" mark keyed on the id alone would
 *  then claim the new question was already answered. */
const decisionKey = (d: SpecDecision) => d.id + '\u0000' + d.title

export default function SpecStatePanel({ detail, answerDecision }: SpecStatePanelProps) {
  const [answering, setAnswering] = useState<string | null>(null)
  // Options this session has already sent, keyed on decision IDENTITY (see decisionKey).
  // The backend is the authority — it refuses a second answer — but its record only
  // reaches this component on the next detail poll, so the card locks here the moment the
  // click is made rather than staying clickable for the round trip plus however long the
  // agent takes to write `answer` into its own state file.
  const [sent, setSent] = useState<Record<string, string>>({})
  // Identities whose request has SETTLED (either way). The parent refetches the detail on
  // settle, so the next payload to arrive is the server's own verdict for them -- which is
  // what lets an optimistic lock be dropped safely.
  const settledIds = useRef<Set<string>>(new Set())
  const st = detail?.state
  const ctx = detail?.context

  // Reconcile the optimistic locks against a payload fetched AFTER the request
  // settled: a decision the server does not consider answered is re-opened, so a
  // request that never reached it cannot leave the card locked forever. Anything
  // still in flight is left alone -- dropping it there is the race this panel exists
  // to close, because the state file lags the dispatch.
  useEffect(() => {
    if (!detail || settledIds.current.size === 0) return
    const locked = new Set(
      (Array.isArray(st?.decisions) ? st!.decisions! : [])
        .filter((d) => !!d.locked || !!d.answer)
        .map(decisionKey),
    )
    const clearing = [...settledIds.current].filter((k) => !locked.has(k))
    if (!clearing.length) return
    clearing.forEach((k) => settledIds.current.delete(k))
    setSent((s) => {
      const next = { ...s }
      clearing.forEach((k) => delete next[k])
      return next
    })
  }, [detail, st])
  // An answer sent mid-turn would be QUEUED behind the running turn, and Pause
  // clears that queue — so the backend refuses one while the agent works. Holding
  // the options back here means the user meets a disabled control instead of an
  // error; the server-side refusal stays as the backstop for the race.
  const busy = !!detail?.running
  const decisions: SpecDecision[] = Array.isArray(st?.decisions) ? st!.decisions! : []

  const answer = async (d: SpecDecision, opt: string) => {
    const key = decisionKey(d)
    setAnswering(d.id)
    setSent((s) => ({ ...s, [key]: opt }))
    try { await answerDecision(d.id, opt, i18nT('apps.specBuilder.components.specStatePanel.decision_title', { title: d.title }) + ': ' + opt) }
    catch (e) {
      // The message is surfaced by the parent mutation's onError. What matters here is
      // whether the answer might have been recorded. A write can commit and still fail
      // on the way back (the response connection drops), so an ambiguous failure KEEPS
      // the optimistic lock for now — re-opening the card immediately would invite a
      // second answer for a decision the agent already has. The reconcile effect above
      // releases it once a refetched detail says the server holds no record.
      //
      // A refusal the backend named is unambiguous: those codes are emitted before
      // anything is recorded, so the card re-opens at once.
      if (DEFINITELY_NOT_RECORDED.has((e as ApiError)?.code || '')) {
        setSent((s) => { const { [key]: _drop, ...rest } = s; return rest })
      }
    } finally {
      // Whatever happened, the request is over: the next detail payload is the
      // server's verdict on this decision, so the effect above may act on it.
      settledIds.current.add(key)
      setAnswering(null)
    }
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
              // A decision is settled once its answer has reached the agent: recorded by
              // the backend (`locked`), written into the agent's own state (`answer`), or
              // sent by this session a moment ago (`sent`). A settled card shows the
              // answer and nothing clickable — offering the options again invites the user
              // to reverse a decision the agent already holds, which is what a re-emitted
              // pending card used to do.
              //
              // The local mark only covers the window before one of the first two arrives.
              const pending = !d.answer ? sent[decisionKey(d)] : undefined
              const chosen = d.answer || pending || ''
              const settled = !!d.locked || !!chosen
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
                      style={{ background: settled ? 'color-mix(in srgb, var(--ok) 15%, transparent)' : SEL_BG, color: settled ? 'var(--ok)' : ACCENT }}
                    >
                      {d.answer || d.locked
                        ? i18nT('apps.specBuilder.components.specStatePanel.answered')
                        : pending
                          ? i18nT('apps.specBuilder.components.specStatePanel.sending')
                          : i18nT('apps.specBuilder.components.specStatePanel.pending')}
                    </span>
                  </div>
                  {settled ? (
                    // A card the SERVER reports locked may have no text yet: it holds the
                    // record, the agent has not written its state file, and this session
                    // is not the one that answered. Locked with nothing to show beats
                    // rendering an empty arrow.
                    chosen ? <div className="text-[12px] text-muted px-3.5 pb-2.5">→ {chosen}</div> : null
                  ) : (
                    <div className="flex flex-col gap-1.5 px-3 py-3" role="group" aria-label={i18nT('apps.specBuilder.components.specStatePanel.options_for', { title: d.title })}>
                      {(d.options || []).map((opt) => (
                        <Clickable
                          key={opt}
                          onClick={() => { if (!answering && !busy) answer(d, opt) }}
                          disabled={!!answering || busy}
                          aria-label={opt === d.recommended
                            ? i18nT('apps.specBuilder.components.specStatePanel.answer_option_recommended', { title: d.title, option: opt })
                            : i18nT('apps.specBuilder.components.specStatePanel.answer_option', { title: d.title, option: opt })}
                          className="flex items-center gap-2.5 px-3 py-2 rounded-md border border-border focus-ring"
                          style={{ cursor: answering || busy ? 'default' : 'pointer', opacity: busy || (answering && answering !== d.id) ? 0.5 : 1 }}
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
