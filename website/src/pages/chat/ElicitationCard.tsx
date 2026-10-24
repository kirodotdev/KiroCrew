import { memo, useContext, useId, useState } from 'react'
import { ReactReduxContext } from 'react-redux'
import { Ban, CheckCircle, Clock, ExternalLink, Loader2, MessageSquareMore } from 'lucide-react'
import ErrorNotice from '../../components/ErrorNotice'
import SimpleSelect from '../../components/SimpleSelect'
import { approvalBtnClass } from '../../components/chat-input/approval'
import { apiTransport } from '../../api/apiTransport'
import { ApiError } from '../../api/apiError'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { ChatMessage } from '../../types'
import { resolveByApprovalId } from '../../store/chatSlice'

/**
 * One field of an elicitation form, as the gateway normalised it
 * (`kiro_crew.acp.elicitation.ElicitationField.as_dict`). The kinds are the
 * only shapes MCP's elicitation schema allows a client to render, so the
 * card draws exactly these and nothing else.
 */
export interface ElicitationField {
  name: string
  kind: 'string' | 'number' | 'integer' | 'boolean' | 'enum' | 'multi_enum'
  required: boolean
  title?: string
  description?: string
  options?: { value: string; label: string }[]
  default?: unknown
  minimum?: number
  maximum?: number
  min_length?: number
  max_length?: number
  /** Multi-select bounds on how many choices the answer may carry. */
  min_items?: number
  max_items?: number
  format?: string
}

/** The row meta an `elicitation` row carries (the backend's `request_id` arrives as `approval_id`). */
export interface ElicitationMeta {
  approval_id?: string
  tool_call_id?: string
  mode?: 'form' | 'url'
  message?: string
  fields?: ElicitationField[]
  url?: string
  resolved?: string
  answer?: Record<string, unknown>
  /** The row's delivery identity; the answer is bound to it (see `request_mid`). */
  mid?: string
  /** Who is asking: the MCP server and tool of the call this ask belongs to, from
   *  the trusted identity the runner recorded for that call. Absent when unknown. */
  mcp_server?: string
  tool_name?: string
}

/** The sender named for the title: `server · tool`, whichever parts are known. */
export function askerOf(meta: ElicitationMeta): string {
  return [meta.mcp_server, meta.tool_name].filter((part): part is string => typeof part === 'string' && part.length > 0).join(' · ')
}

/** The host a URL ask sends the person to, so the card says WHOSE site before the link. */
export function linkHost(url: string | undefined): string {
  if (!url) return ''
  try { return new URL(url).host } catch { return '' }
}

export type ElicitationValue = string | number | boolean | string[] | undefined

/** The person's answers before validation: one entry per field they touched. */
export type ElicitationDraft = Record<string, ElicitationValue>

export function elicitationMetaOf(m: ChatMessage): ElicitationMeta {
  return (m.meta ?? {}) as ElicitationMeta
}

/** A dictionary keyed by the server's field names, which are ordinary own keys
 *  whatever they spell: on a plain `{}` a field named `__proto__` reads as the
 *  inherited prototype and an assignment to it sets the prototype instead of
 *  the answer, which `JSON.stringify` then leaves out. */
function fieldDict<T>(): Record<string, T> {
  return Object.create(null) as Record<string, T>
}

/** Seed the draft from the server's defaults so a field with one is pre-filled. */
export function initialDraft(fields: ElicitationField[]): ElicitationDraft {
  const draft = fieldDict<ElicitationValue>()
  for (const f of fields) {
    if (f.default !== undefined && f.default !== null) draft[f.name] = f.default as ElicitationValue
    else if (f.kind === 'boolean') draft[f.name] = false
    else if (f.kind === 'multi_enum') draft[f.name] = []
  }
  return draft
}

/**
 * Shape the draft for the wire: a number field's text becomes a number, an
 * empty optional field is left out, and a required field with no value is the
 * first thing reported. The gateway validates again against the schema, so
 * this is the client-side half that keeps an obvious miss from a round trip.
 */
