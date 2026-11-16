/**
 * The discussion card a `[GRILL: {json}]` marker draws inside a reply: the
 * conductor proposes that workers grill a question among themselves, and the
 * user edits its direction (question, must, not discussing) before starting it.
 *
 * It never holds the turn. Start and No thanks are deliberate presses: they
 * hand their text, with the reply's chat, to the page serving that chat
 * (`mc:compose-send`), which sends it whatever the Quick Send setting says.
 * While a turn runs the page takes its composer's busy decision (steer, or
 * queue behind the turn); where nothing can go out (offline, a force stop) it
 * puts the text in the chat box. Once used, the card locks for as long as this
 * page shows it, and says what happened: sent, or put in the chat box. Sent
 * means handed to the gateway, which steers it in or queues it behind the turn.
 * A send that then fails shows in the chat's own error row, as a chip's does.
 * A reload opens the card again; the conductor ignores a repeated start.
 */
import { useEffect, useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { GRILL_LIMITS, type GrillProposal } from '../../app-sdk/protocol/grillMarker'
import { btnCls, btnPrimaryCls } from '../redactionPrimitives'
import ErrorNotice from '../ErrorNotice'

/** The event the card dispatches. The host serving `slot` sets `result`: `sent`
 *  when the text went to the gateway, `staged` when it only reached the chat box. */
export const COMPOSE_SEND_EVENT = 'mc:compose-send'
export type ComposeSendResult = 'sent' | 'staged'
export interface ComposeSendDetail { text: string; slot: string; result: ComposeSendResult | null }

/** A transcript row's identity that stays put when a refresh adds its `ts` or
 *  `mid` (the store's own keying order: `clientTs`, then `mid`, then `ts`). */
export function grillRowId(m: { ts?: string; meta?: Record<string, unknown> }): string | undefined {
  const pick = (v: unknown) => (typeof v === 'string' && v ? v : undefined)
  return pick(m.meta?.clientTs) ?? pick(m.meta?.mid) ?? pick(m.ts)
}

/** What a card holds while this page lives: its edited lines and its result.
 *  Kept outside the component because a virtualized transcript unmounts rows
 *  scrolled out of view; nothing is stored, so a reload starts fresh. */
interface CardState { q: string; must: string; skip: string; result: ComposeSendResult | null }
const cardStates = new Map<string, CardState>()

/** Hand `text` to the page serving `slot`; null when none took it (nothing happened). */
function composeSend(text: string, slot: string | undefined): ComposeSendResult | null {
  if (!slot) return null
  const detail: ComposeSendDetail = { text, slot, result: null }
  window.dispatchEvent(new CustomEvent(COMPOSE_SEND_EVENT, { detail }))
  return detail.result
}

const FIELD = 'w-full min-w-0 rounded-md border border-border bg-bg px-2 py-1 text-[13px] leading-5 text-text focus-ring-accent disabled:opacity-70'
const LABEL = 'text-[11px] leading-4 font-semibold text-muted'

/** `slotKey`: the chat this reply belongs to. `stateKey`: this reply version's
 *  identity, so its edits and lock survive the row scrolling out and back. */
export default function GrillCard({ grill, slotKey, stateKey }: { grill: GrillProposal; slotKey?: string; stateKey?: string }) {
  const { t } = useTranslation()
  const id = useId()
  const kept = stateKey ? cardStates.get(stateKey) : undefined
  const [q, setQ] = useState(kept?.q ?? grill.q)
  const [must, setMust] = useState(kept?.must ?? grill.must)
  const [skip, setSkip] = useState(kept?.skip ?? grill.skip)
  const [result, setResult] = useState<ComposeSendResult | null>(kept?.result ?? null)
  useEffect(() => { if (stateKey) cardStates.set(stateKey, { q, must, skip, result }) }, [stateKey, q, must, skip, result])
  const [unhandled, setUnhandled] = useState(false)
  const locked = result !== null

  const act = (kind: 'start' | 'decline') => {
    if (locked) return
    // In the reader's language: a first line, then one labelled line per field.
    // The conductor reads any language.
    const text = kind === 'start'
      ? [
          t('components.grillCard.send_start'),
          t('components.grillCard.send_question', { text: q.trim() }),
          t('components.grillCard.send_must', { text: must.trim() }),
          t('components.grillCard.send_skip', { text: skip.trim() }),
        ].join('\n')
      : t('components.grillCard.send_decline')
    // shortcut: the chat page and ChatPane (split cells, member threads) take the
    // text; any other host (an SDK embed) leaves the card open and saying so.
    // Wire that host's composer to the event when one needs the card.
    const got = composeSend(text, slotKey)
    if (!got) { setUnhandled(true); return }
    setUnhandled(false)
    setResult(got)
  }

  const fields: { key: string; label: string; value: string; set: (v: string) => void }[] = [
    { key: 'q', label: t('components.grillCard.question'), value: q, set: setQ },
    { key: 'must', label: t('components.grillCard.must'), value: must, set: setMust },
    { key: 'skip', label: t('components.grillCard.skip'), value: skip, set: setSkip },
  ]

  return (
    <div
      role="group"
      aria-label={t('components.grillCard.label')}
      data-testid="grill-card"
      data-result={result ?? undefined}
      className="mt-2 min-w-0 flex flex-col gap-2 rounded-lg border border-border bg-card text-card-fg px-3 py-2.5"
    >
      <div className="text-[13px] leading-5 font-semibold text-text-strong">{t('components.grillCard.label')}</div>
      {fields.map(f => (
        <div key={f.key} className="flex flex-col gap-0.5">
          <label htmlFor={`${id}-${f.key}`} className={LABEL}>{f.label}</label>
          <input
            id={`${id}-${f.key}`}
            aria-label={f.label}
            type="text"
            className={FIELD}
            value={f.value}
            maxLength={GRILL_LIMITS.text}
            disabled={locked}
            data-testid={`grill-card-${f.key}`}
            onChange={e => f.set(e.target.value)}
          />
        </div>
      ))}
      <div className="flex flex-col gap-0.5">
        <div className={LABEL}>{t('components.grillCard.who')}</div>
        <ul className="m-0 pl-4 list-disc text-[13px] leading-5 break-words" data-testid="grill-card-who">
          {grill.who.map((p, i) => <li key={i}><span className="font-semibold">{p.name}</span>: {p.stance}</li>)}
        </ul>
      </div>
      <div className="text-[12px] leading-4 text-muted" data-testid="grill-card-minutes">{t('components.grillCard.minutes', { count: grill.minutes })}</div>
      <div className="flex flex-wrap items-center gap-2">
        {locked ? (
          <span className="text-[12px] leading-5 text-muted" role="status" data-testid="grill-card-sent">
            {result === 'staged' ? t('components.grillCard.staged') : t('components.grillCard.sent')}
          </span>
        ) : (
          <>
            <button type="button" className={btnPrimaryCls} data-testid="grill-card-start" disabled={!q.trim()} onClick={() => act('start')}>
              {t('components.grillCard.start')}
            </button>
            <button type="button" className={btnCls} data-testid="grill-card-decline" onClick={() => act('decline')}>
              {t('components.grillCard.decline')}
            </button>
          </>
        )}
      </div>
      {/* No hand-off: asking the agent would navigate away from the card's unsent edited lines. */}
      {unhandled && <ErrorNotice message={t('components.grillCard.not_sent')} variant="inline" askAgent={false} onDismiss={() => setUnhandled(false)} testId="grill-card-unhandled" />}
    </div>
  )
}
