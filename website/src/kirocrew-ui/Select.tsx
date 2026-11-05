import SimpleSelect from '../components/SimpleSelect'

/**
 * The props an app's inline dropdown may pass. A deliberate subset of the host's
 * internal select: the icon, badge, action-row and guide-marker props are host
 * plumbing whose shapes are not frozen, and every prop published here is a
 * contract an installed app's bundle can depend on.
 */
export interface SelectProps {
  /** Option values, in display order. */
  options: string[]
  /** Display labels in option order; an option without one shows its value. */
  optionLabels?: string[]
  value: string
  onChange: (value: string) => void
  /** A selectable top row that sets `''`, shown in the trigger while the value is `''`. */
  clearLabel?: string
  disabled?: boolean
  'aria-label'?: string
  /** Full trigger text for a control whose visible label may be cut off. */
  title?: string
  /** Extra classes for the trigger, merged after the defaults. */
  className?: string
}

/**
 * A themed dropdown for inline use: the host's own select, so it matches every
 * dashboard dropdown and renders the native list on touch devices.
 */
export default function Select({
  options,
  optionLabels,
  value,
  onChange,
  clearLabel,
  disabled,
  'aria-label': ariaLabel,
  title,
  className,
}: SelectProps) {
  return (
    <SimpleSelect
      options={options}
      optionLabels={optionLabels}
      value={value}
      onChange={onChange}
      clearLabel={clearLabel}
      disabled={disabled}
      aria-label={ariaLabel}
      title={title}
      className={className}
    />
  )
}
