/**
 * Per-kind card bodies: the parts of a card that differ by kind. Everything the
 * gateway computed (changes, scope, risk, reason) is drawn by the shell; these
 * add only what a kind needs on top — a schedule's time zone and
 * real next run (or, for a one-shot, the one time it runs), a note that only one crewmate changes.
 */
import { useId } from 'react'
import { Checkbox } from '../components/ui'
import { i18nT } from '../i18n/t'
import { fmtDateTime, fmtUnit } from '../i18n/format'
import type { Card, CardChange } from '../api/cards'
import { cardTimezone, isOneShot, oneShotWhen } from './oneShot'
import { useCardAuthor } from './cardAuthor'

export interface KindBodyProps {
  card: Card
  disabled: boolean
  /** Re-preview the card with new params (an editable field the person changed). */
  preview?: (params: Record<string, unknown>) => unknown
}

/** The `field` of a goal card's Heartbeat checkbox row (`GOAL_HEARTBEAT_FIELD`). */
export const GOAL_HEARTBEAT_FIELD = 'heartbeat_offer'

/** A goal card's Heartbeat checkbox row, when the gateway offered one. */
export function goalHeartbeatOffer(card: Pick<Card, 'kind' | 'changes'>): CardChange | null {
  if (card.kind !== 'crewmate.goal') return null
  return card.changes.find(c => c.field === GOAL_HEARTBEAT_FIELD) ?? null
}

/** "30 min" / "1 hr" in the active locale, the Schedules tab's wording. */
const intervalText = (minutes: number) =>
  minutes < 60 ? fmtUnit(minutes, 'minute', { unitDisplay: 'short' }) : fmtUnit(minutes / 60, 'hour', { unitDisplay: 'short' })

/**
 * A key-value row of the card grid; dividers come from the grid parent. It adds
 * no horizontal inset, so its label starts on the card's one left edge. A
 * narrow card stacks the label above its value; a wide one gives labels a fixed
 * column wide enough that a two-word label does not wrap, values on the label's
 * baseline.
 */
export function CardRow({ label, children, id, testId }: { label: string; children: React.ReactNode; id?: string; testId?: string }) {
  return (
    <div data-card-row="" data-testid={testId} className="grid grid-cols-1 @min-[480px]:grid-cols-[8.5rem_minmax(0,1fr)] gap-x-4 gap-y-0.5 items-baseline py-2">
      <div className="min-w-0 text-[12px] leading-5 text-muted break-words" id={id}>{label}</div>
      <div className="min-w-0 text-[13px] leading-5 text-text break-words">{children}</div>
    </div>
  )
}

export function ScheduleBody({ card }: KindBodyProps) {
  // A change row that already moves the time zone shows it; a second, static
  // row would repeat the new value under the same label.
  const tzChanged = card.changes.some(c => c.field === 'timezone')
  const tz = cardTimezone(card)
  if (isOneShot(card)) {
    // A one-shot has no recurrence and no "next" run: one row says when it
    // runs, unless the change rows already carry it.
    const shown = card.changes.some(c => c.field === 'at')
    const at = card.params.at
    const when = oneShotWhen(card) ?? (typeof at === 'string' && at.trim() ? at : null)
    return (
      <>
        {tz && !tzChanged && <CardRow label={i18nT('components.changeCards.schedule_timezone')}>{tz}</CardRow>}
        {!shown && when && (
          <CardRow label={i18nT('components.changeCards.schedule_once')}>
            <span data-testid="change-card-once">{when}</span>
          </CardRow>
        )}
      </>
    )
  }
  return (
    <>
      {tz && !tzChanged && <CardRow label={i18nT('components.changeCards.schedule_timezone')}>{tz}</CardRow>}
      {card.next_run_at != null && (
        <CardRow label={i18nT('components.changeCards.schedule_next_run')}>
          {fmtDateTime(card.next_run_at, tz ? { timeZone: tz } : undefined)}
        </CardRow>
      )}
    </>
  )
}

