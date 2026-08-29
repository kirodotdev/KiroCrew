/**
 * Tests for ``DropdownWithOther`` -- a select with a built-in "Other
 * (custom)..." escape hatch. The behavioural contract mirrors an older
 * internal version of this app's ``DropdownWithOther``: built-in options
 * render their localized label but store a canonical value on
 * change; a value not in the options list drops the picker into "Other"
 * mode with the raw string in an inline text input; picking a
 * predefined option or being externally re-seeded onto one collapses
 * out of Other mode; the ``__other__`` sentinel string is reserved and
 * never surfaces as a persisted value.
 *
 * The tests below pin the wire semantics -- what ``onChange`` receives
 * and what the picker mode is after each user action. Visual details
 * (chip styling, aria attributes beyond the labelled roles) are not
 * covered here; those belong to the parent-component tests.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// ``SimpleSelect`` (Radix) does not respond to ``fireEvent.change`` and its
// popup is portalled by an open-then-click cycle in a real browser. Stub it
// with a synchronous native ``<select>`` that exposes the same call signature
// so these tests can drive it deterministically without pulling in a
// pointer-event polyfill. Same idiom as ``AppsPageW3Coverage.test.tsx``. The
// component under test (``DropdownWithOther``) is the code being exercised
// here, not ``SimpleSelect`` itself.
vi.mock('../../../components/SimpleSelect', () => ({
  default: ({
    options,
    optionLabels,
    value,
    onChange,
    triggerFallback,
    'aria-label': ariaLabel,
  }: {
    options: string[]
    optionLabels?: string[]
    value: string
    onChange: (nextValue: string) => void
    triggerFallback?: string
    'aria-label'?: string
  }) => (
    <select
      aria-label={ariaLabel}
      value={value}
      onChange={event => onChange(event.target.value)}
    >
      <option value="" disabled>
        {triggerFallback ?? ''}
      </option>
      {options.map((optionValue, optionIndex) => (
        <option key={optionValue} value={optionValue}>
          {optionLabels?.[optionIndex] ?? optionValue}
        </option>
      ))}
    </select>
  ),
}))

import { DropdownWithOther } from './DropdownWithOther'

const AUDIENCE_TEST_OPTIONS = [
  { value: 'internalTeam', label: 'Internal team' },
  { value: 'vpLeadership', label: 'VP / Leadership' },
  { value: 'externalCustomer', label: 'External customer' },
] as const

describe('DropdownWithOther', () => {
  it('renders each provided option with its localized label', () => {
    render(
      <DropdownWithOther
        value=""
        onChange={vi.fn()}
        options={AUDIENCE_TEST_OPTIONS}
        ariaLabel="Audience"
      />,
    )
    // Labels are what the user sees; values are what onChange emits.
    for (const optionEntry of AUDIENCE_TEST_OPTIONS) {
      expect(screen.getByRole('option', { name: optionEntry.label })).toBeInTheDocument()
    }
  })

  it('emits the canonical value (not the label) when a predefined option is picked', () => {
    const onChangeSpy = vi.fn()
    render(
      <DropdownWithOther
        value=""
        onChange={onChangeSpy}
        options={AUDIENCE_TEST_OPTIONS}
        ariaLabel="Audience"
      />,
    )
    const audienceSelect = screen.getByRole('combobox', { name: 'Audience' })
    fireEvent.change(audienceSelect, { target: { value: 'vpLeadership' } })
    // The wire value flows through unchanged; downstream (scanner prompt,
    // review record) has always seen the canonical key, and the Other
    // path must preserve that contract for non-custom picks.
    expect(onChangeSpy).toHaveBeenCalledWith('vpLeadership')
  })

  it('drops into Other mode when the seeded value is not a predefined option', () => {
    render(
      <DropdownWithOther
        value="Board deck reviewers"
        onChange={vi.fn()}
        options={AUDIENCE_TEST_OPTIONS}
        ariaLabel="Audience"
      />,
    )
    // The custom input renders showing the persisted value verbatim.
    // Matched by the input's own aria label so this test does not
    // depend on the placeholder text.
    const customTextInput = screen.getByRole('textbox', { name: /audience/i })
    expect(customTextInput).toHaveValue('Board deck reviewers')
  })

  it('reveals the custom text input when the user picks the Other option', () => {
    const onChangeSpy = vi.fn()
    render(
      <DropdownWithOther
        value="internalTeam"
        onChange={onChangeSpy}
        options={AUDIENCE_TEST_OPTIONS}
        ariaLabel="Audience"
      />,
    )
    // Custom input is hidden until the user explicitly enters Other mode.
    expect(screen.queryByRole('textbox', { name: /audience/i })).toBeNull()
    const audienceSelect = screen.getByRole('combobox', { name: 'Audience' })
    fireEvent.change(audienceSelect, { target: { value: '__other__' } })
    // Custom input appears; ``value`` prop is unchanged (the persisted
    // "internalTeam" stays until the user types) so onChange should NOT
    // fire on the mode switch alone -- picking Other is a UI intent,
    // not a value edit.
    expect(screen.getByRole('textbox', { name: /audience/i })).toBeInTheDocument()
    expect(onChangeSpy).not.toHaveBeenCalled()
  })

  it('emits the typed value verbatim as the user edits the custom input', () => {
    const onChangeSpy = vi.fn()
    render(
      <DropdownWithOther
        value="Board deck reviewers"
        onChange={onChangeSpy}
        options={AUDIENCE_TEST_OPTIONS}
        ariaLabel="Audience"
      />,
    )
    const customTextInput = screen.getByRole('textbox', { name: /audience/i })
    fireEvent.change(customTextInput, { target: { value: 'Q3 execs' } })
    // Wire contract: whatever the user types IS the value. No
    // sanitisation, no coercion; the parent decides on submit.
    expect(onChangeSpy).toHaveBeenCalledWith('Q3 execs')
  })

  it('never persists the __other__ sentinel as a value', () => {
    const onChangeSpy = vi.fn()
    render(
      <DropdownWithOther
        value="__other__"
        onChange={onChangeSpy}
        options={AUDIENCE_TEST_OPTIONS}
        ariaLabel="Audience"
      />,
    )
    // A stray ``__other__`` sneaking in (bad migration, hand-edited
    // settings.json) is treated as empty rather than round-tripping the
    // literal back to the parent. The custom input, if it renders, must
    // not carry the sentinel string as its user-visible value.
    const customTextInput = screen.queryByRole('textbox', { name: /audience/i })
    if (customTextInput) {
      expect(customTextInput).not.toHaveValue('__other__')
    }
  })

  it('stays in Other mode when the value is externally re-seeded from one custom value to another', () => {
    // Regression guard (Spock F1): a persisted custom default of "foo"
    // that gets replaced by another custom default "bar" via a settings
    // refresh must keep the picker in Other mode showing the new
    // string. The behaviour depends on the effect at
    // ``DropdownWithOther.tsx:68`` re-deriving mode from the incoming
    // value: ``changeCameFromCustomInputRef`` is ``false`` (external
    // change), ``isPredefinedValue`` is ``false`` (custom-still-
    // custom), ``trimmedSanitisedValue.length > 0`` (non-empty new
    // value) -> otherModeActive stays ``true``. Regression here would
    // manifest as the picker briefly flashing to select mode or
    // stranding the user without an input to type into.
    const { rerender } = render(
      <DropdownWithOther
        value="Original custom audience"
        onChange={vi.fn()}
        options={AUDIENCE_TEST_OPTIONS}
        ariaLabel="Audience"
      />,
    )
    const initialCustomTextInput = screen.getByRole('textbox', { name: /audience/i })
    expect(initialCustomTextInput).toHaveValue('Original custom audience')
    rerender(
      <DropdownWithOther
        value="Fresh custom audience"
        onChange={vi.fn()}
        options={AUDIENCE_TEST_OPTIONS}
        ariaLabel="Audience"
      />,
    )
    // Still in Other mode, now showing the new custom value.
    const refreshedCustomTextInput = screen.getByRole('textbox', {
      name: /audience/i,
    })
    expect(refreshedCustomTextInput).toHaveValue('Fresh custom audience')
  })

  it('collapses out of Other mode when the value is externally re-seeded to a predefined option', () => {
    // Simulates the "Suggest context" case (the old internal version)
    // or a Settings load that returns a canonical value: the picker
    // must switch back to select mode even if the user had opened
    // Other in a prior state.
    const { rerender } = render(
      <DropdownWithOther
        value="Board deck reviewers"
        onChange={vi.fn()}
        options={AUDIENCE_TEST_OPTIONS}
        ariaLabel="Audience"
      />,
    )
    expect(screen.getByRole('textbox', { name: /audience/i })).toBeInTheDocument()
    rerender(
      <DropdownWithOther
        value="internalTeam"
        onChange={vi.fn()}
        options={AUDIENCE_TEST_OPTIONS}
        ariaLabel="Audience"
      />,
    )
    // Custom input is gone; select shows the predefined option.
    expect(screen.queryByRole('textbox', { name: /audience/i })).toBeNull()
    const audienceSelect = screen.getByRole('combobox', { name: 'Audience' }) as HTMLSelectElement
    expect(audienceSelect.value).toBe('internalTeam')
  })
})
