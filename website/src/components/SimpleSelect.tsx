import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from './ui/select'
import { NativeSelect, NativeSelectOption } from './ui/native-select'
import { useIsTouchDevice } from '../hooks/useIsTouchDevice'
import { SourceBadge } from './ui'

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
 * reintroduce the bug. A caller that wants styled option rows — the one thing
 * the native list cannot do — asks for them with `optionBadges`, and gets the
 * text form on touch instead.
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
  /** Show `optionLabels` only in the open list, keeping the collapsed trigger to
   *  the bare option value. For a label that distinguishes entries while you are
   *  CHOOSING (e.g. a template's source) but only repeats itself once chosen —
   *  the agent-template pane states the same fact on the line below its dropdown.
   *  Honoured on the Radix path only: on touch the trigger IS the selected
   *  `<option>`, so the label is unavoidably visible there. */
  labelsInListOnly?: boolean
  /** Per-option trailing badge in the open list (same order as `options`); leave
   *  an entry undefined for a row with nothing to say. `source` picks the colour,
   *  `label` is the already-translated text.
   *
   *  Radix path only. On touch the row IS a native `<option>`, which holds text
   *  and nothing else, so the same fact is appended as `name — label` there. The
   *  divergence is deliberate: each path gets the best form it can render. */
  optionBadges?: ({ label: string; source: string } | undefined)[]
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
  /** Extra classes for the open LIST (Radix SelectContent). The default panel is
   *  exactly the trigger's width; a caller whose trigger deliberately hugs the
   *  selected value (the template pane's header) passes a `min-w-*` here so the
   *  rows and their badges still render whole. */
  contentClassName?: string
  'aria-label'?: string
}

export default function SimpleSelect({ options, optionLabels, value, onChange, action, clearLabel, triggerFallback, labelsInListOnly, optionBadges, disabled, style, id, className, contentClassName, 'aria-label': ariaLabel }: SimpleSelectProps) {
  const isTouch = useIsTouchDevice()
  const toRadix = (v: string) => (v === '' ? EMPTY_VALUE_SENTINEL : v)
  const fromRadix = (v: string) => (v === EMPTY_VALUE_SENTINEL ? '' : v)
  // '' is selectable only when the options include it or a clearLabel row exists;
  // otherwise an empty value means "nothing selected" and the trigger shows the fallback.
  const emptySelectable = clearLabel !== undefined || options.includes('')
  const selectable = (v: string) => options.includes(v) || (v === '' && emptySelectable)
  const label = (opt: string, i: number) => {
    if (opt === '') return clearLabel ?? optionLabels?.[i] ?? '—'
    if (optionLabels?.[i] !== undefined) return optionLabels[i]
    // A badge cannot render as text, so spell the same fact out for the paths
    // that only take a string: the native list and the collapsed trigger.
    const badge = optionBadges?.[i]
    return badge ? `${opt} — ${badge.label}` : opt
  }

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
        // `style` lands on the WRAPPER on both paths. It is layout intent (a flex
        // basis, a min-width), and on this path the `<select>` is `w-full` inside
        // the wrapper — a flex rule placed on it is simply inert.
        wrapperStyle={style}
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
          <SelectValue placeholder={triggerFallback ?? clearLabel ?? (value || '—')}>
            {/* Children override the selected item's text. Passed only when there
                IS a selectable non-empty value, so an unset control still falls
                through to the placeholder. */}
            {labelsInListOnly && value !== '' && selectable(value) ? value : undefined}
          </SelectValue>
        </SelectTrigger>
        <SelectContent className={contentClassName}>
          {action && (
            <SelectItem value={ACTION_SENTINEL} className="text-accent data-[state=checked]:bg-transparent">
              {action.label}
            </SelectItem>
          )}
          {clearLabel !== undefined && !options.includes('') && (
            <SelectItem value={EMPTY_VALUE_SENTINEL}>{clearLabel}</SelectItem>
          )}
          {options.map((opt, i) => {
            const badge = opt === '' ? undefined : optionBadges?.[i]
            return (
              <SelectItem key={opt} value={toRadix(opt)}>
                {badge ? (
                  <span className="inline-flex items-center gap-1.5">
                    {opt}
                    {/* aria-hidden: the option's accessible name must stay the bare
                        template name — the badge text otherwise joins it and breaks
                        exact-name lookups (locators, SR "select kirocrew"). */}
                    <span aria-hidden="true" className="contents">
                      <SourceBadge source={badge.source} tone="neutral">{badge.label}</SourceBadge>
                    </span>
                  </span>
                ) : label(opt, i)}
              </SelectItem>
            )
          })}
        </SelectContent>
      </Select>
    </div>
  )
}
