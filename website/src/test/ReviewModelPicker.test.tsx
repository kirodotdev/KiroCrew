import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

const { simpleSelectSpy } = vi.hoisted(() => ({ simpleSelectSpy: vi.fn() }))

vi.mock('../hooks/useAvailableModels', () => ({
  useAvailableModels: () => [
    { name: 'auto', description: '' },
    { name: 'model-concrete', description: 'Live model' },
    { name: 'model-other', description: 'Another live model' },
  ],
}))

vi.mock('../components/SimpleSelect', () => ({
  default: ({
    options,
    optionLabels,
    value,
    onChange,
    disabled,
    'aria-label': ariaLabel,
  }: {
    options: string[]
    optionLabels?: string[]
    value: string
    onChange: (value: string) => void
    disabled?: boolean
    'aria-label'?: string
  }) => {
    simpleSelectSpy({ options, optionLabels, value, disabled })
    return (
      <select
        aria-label={ariaLabel}
        disabled={disabled}
        value={value}
        onChange={event => onChange(event.currentTarget.value)}
      >
        {options.map((option, index) => (
          <option key={option} value={option}>
            {optionLabels?.[index] ?? option}
          </option>
        ))}
      </select>
    )
  },
}))

import ReviewModelPicker from '../apps/code-review-sage/components/ReviewModelPicker'
import { REVIEW_MODEL_AUTO } from '../apps/code-review-sage/lib/types'

describe('ReviewModelPicker', () => {
  it('renders live model ids through SimpleSelect with translated Auto first', () => {
    render(<ReviewModelPicker value={REVIEW_MODEL_AUTO} onChange={vi.fn()} />)

    const select = screen.getByRole('combobox', { name: /Review model/i })
    expect(select).toHaveValue(REVIEW_MODEL_AUTO)
    expect(screen.getByRole('option', { name: 'Auto (recommended)' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'model-concrete' })).toBeInTheDocument()

    const props = simpleSelectSpy.mock.calls.at(-1)?.[0]
    expect(props.options).toEqual([REVIEW_MODEL_AUTO, 'model-concrete', 'model-other'])
    expect(props.optionLabels[0]).toBe('Auto (recommended)')
  })

  it('returns a selected concrete live model from the SimpleSelect control', () => {
    const onChange = vi.fn()
    render(<ReviewModelPicker value={REVIEW_MODEL_AUTO} onChange={onChange} />)

    fireEvent.change(screen.getByRole('combobox', { name: /Review model/i }), {
      target: { value: 'model-concrete' },
    })

    expect(onChange).toHaveBeenCalledWith('model-concrete')
  })
})
