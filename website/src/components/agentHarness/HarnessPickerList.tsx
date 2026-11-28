import type { ReactNode } from 'react'

import { AgentPickerRow } from './AgentPickerRow'

/** What one row shows beside the shared parts (radio, name, outline, detail slot). */
export interface HarnessRowParts {
  icon: ReactNode
  status: ReactNode
  /** Rendered only while the row is selected. */
  detail?: ReactNode
  current?: boolean
  describedBy?: string
}

/**
 * The one harness radio list, used by Settings > Agent Harness and first-run
 * setup. The ids, their order and their names come from `harnessList`; each
 * screen passes only what differs per row (`row`). First-run renders it once,
 * under "Bring your harness": Kiro CLI, then the rest.
 */
export function HarnessPickerList({
  ids,
  group,
  legend,
  selectedId,
  onSelect,
  label,
  row,
  detailId,
  detailTestId,
  className = 'mx-0 min-w-0 space-y-2 border-none p-0',
  id,
}: {
  ids: string[]
  group: string
  legend: string
  selectedId: string
  onSelect: (id: string) => void
  label: (id: string) => string
  row: (id: string, selected: boolean) => HarnessRowParts
  detailId: (id: string) => string
  detailTestId: string | ((id: string) => string)
  className?: string
  id?: string
}) {
  return (
    <fieldset id={id} className={className}>
      <legend className="sr-only">{legend}</legend>
      {ids.map(value => {
        const selected = value === selectedId
        const parts = row(value, selected)
        return (
          <AgentPickerRow
            key={value}
            id={value}
            group={group}
            label={label(value)}
            selected={selected}
            onSelect={() => onSelect(value)}
            icon={parts.icon}
            current={parts.current}
            describedBy={parts.describedBy}
            status={parts.status}
            detailId={detailId(value)}
            detailTestId={typeof detailTestId === 'function' ? detailTestId(value) : detailTestId}
          >
            {parts.detail}
          </AgentPickerRow>
        )
      })}
    </fieldset>
  )
}
