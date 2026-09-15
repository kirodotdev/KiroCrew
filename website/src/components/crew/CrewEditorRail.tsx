/**
 * The crew editor's vertical rail.
 *
 * Renders whatever `useCrewEditorSections` returns, so a new surface never edits
 * this file. Group headings come from consecutive rows sharing a `group`, which
 * means the registry's ORDER defines the grouping and there is no second list to
 * keep in step with it.
 *
 * Keyboard behaviour is the WAI-ARIA tabs pattern under a roving tabindex, so the
 * whole rail is one Tab stop rather than one per surface — and the index maths is
 * reused from `Tablist` rather than reimplemented, because "skip a disabled
 * row, wrap at the ends" is exactly the same problem there.
 */
import { useRef } from 'react'
import { nextEnabledIndex, edgeEnabledIndex } from '../Tablist'
import type { CrewEditorSection, CrewPaneKey } from './crewEditorSections'

export interface CrewEditorRailProps {
  /** Wording for the unsaved-edit marker, used in each row's title and as the
   *  marker's own accessible name — the dot alone is invisible to a reader. */
  unsavedLabel: string
  /** Wording for the shared-storage dot, for the same reason. */
  sharedLabel: string
  sections: CrewEditorSection[]
  value: CrewPaneKey
  onChange: (key: CrewPaneKey) => void
  /** Names the rail for assistive tech — the dialog holds no other tablist, but
   *  an unlabelled one is still announced without saying what it navigates. */
  ariaLabel: string
  /** Prefix for the `aria-controls` id each row points at. */
  panelIdPrefix: string
}

