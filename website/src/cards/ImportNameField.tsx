/**
 * The import card's name, editable before the press (rfc-crewmate-share.md §5.4:
 * the kind marks `name` editable). While a `crewmate.import` card is pending or
 * failed this field takes the place of its read-only Name row; every other
 * state keeps the row.
 *
 * The typed name is trimmed and re-previewed after a pause, so the plan the press
 * runs always carries the name on screen. The field holds the press while that
 * is not yet so: an empty name, a re-preview still to send or in flight, or a
 * name the gateway refused. A refusal of the name is worded here, never with
 * the gateway's text. Only the person's own typing is sent: a name the card
 * gets from elsewhere (another view of the same card, or the gateway keeping a
 * name other than the one sent) replaces a field the person has not touched.
 */
import { useEffect, useId, useState } from 'react'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import { errorCode, type Card } from '../api/cards'
import { CardRow } from './kindBodies'
import { problemFrom, type CardProblem } from './refusals'

/** How long an edited field waits before it is re-previewed. */
export const PREVIEW_DEBOUNCE_MS = 400

/** The longest Bot name the import card takes (`_CREWMATE_NAME_MAX`), counted in code points as the gateway counts it. */
export const CREWMATE_NAME_MAX = 24

/** The import card's name field shows, in place of its Name row. */
export function isImportNameEditable(card: Pick<Card, 'kind' | 'status'>): boolean {
  return card.kind === 'crewmate.import' && (card.status === 'pending' || card.status === 'failed')
}

export function ImportNameField({ card, disabled, preview, onProblem, onHeldChange }: {
  card: Card
  disabled: boolean
  /** Re-preview the card; rejects with the gateway's refusal. */
  preview: (params: Record<string, unknown>) => Promise<unknown>
  /** A refusal that is not about the name, for the card's own error line. */
  onProblem: (problem: CardProblem) => void
  /** Whether the press must wait (see the module comment). */
  onHeldChange: (held: boolean) => void
}) {
  const id = useId()
  const cardName = typeof card.params.name === 'string' ? card.params.name : ''
  const [typed, setTyped] = useState(cardName)
  // Typed since the field last sent a name, so the next pause sends it.
  const [edited, setEdited] = useState(false)
  // The name this field last sent, or the card's own name the field last took.
  const [sent, setSent] = useState(cardName)
  const [sending, setSending] = useState(false)
  // The name the gateway last refused, and how the field words it ('' when the
  // refusal was not about the name and the card's own error line says it).
  const [refused, setRefused] = useState<{ name: string; text: string } | null>(null)
  // A new revision is a fresh preview: a send ends once the card carries its
  // answer (the host may store the card after the request settles), any refusal
  // is checked again, and a name the field did not send takes the place of an
  // untouched field.
  const [revision, setRevision] = useState(card.revision)
  if (card.revision !== revision) {
    setRevision(card.revision)
    setSending(false)
    setRefused(null)
    if (!edited && cardName !== sent) {
      setTyped(cardName)
      setSent(cardName)
    }
  }
  const trimmed = typed.trim()

  useEffect(() => {
    if (!edited || sending) return
    if (trimmed === cardName) { setEdited(false); return }
    if (disabled || !trimmed) return
    const timer = window.setTimeout(() => {
      setEdited(false)
      setSent(trimmed)
      setSending(true)
      preview({ ...card.params, name: trimmed })
        .catch(e => {
          setSending(false)
          const code = errorCode(e)
          if (code === 'already_exists') setRefused({ name: trimmed, text: i18nT('components.changeCards.import_name_taken') })
          else if (code === 'invalid_param') setRefused({ name: trimmed, text: i18nT('components.changeCards.import_name_invalid') })
          else {
            setRefused({ name: trimmed, text: '' })
            onProblem(problemFrom(e))
          }
        })
    }, PREVIEW_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [edited, disabled, sending, trimmed, cardName, card.params, preview, onProblem])

  const shownRefusal = refused?.name === trimmed ? refused : null
  const held = !trimmed || trimmed !== cardName || sending || shownRefusal !== null
  useEffect(() => { onHeldChange(held) }, [held, onHeldChange])
  useEffect(() => () => onHeldChange(false), [onHeldChange])

  const refusalText = shownRefusal?.text ?? ''
  // An empty field is a hint, not a failure; only the gateway's refusal is an error.
  const describedBy = !trimmed ? `${id}-hint` : refusalText ? `${id}-error` : undefined
  return (
    <CardRow label={i18nT('components.changeCards.field_name')} id={`${id}-label`} testId="change-card-name">
      <input
        type="text"
        aria-labelledby={`${id}-label`}
        aria-invalid={describedBy ? true : undefined}
        aria-describedby={describedBy}
        autoComplete="off"
        spellCheck={false}
        value={typed}
        disabled={disabled}
        onChange={e => { setTyped(Array.from(e.target.value).slice(0, CREWMATE_NAME_MAX).join('')); setEdited(true) }}
        data-testid="change-card-name-input"
        className="min-h-11 w-full rounded-md border border-border bg-bg px-3 text-[13px] text-text focus:outline-none focus:border-accent disabled:opacity-50"
      />
      {!trimmed && (
        <p id={`${id}-hint`} className="mt-1 text-[12px] text-muted" data-testid="change-card-name-hint">{i18nT('components.changeCards.import_name_empty')}</p>
      )}
      {/* No hand-off: the name typed in this field is not on the card yet. */}
      <ErrorNotice id={`${id}-error`} className="mt-1" variant="inline" testId="change-card-name-error" message={refusalText} />
    </CardRow>
  )
}