/** A select value no server offers (a lone NUL): what an UNSET enum hands the
 *  select, so the "Choose" row shows even when `''` is itself an offered choice. */
export const ENUM_UNSET = '\u0000'

export function draftToContent(fields: ElicitationField[], draft: ElicitationDraft): { content?: Record<string, unknown>; missing?: string; unsafe?: string; notWhole?: string } {
  const content = fieldDict<unknown>()
  for (const f of fields) {
    const v = Object.hasOwn(draft, f.name) ? draft[f.name] : undefined
    if (f.kind === 'multi_enum') {
      // An untouched optional multi-select is an OMITTED answer, whatever its
      // minimum: the bounds govern a list that is sent, and the gateway only
      // requires the field when the schema does. A required one is sent as
      // the list it is, an empty one included, and checked against the bounds.
      const picked = Array.isArray(v) ? v : []
      if (picked.length === 0 && !f.required) continue
      if ((f.min_items !== undefined && picked.length < f.min_items) || (f.max_items !== undefined && picked.length > f.max_items)) {
        return { missing: f.title || f.name }
      }
      content[f.name] = picked
      continue
    }
    // An enum's choice is whatever option the person picked, `''` included: only
    // a field with no draft entry is unset. A free-text field left blank is.
    const empty = f.kind === 'enum' ? v === undefined : v === undefined || v === ''
    if (empty) {
      if (f.required && f.kind !== 'boolean') return { missing: f.title || f.name }
      if (f.kind === 'boolean') content[f.name] = false
      continue
    }
    if (f.kind === 'number' || f.kind === 'integer') {
      const n = typeof v === 'number' ? v : Number(v)
      if (Number.isNaN(n)) return { missing: f.title || f.name }
      // Past 2^53 a JavaScript number has already rounded the digits typed, so
      // the server would receive an integer the person never entered. Refuse
      // it here, a default included, rather than send a silently changed answer.
      // A fraction in an integer field is its own miss, said as such; only a
      // whole number past 2^53 is the too-large case.
      if (f.kind === 'integer' && !Number.isInteger(n)) return { notWhole: f.title || f.name }
      if (f.kind === 'integer' && !Number.isSafeInteger(n)) return { unsafe: f.title || f.name }
      content[f.name] = n
    } else {
      content[f.name] = v
    }
  }
  return { content }
}

/** `text` cut to at most `max` Unicode code points (a surrogate pair is one). */
export function clipCodePoints(text: string, max: number | undefined): string {
  if (max === undefined) return text
  const points = Array.from(text)
  return points.length > max ? points.slice(0, max).join('') : text
}

function FieldLabel({ field }: { field: ElicitationField }) {
  return (
    <span className="flex flex-col gap-0.5 min-w-0">
      <span className="text-[12px] text-text font-medium">
        {field.title || field.name}
        {field.required && <span className="text-danger ml-0.5" aria-hidden="true">*</span>}
      </span>
      {field.description && <span className="text-[11px] text-muted">{field.description}</span>}
    </span>
  )
}

const inputClass = 'w-full rounded-md border border-border bg-bg/60 px-2 py-1 text-[12px] text-text font-body focus:outline-none focus:border-border-strong'

