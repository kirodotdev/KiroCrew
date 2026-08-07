import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from './ui/select'

/**
 * Thin Radix Select wrapper with the retired StyledSelect's props shape.
 *
 * Shared by SettingsSelect (settings pages) and the standalone dropdowns in
 * PublishHub / ArtifactDeployPage / KiroCrewAgentsPage. Holds the sentinel
 * plumbing in one place:
 *
 * - Radix reserves value '' for "no selection", but callers legitimately use
 *   '' as a real option value (mic "System default", per-row deploy profile).
 *   '' maps to EMPTY_VALUE_SENTINEL on the way in and back on the way out.
 * - `clearLabel` reproduces StyledSelect's selectable placeholder row: a top
 *   item that sets '' and renders in the trigger while value is ''.
 * - `action` reproduces the "+ New workspace…" row: selecting it fires
 *   action.onSelect instead of onChange.
 */

const EMPTY_VALUE_SENTINEL = '\u0000simple-select-empty'
const ACTION_SENTINEL = '\u0000simple-select-action'

export interface SimpleSelectProps {
  options: string[]
  /** Optional display labels for each option (same order as options). Falls back to the option value. */
  optionLabels?: string[]
  value: string
  onChange: (value: string) => void
  /** Optional action at top of dropdown (e.g. "+ New workspace…"). Fires onSelect instead of onChange. */
  action?: { label: string; onSelect: () => void }
  /** Selectable top row that clears the value to '' and shows in the trigger while value is ''. */
  clearLabel?: string
  /** Trigger text when the current value has no matching option (legacy values). */
  triggerFallback?: string
  disabled?: boolean
  style?: React.CSSProperties
  /** Extra classes for the TRIGGER. For a caller whose surrounding rows are
   *  denser than the default `px-3 py-2 text-sm` — the dev config table runs at
   *  `h-7 text-[13px]`, and a taller control there would change every row's
   *  height. Merged after the defaults, so it wins. */
  className?: string
  'aria-label'?: string
}

export default function SimpleSelect({ options, optionLabels, value, onChange, action, clearLabel, triggerFallback, disabled, style, className, 'aria-label': ariaLabel }: SimpleSelectProps) {
  const toRadix = (v: string) => (v === '' ? EMPTY_VALUE_SENTINEL : v)
  const fromRadix = (v: string) => (v === EMPTY_VALUE_SENTINEL ? '' : v)
  // '' is selectable only when the options include it or a clearLabel row exists;
  // otherwise an empty value means "nothing selected" and the trigger shows the fallback.
  const emptySelectable = clearLabel !== undefined || options.includes('')
  const selectable = (v: string) => options.includes(v) || (v === '' && emptySelectable)
  return (
    <div style={style}>
      <Select
        value={selectable(value) ? toRadix(value) : ''}
        onValueChange={v => {
          if (v === ACTION_SENTINEL) { action?.onSelect(); return }
          onChange(fromRadix(v))
        }}
        disabled={disabled}
      >
        <SelectTrigger aria-label={ariaLabel} className={className}>
          <SelectValue placeholder={triggerFallback ?? clearLabel ?? (value || '—')} />
        </SelectTrigger>
        <SelectContent>
          {action && (
            <SelectItem value={ACTION_SENTINEL} className="text-accent data-[state=checked]:bg-transparent">
              {action.label}
            </SelectItem>
          )}
          {clearLabel !== undefined && !options.includes('') && (
            <SelectItem value={EMPTY_VALUE_SENTINEL}>{clearLabel}</SelectItem>
          )}
          {options.map((opt, i) => (
            <SelectItem key={opt} value={toRadix(opt)}>
              {opt === '' ? (clearLabel ?? optionLabels?.[i] ?? '—') : (optionLabels?.[i] ?? opt)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}
