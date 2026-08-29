// A select with a built-in "Other (custom)..." escape hatch. Behavioural
// contract mirrors an older internal version of this app's
// ``DropdownWithOther``; the key adaptations for Kiro Crew are:
//
// * Options carry both a canonical ``value`` and a localized ``label``,
//   so onChange emits the wire value (e.g. ``internalTeam``) while the
//   user sees the i18n-backed display string (e.g. "Internal team").
//   The old internal version stored labels as values because its
//   options came from a backend-served list where the two coincided;
//   Kiro Crew keeps the enum-keyed convention already in use across
//   the app.
// * The ``__other__`` sentinel string is reserved on the wire: a value
//   equal to it is coerced to empty rather than round-tripped back to
//   the parent. This defends against a corrupt settings.json or hand-
//   edited default that could otherwise strand the field on the
//   sentinel.
// * Picking "Other" from the select does NOT fire onChange -- it only
//   opens the custom input, preserving whatever value was previously
//   set so a mis-click does not destroy a persisted default. onChange
//   fires when the user types.
// * External re-seeding onto a predefined value (e.g. Settings load
//   returning a canonical key, or "Suggest context" landing on one)
//   collapses the picker out of Other mode. A user's deliberate Other
//   choice on a predefined value stays open until the value actually
//   changes.
import { useEffect, useRef, useState } from 'react'

import { i18nT } from '../../../i18n/t'
import SimpleSelect from '../../../components/SimpleSelect'

// Reserved sentinel for the "Other (custom)..." select option. Never
// persisted as a real value: an incoming ``value`` equal to this literal
// is coerced to empty in ``sanitisedValue`` below.
const OTHER_OPTION_SENTINEL = '__other__'

export interface DropdownOption {
  value: string
  label: string
}

export interface DropdownWithOtherProps {
  value: string
  onChange: (nextValue: string) => void
  options: readonly DropdownOption[]
  placeholder?: string
  ariaLabel?: string
}

export function DropdownWithOther({
  value,
  onChange,
  options,
  placeholder,
  ariaLabel,
}: DropdownWithOtherProps) {
  // Reserve the sentinel: never let an external value collide with the
  // "Other" marker. A colliding value is treated as empty so it neither
  // pre-fills the custom input with '__other__' nor round-trips the
  // literal back to the parent.
  const sanitisedValue = value === OTHER_OPTION_SENTINEL ? '' : value
  const trimmedSanitisedValue = sanitisedValue.trim()
  const isPredefinedValue = options.some(
    optionEntry => optionEntry.value === trimmedSanitisedValue,
  )
  const [otherModeActive, setOtherModeActive] = useState<boolean>(
    !isPredefinedValue && trimmedSanitisedValue.length > 0,
  )
  // Tracks whether the most recent value change came from the user
  // typing into the custom input. When true, the value-sync effect
  // below skips re-deriving the mode so the custom input does not
  // vanish mid-keystroke: a typed prefix could transiently match a
  // predefined option (e.g. "Formal" en route to "Formal letter") and
  // the picker would otherwise collapse out from under the user.
  const changeCameFromCustomInputRef = useRef<boolean>(false)

  // Re-derive the mode whenever the value changes EXTERNALLY (mount,
  // parent re-seed, or a "Suggest context"-style helper). Picking Other
  // from the select does NOT change ``value``, so this effect does not
  // run on that action -- handleSelectChange sets otherModeActive
  // directly and it sticks until the value actually changes.
  useEffect(() => {
    if (changeCameFromCustomInputRef.current) {
      changeCameFromCustomInputRef.current = false
      return
    }
    if (isPredefinedValue) {
      setOtherModeActive(false)
    } else if (trimmedSanitisedValue.length > 0) {
      setOtherModeActive(true)
    } else {
      setOtherModeActive(false)
    }
  }, [isPredefinedValue, trimmedSanitisedValue, sanitisedValue])

  const currentSelectValue = otherModeActive
    ? OTHER_OPTION_SENTINEL
    : isPredefinedValue
      ? trimmedSanitisedValue
      : ''

  const handleSelectChange = (nextSelectValue: string) => {
    if (nextSelectValue === OTHER_OPTION_SENTINEL) {
      // Open the custom input and preserve the prior value as an
      // editable starting point. Deliberately no ``onChange('')``: that
      // would destroy a persisted default on a single mis-click. Picking
      // Other does not change ``value``, so the value-sync effect above
      // does not run and collapse the input -- it stays open until the
      // user types (guarded by the ref) or an external change supplies
      // a new value.
      setOtherModeActive(true)
      return
    }
    setOtherModeActive(false)
    onChange(nextSelectValue)
  }

  const otherOptionLabel = i18nT('apps.writingReview.dropdownWithOther.otherOption')
  const customPlaceholder = i18nT('apps.writingReview.dropdownWithOther.customPlaceholder')
  const customTextInputAriaLabel = ariaLabel
    ? i18nT('apps.writingReview.dropdownWithOther.customAriaLabel', { field: ariaLabel })
    : i18nT('apps.writingReview.dropdownWithOther.customValueGenericAriaLabel')

  // Assemble the option list for ``SimpleSelect`` -- caller options first,
  // then the ``__other__`` sentinel that flips the row into free-text mode.
  // Both arrays walk in the same order so ``optionLabels[i]`` pairs with
  // ``options[i]``.
  const simpleSelectOptions = [...options.map(entry => entry.value), OTHER_OPTION_SENTINEL]
  const simpleSelectOptionLabels = [...options.map(entry => entry.label), otherOptionLabel]
  const emptyValueTrigger = placeholder ?? i18nT('apps.writingReview.dropdownWithOther.noneOption')

  return (
    <div className="space-y-2">
      <SimpleSelect
        aria-label={ariaLabel}
        value={currentSelectValue}
        options={simpleSelectOptions}
        optionLabels={simpleSelectOptionLabels}
        triggerFallback={emptyValueTrigger}
        onChange={handleSelectChange}
      />
      {otherModeActive && (
        <input
          type="text"
          value={sanitisedValue}
          onChange={event => {
            changeCameFromCustomInputRef.current = true
            onChange(event.target.value)
          }}
          placeholder={customPlaceholder}
          aria-label={customTextInputAriaLabel}
          className="w-full px-2 py-1.5 rounded border border-border bg-bg text-[13px] text-text"
        />
      )}
    </div>
  )
}