function FieldControl({ field, value, onChange, disabled }: {
  field: ElicitationField
  value: ElicitationValue
  onChange: (v: ElicitationValue) => void
  disabled: boolean
}) {
  // Per instance, not per field name: two cards on one page (a session grid)
  // may carry a field of the same name, and a shared id would send the second
  // label's click, and the typing after it, into the other session's form.
  const id = useId()
  if (field.kind === 'boolean') {
    return (
      <label className="flex items-start gap-2 cursor-pointer">
        <input
          id={id}
          type="checkbox"
          aria-label={field.title || field.name}
          className="mt-0.5 accent-accent"
          checked={value === true}
          disabled={disabled}
          onChange={e => onChange(e.target.checked)}
        />
        <FieldLabel field={field} />
      </label>
    )
  }
  if (field.kind === 'enum') {
    const options = field.options ?? []
    return (
      <div className="flex flex-col gap-1">
        <label htmlFor={id}><FieldLabel field={field} /></label>
        <SimpleSelect
          id={id}
          aria-label={field.title || field.name}
          options={options.map(o => o.value)}
          optionLabels={options.map(o => o.label)}
          value={typeof value === 'string' ? value : ENUM_UNSET}
          triggerFallback={i18nT('pages.chat.elicitationCard.choose')}
          disabled={disabled}
          onChange={onChange}
        />
      </div>
    )
  }
  if (field.kind === 'multi_enum') {
    const selected = Array.isArray(value) ? value : []
    return (
      <fieldset className="flex flex-col gap-1 border-none p-0 m-0 min-w-0">
        <legend className="p-0"><FieldLabel field={field} /></legend>
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {(field.options ?? []).map(o => (
            <label key={o.value} className="inline-flex items-center gap-1.5 text-[12px] text-text cursor-pointer">
              <input
                type="checkbox"
                aria-label={o.label}
                className="accent-accent"
                checked={selected.includes(o.value)}
                disabled={disabled || (field.max_items !== undefined && selected.length >= field.max_items && !selected.includes(o.value))}
                onChange={e => onChange(e.target.checked ? [...selected, o.value] : selected.filter(s => s !== o.value))}
              />
              {o.label}
            </label>
          ))}
        </div>
      </fieldset>
    )
  }
  const numeric = field.kind === 'number' || field.kind === 'integer'
  // `date-time` stays a text input: a `datetime-local` control shows an RFC 3339
  // value such as `2026-10-23T12:00:00Z` as BLANK while the draft still holds it,
  // so a person could submit a default they never saw. Text shows exactly what
  // is sent; the gateway validates the format.
  const inputType = numeric ? 'number' : field.format === 'email' ? 'email' : field.format === 'uri' ? 'url' : field.format === 'date' ? 'date' : 'text'
  return (
    <label htmlFor={id} className="flex flex-col gap-1">
      <FieldLabel field={field} />
      <input
        id={id}
        type={inputType}
        aria-label={field.title || field.name}
        className={inputClass}
        value={value === undefined || typeof value === 'boolean' || Array.isArray(value) ? '' : String(value)}
        disabled={disabled}
        required={field.required}
        min={field.minimum}
        max={field.maximum}
        step={field.kind === 'integer' ? 1 : undefined}
        placeholder={field.format === 'date-time' ? i18nT('pages.chat.elicitationCard.date_time_format') : undefined}
        // Not the native `maxLength`: it counts UTF-16 units, so one emoji is two
        // against a `maxLength: 1` the gateway (code points, like the schema)
        // accepts. Cut by code points instead, as the native attribute would on
        // paste; the gateway still checks both bounds on submit.
        onChange={e => onChange(clipCodePoints(e.target.value, field.max_length))}
      />
    </label>
  )
}

/** What an accepted form sent, one `label: value` per answered field, in field order. */
export function answerSummary(fields: ElicitationField[], answer: Record<string, unknown> | undefined): string {
  if (!answer) return ''
  const parts: string[] = []
  for (const f of fields) {
    if (!Object.hasOwn(answer, f.name)) continue
    const v = answer[f.name]
    const label = (value: unknown) => f.options?.find(o => o.value === value)?.label ?? String(value)
    const shown = Array.isArray(v) ? v.map(label).join(', ') : typeof v === 'boolean' ? (v ? '✓' : '✗') : label(v)
    parts.push(`${f.title || f.name}: ${shown}`)
  }
  return parts.join(' · ')
}

export function resolvedLabel(resolved: string): string {
  if (resolved === 'accept') return i18nT('pages.chat.elicitationCard.resolved_accept')
  if (resolved === 'decline') return i18nT('pages.chat.elicitationCard.resolved_decline')
  return i18nT('pages.chat.elicitationCard.resolved_cancel')
}

