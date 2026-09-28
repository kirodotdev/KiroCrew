import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import { SettingsButtonGroup, SettingsInput, SettingsSelect, SettingsStepper } from '../components/settings'

/**
 * SettingsField label association (issue #3283).
 *
 * The bug this locks: SettingsField rendered its caption as a plain <span>,
 * so the visible caption was never programmatically associated with the
 * control it captions. The single-line branch of SettingsInput passed
 * `aria-label={ariaLabel}` where ariaLabel is undefined unless a caller
 * explicitly duplicated the caption — so those inputs had NO accessible name
 * at all (screen readers fell back to placeholder text or nothing).
 *
 * The fix generates a per-instance id (React.useId) in each field component,
 * renders the caption as <label htmlFor={id}>, and threads the id onto the
 * wrapped control. getByLabelText(<caption>) is therefore the assertion of
 * record here: it resolves only through a real accessible-name channel
 * (htmlFor/id association or aria-label), never through visual adjacency.
 */
describe('SettingsField label association', () => {
  it('single-line input: caption resolves the control via htmlFor/id with no aria-label passed', () => {
    render(<SettingsInput label="API key" value="" onChange={() => {}} placeholder="sk-..." />)

    // Fail-before case: on main this throws — the caption is a <span> and the
    // input has aria-label={undefined}, so the input has no accessible name.
    const input = screen.getByLabelText('API key')
    expect(input.tagName).toBe('INPUT')

    // Prove the channel is a REAL label association, not an aria-label
    // default: the caption element must be a <label> whose htmlFor points at
    // the input's id.
    const label = screen.getByText('API key')
    expect(label.tagName).toBe('LABEL')
    expect(input.id).toBeTruthy()
    expect(label).toHaveAttribute('for', input.id)
    // No aria-label was passed and none may be invented: the association IS
    // the name. Inventing one would mask a future htmlFor regression.
    expect(input).not.toHaveAttribute('aria-label')
  })

  it('multiline textarea: caption is a <label> associated with the textarea, aria-label fallback kept', () => {
    render(<SettingsInput label="System prompt" value="" onChange={() => {}} multiline />)

    const textarea = screen.getByLabelText('System prompt')
    expect(textarea.tagName).toBe('TEXTAREA')

    // The pre-existing aria-label fallback must survive. NOTE: this is a
    // deliberate contract lock on the fallback the spec says to keep, not an
    // a11y requirement — it wins over the label element and carries the same
    // string, so nothing double-announces.
    expect(textarea).toHaveAttribute('aria-label', 'System prompt')

    // And the label association must exist underneath it.
    const label = screen.getByText('System prompt')
    expect(label.tagName).toBe('LABEL')
    expect(textarea.id).toBeTruthy()
    expect(label).toHaveAttribute('for', textarea.id)
  })

  it('select: caption is a <label> associated with the Radix trigger, aria-label fallback kept', () => {
    render(
      <SettingsSelect
        label="Theme"
        value="dark"
        options={['light', 'dark']}
        onChange={() => {}}
      />
    )

    const trigger = screen.getByLabelText('Theme')
    // The actual contract is that the trigger is a labelable element (<label
    // htmlFor> only names form-associated elements); its ARIA role is Radix's
    // business and may change across versions.
    expect(trigger.tagName).toBe('BUTTON')

    // Pre-existing aria-label path must survive as the fallback. NOTE: this is
    // a deliberate contract lock on the fallback the spec says to keep (an
    // explicit override channel), not an a11y requirement — the htmlFor
    // association below would name the trigger by itself.
    expect(trigger).toHaveAttribute('aria-label', 'Theme')

    // And the trigger must now carry the id the caption's htmlFor points at.
    const label = screen.getByText('Theme')
    expect(label.tagName).toBe('LABEL')
    expect(trigger.id).toBeTruthy()
    expect(label).toHaveAttribute('for', trigger.id)
  })

  it('explicit aria-label override still wins over the label association', () => {
    render(
      <SettingsInput
        label="Visible caption"
        aria-label="Announced name"
        value=""
        onChange={() => {}}
      />
    )

    // Query by COMPUTED accessible name: getByRole implements accname
    // precedence, so this fails if the htmlFor association ever outranks the
    // explicit aria-label (getByLabelText alone would pass through either
    // channel and could not catch a precedence regression).
    const input = screen.getByRole('textbox', { name: 'Announced name' })
    expect(input).toHaveAttribute('aria-label', 'Announced name')

    // The caption text must NOT be the computed name once overridden…
    expect(screen.queryByRole('textbox', { name: 'Visible caption' })).toBeNull()

    // …but it still renders visually (the override changes the announced
    // name, not the visual caption).
    expect(screen.getByText('Visible caption')).toBeInTheDocument()
  })

  it('keeps the data-setting-label / data-setting-key query hooks intact', () => {
    const { container } = render(
      <SettingsInput label="Poll interval" configKey="poll.interval" value="30" onChange={() => {}} />
    )

    const field = container.querySelector('[data-setting-label="Poll interval"]')
    expect(field).not.toBeNull()
    expect(field).toHaveAttribute('data-setting-key', 'poll.interval')
  })

  it('generates distinct ids for sibling fields with the same caption', () => {
    render(
      <>
        <SettingsInput label="Token" value="a" onChange={() => {}} />
        <SettingsInput label="Token" value="b" onChange={() => {}} />
      </>
    )

    const inputs = screen.getAllByLabelText('Token')
    expect(inputs).toHaveLength(2)
    expect(inputs[0].id).not.toBe(inputs[1].id)
  })

  it('stepper and button-group captions stay a <span> — no single labelable control', () => {
    // Locks the deliberate no-controlId decision: a <label htmlFor> pointing
    // at a button group (or at nothing) is itself an a11y defect. If a later
    // refactor threads controlId into these wrappers, this fails.
    render(
      <>
        <SettingsStepper label="Retries" value={3} onIncrement={() => {}} onDecrement={() => {}} />
        <SettingsButtonGroup
          label="Density"
          value="cozy"
          options={[{ value: 'cozy', label: 'Cozy' }, { value: 'compact', label: 'Compact' }]}
          onChange={() => {}}
        />
      </>
    )

    expect(screen.getByText('Retries').tagName).toBe('SPAN')
    expect(screen.getByText('Density').tagName).toBe('SPAN')
    // The group keeps its own naming channel (role=group + aria-label).
    expect(screen.getByRole('group', { name: 'Density' })).toBeInTheDocument()
  })
})
