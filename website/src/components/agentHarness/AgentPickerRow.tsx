import type { ReactNode } from 'react'

/**
 * One row of a coding-agent picker: a radio, the harness name, its status on
 * the right, and — when the row is the chosen one — its detail opening directly
 * UNDER the row, inside the same outline, so it reads as that agent's panel
 * rather than a card detached at the foot of the list.
 *
 * Shared by first-run setup's "Use other coding agents" and Settings > Agent
 * Harness. Both are radio groups (`group` is the shared `name`), so the browser
 * supplies the keyboard: one tab stop per group, arrows move the check and the
 * focus together. The focus ring lives on this rounded outline, keyed to the
 * radio alone: drawn inset on the square row inside it, the ring's corners were
 * clipped by the rounding.
 *
 * Picking a row is never the same as USING the agent. `onSelect` opens the
 * detail; the detail's own button writes the config. That is why the radio is
 * checked by `selected` and not by which agent is configured.
 */
export function AgentPickerRow({
  id,
  group,
  label,
  selected,
  onSelect,
  icon,
  status,
  detailId,
  detailTestId,
  current = false,
  describedBy,
  children,
}: {
  /** The radio's value — the harness id. */
  id: string
  /** The radio group's `name`, shared by every row of one picker. */
  group: string
  /** The harness's display name; also the radio's accessible name. */
  label: string
  selected: boolean
  onSelect: () => void
  /** Decorative glyph before the name. */
  icon: ReactNode
  /** What renders at the row's right edge — the status badge, and any marks. */
  status: ReactNode
  detailId: string
  detailTestId: string
  /** Whether this harness is the configured one (`aria-current` on the radio). */
  current?: boolean
  /** Id of an element that describes the row's state in words. */
  describedBy?: string
  /** The detail; rendered only while the row is selected. */
  children?: ReactNode
}) {
  const showDetail = selected && children != null && children !== false
  return (
    <div
      className={`overflow-hidden rounded-lg border transition-colors has-[input:focus-visible]:ring-2 has-[input:focus-visible]:ring-[var(--accent)] ${
        selected ? 'border-accent/60' : 'border-border'
      }`}
    >
      <label
        className={`flex cursor-pointer items-center justify-between gap-3 px-3 py-2 transition-colors ${
          selected ? 'bg-accent-subtle' : 'bg-card hover:bg-bg-hover'
        }`}
      >
        <span className="flex min-w-0 items-center gap-2.5">
          <input
            type="radio"
            name={group}
            value={id}
            checked={selected}
            aria-label={label}
            aria-controls={selected ? detailId : undefined}
            aria-current={current ? 'true' : undefined}
            aria-describedby={describedBy}
            onChange={onSelect}
            /* focus-cue-ok: the cue is on the row's rounded outline above —
               `has-[input:focus-visible]:ring-2` on the wrapping div — so this
               radio's own outline would paint a second, corner-clipped ring
               inside it.
               Drawn by hand rather than native: a native radio under a dark
               `color-scheme` paints its unchecked circle as a filled grey disc,
               which reads as "checked" beside the one that is. A hollow ring
               that thickens into the accent when checked reads the same in
               every theme; forced-colors keeps the native control. */
            className="h-4 w-4 shrink-0 cursor-pointer appearance-none rounded-full border-[1.5px] border-[var(--muted)] bg-transparent transition-[border-width,border-color] checked:border-[5px] checked:border-[var(--accent)] focus-visible:outline-none forced-colors:appearance-auto"
          />
          {icon}
          <span className="truncate text-sm font-medium text-text-strong">{label}</span>
        </span>
        {status}
      </label>
      {showDetail && (
        <div id={detailId} className="space-y-3 border-t border-accent/30 bg-card p-4" data-testid={detailTestId}>
          {children}
        </div>
      )}
    </div>
  )
}
