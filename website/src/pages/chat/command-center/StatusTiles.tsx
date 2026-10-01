import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { motion, useReducedMotion } from 'framer-motion'
import { ChevronDown } from 'lucide-react'
import { fmtNumber } from '../../../i18n/format'
import { Btn } from '../../../components/ui'
import type { CommandCenterModel } from './model'

export type Tile = 'progress' | 'blocked' | 'attention'

type Counts = Pick<CommandCenterModel, 'progress' | 'running' | 'blocked'> & { attention: { length: number } }

/** The three numbers a glance needs: how far, what is stuck, what waits on the
 * user. With `onSelect` each tile is a disclosure button for its own short list
 * (`aria-expanded`; the open tile names its region through `aria-controls`, and
 * the caller closes it when it is clicked again), grouped under the dock's
 * name; without it, a plain readout for the panel header. The tiles are ONE
 * segmented control expressing one setting — which list is open, or none — not
 * three peer actions: at most one is open, a click on the open one closes it,
 * and none of them performs anything. The row's action controls are the two
 * `controls` beside them. One component so both surfaces agree. The row wraps
 * rather than truncating: in a narrow column the tiles fall onto further rows
 * and the controls stay at the end of the first. */
export default function StatusTiles({ data, selected, onSelect, controls, idBase }: {
  data: Counts
  selected?: Tile | null
  onSelect?: (tile: Tile) => void
  /** Rendered after the tiles on the first row, e.g. the dock's hide/open buttons. */
  controls?: ReactNode
  /** Prefix for the `aria-controls` ids the dock's disclosure regions use. */
  idBase?: string
}) {
  const { t } = useTranslation()
  const reducedMotion = useReducedMotion()
  const attention = data.attention.length
  // The first tile is always Progress: without a plan its value is the running
  // count, so the label does not flip as a board appears or empties. That value
  // says what it counts in the cell itself ("2 running"), because a bare number
  // under Progress reads as work done; its title says why no done count shows.
  // Blocked and Needs you both read as "stuck", so each title names who acts:
  // the agent for a blocked run or item, the user for a question or approval.
  const tiles: { id: Tile; label: string; value: string; title: string; tone: string }[] = [
    data.progress
      ? { id: 'progress', label: t('commandCenter.tile_progress'), value: `${fmtNumber(data.progress.done)}/${fmtNumber(data.progress.total)}`,
        title: t('commandCenter.progress', { done: fmtNumber(data.progress.done), total: fmtNumber(data.progress.total) }), tone: 'text-text-strong' }
      : { id: 'progress', label: t('commandCenter.tile_progress'), value: t('commandCenter.running_count', { countText: fmtNumber(data.running) }),
        title: t('commandCenter.running_title'), tone: 'text-text-strong' },
    { id: 'blocked', label: t('commandCenter.blocked'), value: fmtNumber(data.blocked), title: t('commandCenter.blocked_title'), tone: data.blocked > 0 ? 'text-warn' : 'text-muted' },
    { id: 'attention', label: t('commandCenter.attention_filter'), value: fmtNumber(attention), title: t('commandCenter.attention_title'), tone: attention > 0 ? 'text-danger' : 'text-muted' },
  ]
  const cells = tiles.map(tile => {
      const label = <span className="text-[11px] text-muted truncate">{tile.label}</span>
      const value = <span className={`inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap font-mono tabular-nums text-[15px] font-semibold leading-none ${tile.tone}`}>
        {tile.id === 'attention' && attention > 0 && <motion.span aria-hidden="true" className="inline-block w-1.5 h-1.5 rounded-full bg-danger"
          animate={reducedMotion ? undefined : { opacity: [1, 0.35, 1] }} transition={{ duration: 1.4, repeat: 3 }} />}
        {tile.value}
      </span>
      const active = selected === tile.id
      // Only the dock's tiles wear a card: they are the disclosure buttons, and
      // the chevron at the trailing edge says so before the first click (it
      // turns while the list is open). The panel's readout is plain text on the
      // panel background, so it does not invite the click the dock just taught.
      return onSelect
        ? <Btn key={tile.id} aria-expanded={active} aria-controls={idBase && active ? `${idBase}-${tile.id}` : undefined} title={tile.title}
          onClick={() => onSelect(tile.id)} className={`flex-1 basis-[7.5rem] min-w-0 flex items-center justify-between gap-2 rounded-lg border bg-card px-2.5 py-1.5 text-left ${active ? 'border-accent' : 'border-border'}`}>
          {label}
          <span className="inline-flex shrink-0 items-center gap-1.5">
            {value}
            <ChevronDown size={12} aria-hidden="true" className={`text-muted transition-transform ${active ? 'rotate-180' : ''}`} />
          </span>
        </Btn>
        : <div key={tile.id} title={tile.title} className="flex-1 basis-[7.5rem] min-w-0 flex items-center justify-between gap-2 px-1 py-1" data-testid={`status-tile-${tile.id}`}>{label}{value}</div>
  })
  return <div className="flex flex-wrap items-start gap-1.5" aria-live="polite" data-testid="status-tiles">
    {onSelect
      ? <div role="group" aria-label={t('commandCenter.title')} className="flex flex-wrap gap-1.5 min-w-0 flex-1">{cells}</div>
      : cells}
    {controls}
  </div>
}
