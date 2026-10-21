/**
 * The crewmate editor's section rail (`CrewEditorNextDialog`'s sidebar).
 *
 * A segment host for find_ui: a section that is a registered UI location is
 * named in `segments` at the call site (`{ key, label, ...uiLocation(id) }`,
 * the same shape as SegmentedControl's `segments`), and this component draws
 * that entry's marker on the section's tab. The generator reads the marker and
 * its label from the call site, so the entry's `label` must be the label the
 * tab shows.
 */
import { type Ref } from 'react'
import { forwardUiLocation, UI_LOCATION_ATTR } from '../../uiLocations/uiLocation'

export interface CrewEditorNextRailSection<K extends string> {
  key: K
  label: string
}

/** A section that is a registered UI location; its marker lands on its tab. */
export interface CrewEditorNextRailSegment<K extends string> {
  key: K
  label: string
  'data-ui-location'?: string
  ref?: Ref<Element>
}

export interface CrewEditorNextRailProps<K extends string> {
  sections: readonly CrewEditorNextRailSection<K>[]
  value: K
  onSelect: (key: K) => void
  /** Every inactive section is disabled while a draft that would be lost on
   *  unmount is live, or a save is running. */
  locked: boolean
  ariaLabel: string
  /** The section drawn with the danger tone, set off from the group above. */
  dangerKey?: K
  segments?: readonly CrewEditorNextRailSegment<K>[]
}

export default function CrewEditorNextRail<K extends string>({
  sections, value, onSelect, locked, ariaLabel, dangerKey, segments,
}: CrewEditorNextRailProps<K>) {
  const markerOf = (key: K) =>
    forwardUiLocation<HTMLButtonElement>(segments?.find(seg => seg.key === key)?.[UI_LOCATION_ATTR])
  return (
    <div
      role="tablist"
      aria-orientation="vertical"
      aria-label={ariaLabel}
      className="flex shrink-0 gap-px overflow-x-auto border-b border-border bg-bg-accent p-2
                 sm:w-[180px] sm:flex-col sm:overflow-x-visible sm:border-b-0 sm:border-r"
    >
      {sections.map(s => {
        const isActive = s.key === value
        const isDanger = s.key === dangerKey
        return (
          <button
            key={s.key}
            type="button"
            role="tab"
            aria-selected={isActive}
            data-testid={`next-rail-${s.key}`}
            onClick={() => { if (!locked) onSelect(s.key) }}
            disabled={!isActive && locked}
            {...markerOf(s.key)}
            className={[
              'flex w-full rounded-md px-2.5 py-1.5 text-left text-[12.5px] focus-ring',
              // A locked rail says why it will not switch: dimmed, not-allowed
              // cursor, rather than a dead click.
              'disabled:opacity-50 disabled:cursor-not-allowed',
              // The danger section sits directly below the ordinary ones, set
              // off by a small top margin, and is danger-toned at rest so it
              // never reads as a routine pane.
              isDanger ? 'mt-2' : '',
              isActive
                ? (isDanger ? 'bg-danger-subtle text-danger-text' : 'bg-accent-subtle text-text')
                : (isDanger
                    ? 'text-danger-text hover:bg-danger-subtle'
                    : 'text-muted hover:bg-bg-hover hover:text-text'),
            ].join(' ')}
          >
            {s.label}
          </button>
        )
      })}
    </div>
  )
}