export function OneCrewmateBody({ card }: KindBodyProps) {
  // The gateway's scope wins when it sends one; otherwise say what is true of
  // these kinds by construction.
  if (card.scope) return null
  return <p className="m-0 py-2 text-[12px] leading-5 text-muted">{i18nT('components.changeCards.scope_only_this')}</p>
}

/**
 * A goal card's Heartbeat option: while the mate's Heartbeat is off and was
 * never declined, a checkbox (checked by default) that turns it on with the
 * goal. It is an option of the Set goal press, so the card draws it after the
 * reason, just above the buttons (`CardKindSpec.Option`). Flipping it
 * re-previews the card with `heartbeat`, so the plan the gateway runs is always
 * the one the box shows.
 */
export function GoalHeartbeatOption({ card, disabled, preview }: KindBodyProps) {
  const id = useId()
  const offer = goalHeartbeatOffer(card)
  if (!offer) return null
  const minutes = typeof offer.interval === 'number' ? offer.interval : 60
  const editable = card.status === 'pending' || card.status === 'failed'
  return (
    <div className="mt-2" data-testid="change-card-goal-heartbeat">
      {/* One unit, the SettingsToggle label/description pattern: the note sits
          mt-0.5 under the label text, inside the same 44px tap target. */}
      <label htmlFor={`${id}-heartbeat`} className="flex min-h-11 items-start gap-2 cursor-pointer text-[13px] leading-5 text-text">
        {/* The shared Checkbox at its own size, centred on the label's first line. */}
        <span className="flex h-5 shrink-0 items-center">
          <Checkbox
            id={`${id}-heartbeat`}
            aria-label={i18nT('components.changeCards.goal_heartbeat_label')}
            data-testid="change-card-goal-heartbeat-box"
            checked={offer.after === true}
            disabled={disabled || !editable || !preview}
            aria-describedby={`${id}-heartbeat-note`}
            onChange={e => { void preview?.({ ...card.params, heartbeat: e.target.checked }) }}
          />
        </span>
        <span className="min-w-0">
          <span className="block">{i18nT('components.changeCards.goal_heartbeat_label')}</span>
          <span id={`${id}-heartbeat-note`} className="mt-0.5 block text-[12px] leading-5 text-muted" data-testid="change-card-goal-heartbeat-note">
            {i18nT('components.changeCards.goal_heartbeat_note', { interval: intervalText(minutes) })}
          </span>
        </span>
      </label>
    </div>
  )
}

/**
 * The values a plan's `user` fills need, typed by the person (a secret's value).
 * They live only in the card's local state, are written only into the declared
 * fill field of the step that names them, and are cleared once that step is
 * sent. Never part of a preview, the cache or a log.
 */
export function UserInputs({ fields, values, onChange, disabled }: {
  fields: string[]
  values: Record<string, string>
  onChange: (field: string, v: string) => void
  disabled: boolean
}) {
  const id = useId()
  const author = useCardAuthor()
  return (
    <>
      {fields.map(field => (
        <div key={field} className="flex flex-col gap-1 py-2">
          <label htmlFor={`${id}-${field}`} className="text-[12px] text-muted">{i18nT('components.changeCards.secret_label')}</label>
          <input
            id={`${id}-${field}`}
            aria-label={i18nT('components.changeCards.secret_label')}
            type="password"
            autoComplete="new-password"
            spellCheck={false}
            value={values[field] ?? ''}
            disabled={disabled}
            onChange={e => onChange(field, e.target.value)}
            data-testid={`change-card-input-${field}`}
            className="min-h-11 w-full rounded-md border border-border bg-bg px-3 text-[13px] text-text focus:outline-none focus:border-accent disabled:opacity-50"
          />
          <p className="m-0 text-[12px] text-muted">{i18nT('components.changeCards.secret_hint', { name: author })}</p>
        </div>
      ))}
    </>
  )
}

export function NoBody() {
  return null
}
