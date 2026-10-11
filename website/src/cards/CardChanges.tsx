/**
 * What a card changes, as the gateway computed it, in ONE form per row: a list
 * edit is its added / removed items as +/− chips, a scalar is old → new. A
 * list row also carries the whole list before and after; printing that beside
 * the chips said the same change twice, so the chips alone stand for it.
 */
import { i18nT } from '../i18n/t'
import { fmtList } from '../i18n/format'
import type { CardChange } from '../api/cards'
import { CardRow } from './kindBodies'
import { fmtCardCron } from './scheduleText'

/** Localized labels for the card fields the gateway names in `field`. */
export const FIELD_LABELS: Record<string, () => string> = {
  value: () => i18nT('components.changeCards.field_value'),
  name: () => i18nT('components.changeCards.field_name'),
  message: () => i18nT('components.changeCards.field_message'),
  cron_expr: () => i18nT('components.changeCards.field_cron_expr'),
  at: () => i18nT('components.changeCards.field_at'),
  timezone: () => i18nT('components.changeCards.field_timezone'),
  goal: () => i18nT('components.changeCards.field_goal'),
  auto_approve: () => i18nT('components.changeCards.field_auto_approve'),
  enabled: () => i18nT('components.changeCards.field_enabled'),
}

/** A row's label: the localized field name when known, else the gateway's. */
export function changeLabel(c: CardChange): string {
  return (c.field && FIELD_LABELS[c.field]?.()) || c.label
}

/** A row's value as the Schedule page would show it: cron reads as a clock and
 *  days, plus the recurrence the card must always state. */
function changeValue(c: CardChange, v: unknown): string {
  return c.field === 'cron_expr' && typeof v === 'string' ? fmtCardCron(v) : formatCardValue(v)
}

export function formatCardValue(v: unknown): string {
  if (v === undefined || v === null || v === '') return i18nT('components.changeCards.empty_value')
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  if (Array.isArray(v) && v.every(item => typeof item === 'string')) {
    // A string-list setting (e.g. hidden models): read as a list, not JSON.
    return v.length ? fmtList(v as string[], { style: 'narrow' }) : i18nT('components.changeCards.empty_value')
  }
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

/** True when the row is a list edit: its items, not its whole list, are the change. */
export function isListChange(c: CardChange): boolean {
  return !!(c.add?.length || c.remove?.length)
}

const CHIP = 'inline-flex max-w-full items-baseline rounded px-1.5 text-[12px] leading-5 text-text [overflow-wrap:anywhere]'

export function CardChanges({ changes }: { changes: CardChange[] }) {
  return (
    <>
      {changes.map((c, i) => (
        <CardRow key={`${c.label}-${i}`} label={changeLabel(c)}>
          {isListChange(c) ? (
            <ul className="m-0 flex list-none flex-wrap gap-1.5 p-0" data-testid="change-card-items">
              {(c.add ?? []).map((v, j) => (
                <li key={`a${j}`} className={`${CHIP} bg-ok-subtle`}>
                  <span aria-hidden="true">+&nbsp;</span><span className="sr-only">{i18nT('components.changeCards.added')} </span>{formatCardValue(v)}
                </li>
              ))}
              {(c.remove ?? []).map((v, j) => (
                <li key={`r${j}`} className={`${CHIP} bg-danger-subtle`}>
                  <span aria-hidden="true">−&nbsp;</span><span className="sr-only">{i18nT('components.changeCards.removed')} </span>{formatCardValue(v)}
                </li>
              ))}
            </ul>
          ) : (c.before !== undefined || c.after !== undefined) && (
            <span className="inline-flex max-w-full flex-wrap items-baseline gap-x-1.5" data-testid="change-card-value">
              {c.before !== undefined && <span className="text-muted [overflow-wrap:anywhere]">{changeValue(c, c.before)}</span>}
              {c.before !== undefined && <span aria-hidden="true" className="text-muted">→</span>}
              {c.before !== undefined && <span className="sr-only">{i18nT('components.changeCards.becomes')}</span>}
              {c.after !== undefined && <span className="font-medium [overflow-wrap:anywhere]">{changeValue(c, c.after)}</span>}
            </span>
          )}
        </CardRow>
      ))}
    </>
  )
}
