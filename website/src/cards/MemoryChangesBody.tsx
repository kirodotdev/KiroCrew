/**
 * A `crewmate.memory` card's rows: each memory the bot asks to delete or edit,
 * with a checkbox. Unticking a row re-previews the card with that row in `skip`,
 * so the plan the gateway runs is always the rows that are ticked. The last
 * ticked row cannot be unticked: declining the card is how to change nothing.
 */
import { useId } from 'react'
import { Checkbox } from '../components/ui'
import { i18nT } from '../i18n/t'
import type { Card, CardChange } from '../api/cards'
import type { KindBodyProps } from './kindBodies'

/** The `field` of each row (`change_card_memory.ROW_FIELD`). */
export const MEMORY_ROW_FIELD = 'memory_change'

export function memoryRows(card: Pick<Card, 'kind' | 'changes'>): CardChange[] {
  if (card.kind !== 'crewmate.memory') return []
  return card.changes.filter(c => c.field === MEMORY_ROW_FIELD && typeof c.index === 'number')
}

/** The ticked rows' count, by what they do. */
export function memoryCounts(card: Pick<Card, 'kind' | 'changes'>): { deleted: number; edited: number } {
  const ticked = memoryRows(card).filter(r => r.included !== false)
  return {
    deleted: ticked.filter(r => r.action === 'delete').length,
    edited: ticked.filter(r => r.action === 'edit').length,
  }
}

function rowHeading(row: CardChange): string {
  const episode = row.memory_kind === 'episode'
  if (row.action === 'edit') {
    return i18nT(episode ? 'components.changeCards.memory_edit_episode' : 'components.changeCards.memory_edit_fact')
  }
  return i18nT(episode ? 'components.changeCards.memory_delete_episode' : 'components.changeCards.memory_delete_fact')
}

const text = (v: unknown) => (typeof v === 'string' ? v : v == null ? '' : JSON.stringify(v))

export function MemoryChangesBody({ card, disabled, preview }: KindBodyProps) {
  const id = useId()
  const rows = memoryRows(card)
  if (rows.length === 0) return null
  const editable = card.status === 'pending' || card.status === 'failed'
  const ticked = rows.filter(r => r.included !== false).length
  const skip = rows.filter(r => r.included === false).map(r => r.index as number)
  const toggle = (index: number, on: boolean) => {
    const next = on ? skip.filter(i => i !== index) : [...skip, index]
    void preview?.({ ...card.params, skip: next.sort((a, b) => a - b) })
  }
  return (
    <ul className="m-0 list-none p-0 py-1" data-testid="change-card-memory-rows">
      {rows.map(row => {
        const index = row.index as number
        const on = row.included !== false
        const boxId = `${id}-${index}`
        const lastTicked = on && ticked === 1
        return (
          <li key={index} className="flex items-start gap-2 border-t border-border py-2 first:border-t-0" data-testid="change-card-memory-row" data-included={on ? 'true' : 'false'}>
            <span className="flex min-h-11 shrink-0 items-start pt-[2px]">
              <Checkbox
                id={boxId}
                aria-label={rowHeading(row)}
                aria-describedby={`${boxId}-text`}
                data-testid="change-card-memory-box"
                checked={on}
                disabled={disabled || !editable || !preview || lastTicked}
                onChange={e => toggle(index, e.target.checked)}
              />
            </span>
            <div className={`min-w-0 flex-1 text-[13px] leading-5 ${on ? 'text-text' : 'text-muted'}`}>
              <label htmlFor={boxId} className="block cursor-pointer font-medium">{rowHeading(row)}</label>
              <div id={`${boxId}-text`}>
                {row.action === 'edit' ? (
                  <>
                    <p className="m-0 break-words text-muted">
                      <span className="mr-1 text-[12px]">{i18nT('components.changeCards.memory_now')}</span>
                      <span className={on ? 'line-through' : undefined}>{text(row.before)}</span>
                    </p>
                    <p className="m-0 break-words">
                      <span className="mr-1 text-[12px] text-muted">{i18nT('components.changeCards.memory_new')}</span>
                      {text(row.after)}
                    </p>
                  </>
                ) : (
                  <p className={`m-0 break-words text-muted${on ? ' line-through' : ''}`}>{text(row.before)}</p>
                )}
                <p className="m-0 mt-0.5 break-words text-[12px] text-muted" data-testid="change-card-memory-reason">{row.label}</p>
                {lastTicked && editable && preview && (
                  <p className="m-0 mt-0.5 text-[12px] text-muted" data-testid="change-card-memory-last">{i18nT('components.changeCards.memory_last_ticked')}</p>
                )}
              </div>
            </div>
          </li>
        )
      })}
    </ul>
  )
}