export default function CrewEditorRail({
  sections, value, onChange, ariaLabel, panelIdPrefix, unsavedLabel, sharedLabel,
}: CrewEditorRailProps) {
  const refs = useRef<Array<HTMLButtonElement | null>>([])

  // `nextEnabledIndex` takes `TablistTab`s; only `key` and `disabled` are read,
  // and projecting avoids widening a shared component's signature for one caller.
  const nav = sections.map(s => ({ key: s.key, label: s.label, disabled: s.disabled }))

  const move = (to: number) => {
    const target = sections[to]
    if (!target || target.disabled) return
    onChange(target.key)
    // Focus follows selection so the next arrow press continues from the row the
    // user actually landed on.
    refs.current[to]?.focus()
  }

  const onKeyDown = (e: React.KeyboardEvent, i: number) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {
      e.preventDefault()
      move(nextEnabledIndex(nav, i, 1))
    } else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
      e.preventDefault()
      move(nextEnabledIndex(nav, i, -1))
    } else if (e.key === 'Home') {
      e.preventDefault()
      move(edgeEnabledIndex(nav, 'first'))
    } else if (e.key === 'End') {
      e.preventDefault()
      move(edgeEnabledIndex(nav, 'last'))
    }
  }

  let lastGroup = ''
  return (
    <div
      role="tablist"
      aria-orientation="vertical"
      aria-label={ariaLabel}
      // 200px from `sm` up, matching the Capabilities page's own tab sidebar:
      // narrower clipped the two-part "Workspace · Memory" row, and a truncated
      // navigation label is the one place truncation is not acceptable.
      //
      // At phone widths it lays across the top instead and scrolls sideways. A
      // 200px rail against a 420px viewport leaves the pane too narrow to hold a
      // select, and this dialog is reachable at 320px.
      className="flex shrink-0 gap-px overflow-x-auto border-b border-border bg-bg-accent p-2
                 sm:w-[200px] sm:flex-col sm:overflow-x-visible sm:border-b-0 sm:border-r"
    >
      {sections.map((s, i) => {
        const Icon = s.icon
        const isActive = s.key === value
        const isDisabled = s.disabled === true
        const heading = !s.foot && s.group && s.group !== lastGroup ? s.group : ''
        if (heading) lastGroup = s.group
        return (
          <div key={s.key} className={s.foot ? 'sm:mt-auto sm:pt-2' : undefined}>
            {heading && (
              <div className="hidden px-2 pb-1 pt-2.5 text-[10px] uppercase tracking-[0.08em]
                              text-muted-strong sm:block">
                {heading}
              </div>
            )}
            <button
              ref={el => {
                refs.current[i] = el
              }}
              type="button"
              role="tab"
              // Names the matching tabpanel via `aria-labelledby` — focus can be
              // handed to the panel (a diagram-node click does), and an unnamed
              // panel is announced as nothing but "tab panel".
              id={`${panelIdPrefix}-tab-${s.key}`}
              aria-selected={isActive}
              aria-disabled={isDisabled || undefined}
              // Roving tabindex. A disabled row stays reachable by arrow key so
              // its reason is readable, but never becomes the single Tab stop.
              tabIndex={isActive ? 0 : -1}
              title={[s.label, s.reason, s.shared ? sharedLabel : '', s.dirty ? unsavedLabel : '']
                .filter(Boolean).join(' — ')}
              // Only a selectable row controls a panel. A disabled row has no
              // pane, so pointing at an id that never exists would be a broken
              // reference rather than a relationship.
              {...(isDisabled ? {} : { 'aria-controls': `${panelIdPrefix}-${s.key}` })}
              data-testid={`crew-rail-${s.key}`}
              onClick={() => {
                if (!isDisabled) onChange(s.key)
              }}
              onKeyDown={e => onKeyDown(e, i)}
              className={[
                'flex w-full gap-2.5 rounded-md px-2 py-1.5 text-left text-[12.5px] focus-ring',
                // A disabled row shows its reason as text rather than only in a
                // `title`: arrow keys skip it and clicks are refused, so a hover
                // affordance would leave keyboard and touch users with a bare
                // greyed word. It stacks only from `sm` up, where the rail is a
                // vertical column with room to wrap. At phone widths the rail is a
                // horizontal scroller, so stacking there would turn it into a tall
                // blank band and push the pane below the fold.
                isDisabled ? 'items-center sm:flex-col sm:items-start' : 'items-center',
                'whitespace-nowrap',
                isDisabled ? 'cursor-default opacity-40' : 'hover:bg-bg-hover',
                isActive ? 'bg-bg-hover text-text-strong' : 'text-muted',
                s.foot ? 'text-danger' : '',
              ].join(' ')}
            >
              <span className="flex w-full min-w-0 items-center gap-2.5">
                <Icon className="lucide-inline h-[13px] w-[13px] shrink-0" aria-hidden="true" />
                <span className="min-w-0 flex-1 truncate">{s.label}</span>
              {/* The two dots differ by SHAPE, not only hue: a row can be both
                  shared and edited, and two 6px circles apart only in colour are
                  indistinguishable to a colour-blind reader. Unsaved is a ring,
                  shared is filled, and both carry an accessible name because a
                  dot says nothing to a screen reader. */}
              {s.dirty && (
                <span
                  className="h-2 w-2 shrink-0 rounded-full border-[1.5px] border-accent"
                  role="img"
                  aria-label={unsavedLabel}
                  data-testid={`crew-rail-dirty-${s.key}`}
                />
              )}
              {s.shared && (
                <span
                  className="h-1.5 w-1.5 shrink-0 rounded-full bg-info"
                  role="img"
                  aria-label={sharedLabel}
                  data-testid={`crew-rail-shared-${s.key}`}
                />
              )}
              {s.count && (
                <span className="shrink-0 text-[10px] tabular-nums text-muted-strong">{s.count}</span>
              )}
              </span>
              {isDisabled && s.reason && (
                <span
                  className="shrink-0 text-[10px] leading-snug text-muted-strong
                             sm:whitespace-normal sm:pl-[23px]"
                  data-testid={`crew-rail-reason-${s.key}`}
                >
                  {s.reason}
                </span>
              )}
            </button>
          </div>
        )
      })}
    </div>
  )
}