/** The sentence a failed answer shows: the gateway's own reason when it sent one. */
function answerErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 404) return i18nT('pages.chat.elicitationCard.no_longer_pending')
    // The transport already unwrapped the gateway's `error` field into the
    // message (friendlyErrText), which for a refused value names the field.
    if (err.message) return err.message
  }
  return i18nT('pages.chat.elicitationCard.answer_failed')
}

/**
 * An MCP server's own question to the person, raised in the middle of a tool
 * call: either a small form built from the schema the server sent, or a link
 * the person is asked to visit. Neither is Kiro Crew's tool approval -- the
 * server declared that a human must answer, so no trust setting answers it --
 * which is why this card has no Trust tier and its wording names the tool,
 * not the agent. Answering posts to the slot's `elicitation` endpoint; the
 * gateway validates a form against the schema and a refused value comes back
 * as the card's inline error with the form still open.
 */
export default memo(function ElicitationCard({ message, slot }: { message: ChatMessage; slot: string | null }) {
  useLanguageGeneration()
  const meta = elicitationMetaOf(message)
  const fields = meta.fields ?? []
  const isForm = meta.mode !== 'url'
  const [draft, setDraft] = useState<ElicitationDraft>(() => initialDraft(fields))
  const [busy, setBusy] = useState<'accept' | 'decline' | null>(null)
  // Two different things a person reads under the form: a client-side hint
  // that a required field is still empty (plain text beside the field it
  // names), and a request that failed (the shared ErrorNotice surface). They
  // never share a state, so a hint cannot be dressed as a failure or vice versa.
  const [hint, setHint] = useState<string | null>(null)
  const [requestError, setRequestError] = useState<string | null>(null)
  const resolved = meta.resolved
  const asker = askerOf(meta)
  const host = linkHost(meta.url)
  const requestId = meta.approval_id
  const requestMid = typeof meta.mid === 'string' ? meta.mid : ''
  // Optional: an embed that draws this card without the dashboard store has no
  // row to stamp, and the gateway's frame is then its only resolution.
  const store = useContext(ReactReduxContext)?.store

  const answer = async (action: 'accept' | 'decline') => {
    if (!slot || !requestId || !requestMid) return
    let content: Record<string, unknown> | undefined
    if (action === 'accept' && isForm) {
      const shaped = draftToContent(fields, draft)
      if (shaped.missing) {
        setHint(i18nT('pages.chat.elicitationCard.required_missing', { field: shaped.missing }))
        return
      }
      if (shaped.notWhole) {
        setHint(i18nT('pages.chat.elicitationCard.integer_not_whole', { field: shaped.notWhole }))
        return
      }
      if (shaped.unsafe) {
        setHint(i18nT('pages.chat.elicitationCard.integer_too_large', { field: shaped.unsafe }))
        return
      }
      content = shaped.content
    }
    setBusy(action)
    setHint(null)
    setRequestError(null)
    try {
      // `post` returns the raw Response; `j` is what turns a non-2xx into the
      // ApiError whose message carries the gateway's reason.
      // `request_mid` names the row this card was drawn from: the gateway
      // answers only the instance the person read, never a later ask that
      // reused the ACP request id.
      await apiTransport.j(await apiTransport.post(`/api/chat/slots/${encodeURIComponent(slot)}/elicitation`, {
        request_id: requestId,
        request_mid: requestMid,
        action,
        ...(content ? { content } : {}),
      }))
      // The gateway accepted the answer, so this row is decided now. The
      // resolution frame says the same, but a socket that drops during the
      // post would miss it, and a later refresh (whose bounded page leaves an
      // answered ask out) would then retire the card as "Not answered".
      store?.dispatch(resolveByApprovalId({ id: requestId, slot, decision: action, mid: requestMid, ...(content ? { answer: content } : {}) }))
    } catch (err) {
      setRequestError(answerErrorText(err))
    } finally {
      setBusy(null)
    }
  }

  return (
    // The transcript card recipe NoticeCard and RecoveryCard wear (an opaque
    // `bg-card` on a hairline ring), not a glass chip: over the transcript in
    // the light theme a translucent chip reads as a grey, switched-off slab,
    // and this card asks the person to act. The warn glow marks it pending.
    <div
      className={`w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-border bg-card text-text${resolved ? '' : ' approval-glow'}`}
      data-testid="elicitation-card"
    >
      <div className="flex flex-col gap-2 px-3.5 py-2.5">
        <div className="flex items-start gap-2">
          <MessageSquareMore size={14} className="text-warn shrink-0 mt-0.5" />
          <div className="flex flex-col gap-0.5 min-w-0 flex-1">
            <span className="text-[11px] text-muted uppercase tracking-wide">
              {asker
                ? (resolved ? i18nT('pages.chat.elicitationCard.title_named_resolved', { who: asker }) : i18nT('pages.chat.elicitationCard.title_named', { who: asker }))
                : (resolved ? i18nT('pages.chat.elicitationCard.title_resolved') : i18nT('pages.chat.elicitationCard.title'))}
            </span>
            <span className="text-[13px] text-text whitespace-pre-wrap break-words">{meta.message || message.content}</span>
          </div>
        </div>
        {!resolved && isForm && fields.length > 0 && (
          <div className="flex flex-col gap-2 pl-6">
            {fields.map(f => (
              <FieldControl
                key={f.name}
                field={f}
                value={draft[f.name]}
                disabled={busy !== null}
                onChange={v => setDraft(d => Object.assign(fieldDict<ElicitationValue>(), d, { [f.name]: v }))}
              />
            ))}
          </div>
        )}
        {!resolved && !isForm && meta.url && (
          <div className="pl-6">
            {host && (
              // The site's name before the link, so the person reads whose
              // page they are sent to before deciding to open it.
              <span className="block text-[12px] text-text font-medium" data-testid="elicitation-link-host">{host}</span>
            )}
            <a
              href={meta.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-[12px] text-accent hover:underline break-all"
            >
              <ExternalLink size={12} className="shrink-0" />{meta.url}
            </a>
            <div className="text-[11px] text-muted mt-1">{i18nT('pages.chat.elicitationCard.url_hint')}</div>
          </div>
        )}
        {hint && <div className="pl-6 text-[12px] text-muted" role="status">{hint}</div>}
        {requestError && (
          <div className="pl-6">
            {/* No hand-off: the form draft above is unsaved local state the
                hand-off's navigation would discard. */}
            <ErrorNotice variant="inline" message={requestError} onDismiss={() => setRequestError(null)} testId="elicitation-answer-error" />
          </div>
        )}
        <div className="flex items-center gap-1.5 pl-6">
          {resolved ? (
            <span className="inline-flex items-start gap-1 text-[12px] text-muted min-w-0">
              {resolved === 'accept'
                ? <CheckCircle size={12} className="shrink-0 mt-0.5" />
                : resolved === 'cancel'
                  // An ask nobody answered is not a refusal: a clock, not a ban.
                  ? <Clock size={12} className="shrink-0 mt-0.5" />
                  : <Ban size={12} className="shrink-0 mt-0.5" />}
              <span className="min-w-0 break-words">
                {resolvedLabel(resolved)}
                {resolved === 'accept' && answerSummary(fields, meta.answer) && (
                  <span className="text-text">{': '}{answerSummary(fields, meta.answer)}</span>
                )}
              </span>
            </span>
          ) : busy ? (
            <span className="inline-flex items-center gap-1 text-[12px] text-muted/60">
              <Loader2 size={12} className="animate-spin shrink-0" />{i18nT('pages.chat.elicitationCard.sending')}
            </span>
          ) : (
            <>
              <button type="button" onClick={() => void answer('accept')} className={approvalBtnClass} disabled={!slot}>
                <CheckCircle size={12} className="shrink-0" />
                {isForm ? i18nT('pages.chat.elicitationCard.submit') : i18nT('pages.chat.elicitationCard.done')}
              </button>
              <button type="button" onClick={() => void answer('decline')} className={`${approvalBtnClass} hover:!text-danger hover:!border-danger`} disabled={!slot}>
                <Ban size={12} className="shrink-0" />{i18nT('pages.chat.elicitationCard.decline')}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
})
