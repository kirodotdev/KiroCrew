import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from './ui/select'
import { NativeSelect, NativeSelectOption } from './ui/native-select'
import { useIsTouchDevice } from '../hooks/useIsTouchDevice'

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
 *
 * ON TOUCH DEVICES THIS RENDERS `ui/native-select` INSTEAD of the Radix popup,
 * because the popup's list is a `position:fixed` overflow scroller inside a
 * scroll lock and a finger drag does not reliably move it on iOS Safari — a
 * 40-row list like the STT language codes is then unreachable past the first
 * screenful.
 *
 * shadcn ships the two as separate components and leaves the choice to each
 * call site. The choice is made HERE instead, on purpose: this wrapper is
 * already the single choke point every dropdown in Settings goes through, and
 * ~30 call sites each having to remember the touch case is ~30 chances to
 * reintroduce the bug. Nothing routed through here needs styled option rows,
 * which is the one thing the native list cannot do; a caller that ever does can
 * be given an opt-out then rather than now.
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
  /** Forwarded to the trigger so a caption's `<label htmlFor>` can name it
   *  (SettingsField pairs this with its label association). Optional: the
   *  standalone dropdowns keep naming themselves via aria-label. */
  id?: string
  /** Extra classes for the TRIGGER. For a caller whose surrounding rows are
   *  denser than the default `px-3 py-2 text-sm` — the dev config table runs at
   *  `h-7 text-[13px]`, and a taller control there would change every row's
   *  height. Merged after the defaults, so it wins. */
  className?: string
  'aria-label'?: string
}

export default function SimpleSelect({ options, optionLabels, value, onChange, action, clearLabel, triggerFallback, disabled, style, id, className, 'aria-label': ariaLabel }: SimpleSelectProps) {
  const isTouch = useIsTouchDevice()
  const toRadix = (v: string) => (v === '' ? EMPTY_VALUE_SENTINEL : v)
  const fromRadix = (v: string) => (v === EMPTY_VALUE_SENTINEL ? '' : v)
  // '' is selectable only when the options include it or a clearLabel row exists;
  // otherwise an empty value means "nothing selected" and the trigger shows the fallback.
  const emptySelectable = clearLabel !== undefined || options.includes('')
  const selectable = (v: string) => options.includes(v) || (v === '' && emptySelectable)
  const label = (opt: string, i: number) =>
    opt === '' ? (clearLabel ?? optionLabels?.[i] ?? '—') : (optionLabels?.[i] ?? opt)

  if (isTouch) {
    // A value with no matching option (a legacy or provider-dropped setting) has
    // nowhere to live in a native list, so it gets a row of its own — otherwise
    // the browser would silently display the FIRST option and the row would read
    // as if that were the saved setting.
    const unmatched = !selectable(value)
    return (
      <NativeSelect
        id={id}
        aria-label={ariaLabel}
        disabled={disabled}
        // On the Radix path `style` lands on the wrapper div; here it lands on the
        // control itself, which is where a native form control expects it. No
        // caller passes `style` today, so the two never diverge in practice.
        style={style}
        className={className}
        value={toRadix(value)}
        onChange={e => {
          const v = e.target.value
          if (v === ACTION_SENTINEL) { action?.onSelect(); return }
          onChange(fromRadix(v))
        }}
      >
        {unmatched && (
          // The row carries the CURRENT value rather than a sentinel, which is what
          // makes it need no guard: an engine that ignores `hidden` and lets the
          // user pick it reports the value that was already set, so the setting
          // cannot silently change. `hidden` rather than `disabled` because a
          // disabled option renders GREY while it is the displayed one, which reads
          // as the whole control being disabled — and hidden also keeps the slot out
          // of the picker, so the list offers only values that can be chosen.
          <NativeSelectOption value={toRadix(value)} hidden>
            {triggerFallback ?? clearLabel ?? (value || '—')}
          </NativeSelectOption>
        )}
        {action && <NativeSelectOption value={ACTION_SENTINEL}>{action.label}</NativeSelectOption>}
        {clearLabel !== undefined && !options.includes('') && (
          <NativeSelectOption value={EMPTY_VALUE_SENTINEL}>{clearLabel}</NativeSelectOption>
        )}
        {options.map((opt, i) => (
          <NativeSelectOption key={opt} value={toRadix(opt)}>{label(opt, i)}</NativeSelectOption>
        ))}
      </NativeSelect>
    )
  }

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
        <SelectTrigger id={id} aria-label={ariaLabel} className={className}>
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
              {label(opt, i)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}
