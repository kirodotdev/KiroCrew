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
 * puts the text in the chat box. A press locks the card at once (sending), so a
 * second click sends nothing, and the card then says what really happened:
 * sent; not sent (it opens again to retry); or not confirmed (it may have gone
 * out, so it stays locked rather than invite a second start).
 *
 * Each field is a multi-line box. A further line of a field goes out starting
 * with two spaces (`grillFieldText`), so the conductor still reads one
 * labelled line per field. Pictures the reader adds upload through the chat
 * box's own upload route and go out with Start, never what the chat box has
 * staged; a picture shows as added only once the server has its path, and
 * Start waits while one is uploading.
 *
 * Both texts end with a `key: <mid>:<version>` line naming this card. A reload
 * locks the card again once a later stored user message carries that exact
 * line; the conductor reads the same line to ignore a repeated start. A row the
 * server has not named yet sends no key line, and that card reopens on reload.
 */
import { useEffect, useId, useRef, useState, type ChangeEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { GRILL_LIMITS, grillFieldText, grillKeyLine, type GrillProposal } from '../../app-sdk/protocol/grillMarker'
import { api } from '../../api/client'
import { btnCls, btnPrimaryCls } from '../redactionPrimitives'
import ErrorNotice from '../ErrorNotice'
import StatusNotice from '../StatusNotice'
import { X } from 'lucide-react'
import { withReason } from '../../i18n/format'

/** The event the card dispatches. The host serving `slot` sets `result`
 *  before it returns: `staged` when the text only reached the chat box, or
 *  `pending` when it went to send; a pending send then calls `onOutcome` once
 *  with what the send came to. No host (result still null): nothing happened. */
export const COMPOSE_SEND_EVENT = 'mc:compose-send'
export type ComposeSendResult = 'pending' | 'staged'
/** `refused`: it did not go out (the server said no, or the send stopped before
 *  it). `indeterminate`: the answer was lost, so it may be running. */
export type ComposeSendOutcome = 'sent' | 'refused' | 'indeterminate'
export interface ComposeSendDetail {
  text: string
  slot: string
  /** Paths of files the card uploaded itself, sent with `text` (or, where
   *  nothing can go out, put in the chat box beside it). */
  files?: string[]
  result: ComposeSendResult | null
  /** `reason`: why a `refused` send was refused, already framed for the
   *  reader, when the host knows (the main chat's isolated send shows it
   *  nowhere else). */
  onOutcome: (outcome: ComposeSendOutcome, reason?: string) => void
}

/** A transcript row's identity that stays put when a refresh adds its `ts` or
 *  `mid` (the store's own keying order: `clientTs`, then `mid`, then `ts`). */
export function grillRowId(m: { ts?: string; meta?: Record<string, unknown> }): string | undefined {
  const pick = (v: unknown) => (typeof v === 'string' && v ? v : undefined)
  return pick(m.meta?.clientTs) ?? pick(m.meta?.mid) ?? pick(m.ts)
}

/** What the card shows once pressed. `failed` is not a lock: the card is open
 *  again and says its last press did not go out. */
type CardResult = 'pending' | 'staged' | 'sent' | 'unconfirmed' | 'failed'

/** What a card holds while this page lives: its edited lines and its result.
 *  Kept outside the component because a virtualized transcript unmounts rows
 *  scrolled out of view; nothing is stored, so a reload starts fresh. A send's
 *  outcome is written here too, and handed to whichever copy of the card is on
 *  screen when it lands (`shown`), so a row scrolled away and back shows it. */
interface CardState { q: string; must: string; skip: string; files: string[]; result: CardResult | null; reason?: string }
const cardStates = new Map<string, CardState>()
const shown = new Map<string, (r: CardResult, reason?: string) => void>()

/** Hand `text` to the page serving `slot`; null when none took it (nothing happened). */
function composeSend(text: string, files: string[], slot: string | undefined, onOutcome: ComposeSendDetail['onOutcome']): ComposeSendResult | null {
  if (!slot) return null
  const detail: ComposeSendDetail = { text, slot, result: null, onOutcome, ...(files.length ? { files } : {}) }
  window.dispatchEvent(new CustomEvent(COMPOSE_SEND_EVENT, { detail }))
  return detail.result
}

const OUTCOME: Record<ComposeSendOutcome, CardResult> = { sent: 'sent', refused: 'failed', indeterminate: 'unconfirmed' }

// Grows with what is written (`field-sizing`), up to a cap, then scrolls.
const FIELD = 'w-full min-w-0 [field-sizing:content] max-h-60 resize-y rounded-md border border-border bg-bg px-2 py-1 text-[13px] leading-5 text-text focus-ring-accent disabled:opacity-70'
/** Pictures that go out as image references (`IMG_EXT`); HEIC/HEIF upload as JPEG. */
const PICTURE_TYPES = '.png,.jpg,.jpeg,.gif,.webp,.bmp,.heic,.heif'
/** The composer's own per-send cap on files. */
const MAX_FILES = 20
const baseName = (p: string) => p.split(/[\\/]/).pop() || p
const LABEL = 'text-[11px] leading-4 font-semibold text-muted'

/** `slotKey`: the chat this reply belongs to. `stateKey`: this reply version's
 *  identity, so its edits and lock survive the row scrolling out and back.
 *  `wireKey`: the key its texts carry (absent: none yet). `answered`: a later
 *  stored user message already carries that key, so the card is used. */
export default function GrillCard({ grill, slotKey, stateKey, wireKey, answered = false }: { grill: GrillProposal; slotKey?: string; stateKey?: string; wireKey?: string; answered?: boolean }) {
  const { t } = useTranslation()
  const id = useId()
  const kept = stateKey ? cardStates.get(stateKey) : undefined
  const [q, setQ] = useState(kept?.q ?? grill.q)
  const [must, setMust] = useState(kept?.must ?? grill.must)
  const [skip, setSkip] = useState(kept?.skip ?? grill.skip)
  const [files, setFiles] = useState<string[]>(kept?.files ?? [])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState('')
  // A pre-check refusal: nothing was sent, so it is a hint, not a failure.
  const [uploadHint, setUploadHint] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)
  // shortcut: an upload still running when the row scrolls away is cancelled,
  // so its pictures never show as added; pick them again on return. Upgrade by
  // writing the result into `cardStates` if readers hit this.
  const uploadCtl = useRef<AbortController | null>(null)
  useEffect(() => () => uploadCtl.current?.abort(), [])
  const [result, setResult] = useState<CardResult | null>(kept?.result ?? null)
  const [reason, setReason] = useState<string | undefined>(kept?.reason)
  // Read on press, ahead of the render a state change waits for: a second
  // click in the same tick finds the card already sending.
  const resultRef = useRef(result)
  resultRef.current = result
  useEffect(() => { if (stateKey) cardStates.set(stateKey, { q, must, skip, files, result, reason }) }, [stateKey, q, must, skip, files, result, reason])
  useEffect(() => {
    if (!stateKey) return
    const show = (r: CardResult, why?: string) => { setResult(r); setReason(why) }
    shown.set(stateKey, show)
    return () => { if (shown.get(stateKey) === show) shown.delete(stateKey) }
  }, [stateKey])
  const [unhandled, setUnhandled] = useState(false)
  // A later stored (or queued) message carrying this card's key is the
  // strongest word on it, so it outranks whatever this page last saw.
  const pressed = result !== null && result !== 'failed'
  const locked = pressed || answered
  const shownResult = answered ? 'sent' : result

  const pick = async (e: ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files ?? [])
    e.target.value = ''
    if (!picked.length || uploadCtl.current) return
    setUploadError('')
    setUploadHint('')
    if (files.length + picked.length > MAX_FILES) { setUploadHint(t('pages.chatPage.too_many_files_max_20')); return }
    const controller = new AbortController()
    uploadCtl.current = controller
    setUploading(true)
    try {
      const res = await api.uploadFiles(picked, controller.signal)
      if (controller.signal.aborted) return
      if (res.error) setUploadError(t('pages.chatPage.upload_failed_error', { error: res.error }))
      else setFiles(cur => [...cur, ...res.paths.filter(p => p && !cur.includes(p))])
    } catch (err) {
      if (controller.signal.aborted || (err as Error | undefined)?.name === 'AbortError') return
      const message = (err as Error | undefined)?.message
      setUploadError(t('pages.chatPage.upload_failed_error', { error: !message || err instanceof TypeError ? t('pages.chatPage.connection_error') : message }))
    } finally {
      if (uploadCtl.current === controller) { uploadCtl.current = null; setUploading(false) }
    }
  }

  const act = (kind: 'start' | 'decline') => {
    if (answered || (resultRef.current !== null && resultRef.current !== 'failed')) return
    // A picture still on its way would be left behind.
    if (kind === 'start' && uploadCtl.current) return
    // In the reader's language: a first line, then one labelled line per field.
    // The conductor reads any language. The key line is a wire value.
    const keyLine = wireKey ? [grillKeyLine(wireKey)] : []
    const text = (kind === 'start'
      ? [
          t('components.grillCard.send_start'),
          t('components.grillCard.send_question', { text: grillFieldText(q) }),
          t('components.grillCard.send_must', { text: grillFieldText(must) }),
          t('components.grillCard.send_skip', { text: grillFieldText(skip) }),
          ...keyLine,
        ]
      : [t('components.grillCard.send_decline'), ...keyLine]).join('\n')
    // The outcome lands after this press returns, maybe once the row has
    // scrolled away: it goes into the kept state, and to the card on screen.
    const settle = (r: CardResult, why?: string) => {
      resultRef.current = r
      if (!stateKey) { setResult(r); setReason(why); return }
      const cur = cardStates.get(stateKey)
      cardStates.set(stateKey, { q: cur?.q ?? q, must: cur?.must ?? must, skip: cur?.skip ?? skip, files: cur?.files ?? files, result: r, reason: why })
      shown.get(stateKey)?.(r, why)
    }
    let pending = false
    // An outcome the host reports before it returns (a send that stops at
    // once) waits here for the press to finish.
    const early: { r?: CardResult; why?: string } = {}
    const onOutcome = (o: ComposeSendOutcome, why?: string) => {
      const r = OUTCOME[o]
      const kept = r === 'failed' ? why : undefined
      if (pending) settle(r, kept)
      else { early.r = r; early.why = kept }
    }
    // shortcut: the chat page and ChatPane (split cells, member threads) take the
    // text; any other host (an SDK embed) leaves the card open and saying so.
    // Wire that host's composer to the event when one needs the card.
    resultRef.current = 'pending'
    const got = composeSend(text, kind === 'start' ? files : [], slotKey, onOutcome)
    if (!got) { resultRef.current = result; setUnhandled(true); return }
    setUnhandled(false)
    pending = got === 'pending'
    if (got === 'pending' && early.r) settle(early.r, early.why)
    else settle(got === 'pending' ? 'pending' : 'staged')
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
      data-result={shownResult ?? undefined}
      className="mt-2 min-w-0 flex flex-col gap-2 rounded-lg border border-border bg-card text-card-fg px-3 py-2.5"
    >
      <div className="text-[13px] leading-5 font-semibold text-text-strong">{t('components.grillCard.label')}</div>
      {fields.map(f => (
        <div key={f.key} className="flex flex-col gap-0.5">
          <label htmlFor={`${id}-${f.key}`} className={LABEL}>{f.label}</label>
          <textarea
            id={`${id}-${f.key}`}
            aria-label={f.label}
            rows={1}
            className={FIELD}
            value={f.value}
            maxLength={GRILL_LIMITS.edit}
            disabled={locked}
            data-testid={`grill-card-${f.key}`}
            onChange={e => f.set(e.target.value)}
          />
        </div>
      ))}
      {(files.length > 0 || !locked) && <div className="flex flex-wrap items-center gap-1.5" data-testid="grill-card-files">
        {files.map(p => (
          <span key={p} className="inline-flex min-w-0 max-w-full items-center gap-1 rounded-md border border-border bg-bg px-1.5 py-0.5 text-[12px] leading-4 text-text">
            <span className="truncate" title={p}>{baseName(p)}</span>
            {!locked && (
              <button type="button" className="text-muted hover:text-text focus-ring-accent rounded" aria-label={t('components.grillCard.remove_picture', { name: baseName(p) })} onClick={() => setFiles(cur => cur.filter(x => x !== p))}><X className="lucide-inline" aria-hidden /></button>
            )}
          </span>
        ))}
        {!locked && (
          <>
            <input ref={fileInput} type="file" accept={PICTURE_TYPES} multiple hidden aria-label={t('components.grillCard.add_picture')} data-testid="grill-card-file-input" onChange={e => { void pick(e) }} />
            <button type="button" className={btnCls} data-testid="grill-card-attach" disabled={uploading} onClick={() => fileInput.current?.click()}>
              {uploading ? t('components.grillCard.uploading') : t('components.grillCard.add_picture')}
            </button>
          </>
        )}
      </div>}
      {/* No hand-off: asking the agent would navigate away from the card's unsent edited lines and picked pictures. */}
      {uploadError && !locked && <ErrorNotice message={uploadError} variant="inline" askAgent={false} onDismiss={() => setUploadError('')} testId="grill-card-upload-failed" />}
      {!locked && <StatusNotice message={uploadHint} onDismiss={() => setUploadHint('')} testId="grill-card-upload-hint" />}
      <div className="flex flex-col gap-0.5">
        <div className={LABEL}>{t('components.grillCard.who')}</div>
        <ul className="m-0 pl-4 list-disc text-[13px] leading-5 break-words" data-testid="grill-card-who">
          {grill.who.map((p, i) => <li key={i}><span className="font-semibold">{p.name}</span>: {p.stance}</li>)}
        </ul>
      </div>
      <div className="text-[12px] leading-4 text-muted" data-testid="grill-card-minutes">{t('components.grillCard.minutes', { count: grill.minutes })}</div>
      <div className="flex flex-wrap items-center gap-2">
        {/* A lost answer is a pressed action's failure, so it shows as a
            blocking error notice. No hand-off: asking the agent would navigate
            away from the card's edited direction lines (question, must, not
            discussing), as for a refused send below. */}
        {locked && shownResult === 'unconfirmed' ? (
          <ErrorNotice message={t('components.grillCard.unconfirmed')} variant="inline" askAgent={false} testId="grill-card-unconfirmed" />
        ) : locked ? (
          <span className="text-[12px] leading-5 text-muted" role="status" data-testid="grill-card-sent">
            {shownResult === 'pending' ? t('components.grillCard.sending')
              : shownResult === 'staged' ? t('components.grillCard.staged')
                : t('components.grillCard.sent')}
          </span>
        ) : (
          <>
            <button type="button" className={btnPrimaryCls} data-testid="grill-card-start" disabled={!q.trim() || uploading} onClick={() => act('start')}>
              {t('components.grillCard.start')}
            </button>
            <button type="button" className={btnCls} data-testid="grill-card-decline" onClick={() => act('decline')}>
              {t('components.grillCard.decline')}
            </button>
          </>
        )}
      </div>
      {/* No hand-off: asking the agent would navigate away from the card's unsent edited lines. */}
      {!locked && result === 'failed' && <ErrorNotice message={withReason(reason, t('components.grillCard.send_failed'))} variant="inline" askAgent={false} onDismiss={() => setResult(null)} testId="grill-card-failed" />}
      {unhandled && <ErrorNotice message={t('components.grillCard.not_sent')} variant="inline" askAgent={false} onDismiss={() => setUnhandled(false)} testId="grill-card-unhandled" />}
    </div>
  )
}
