/** Qoder supplies service display names while the wire value remains its id. */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

import ModelDropdownList from '../components/ModelDropdownList'

describe('ModelDropdownList — backend labels', () => {
  it('renders the label as primary text and keeps the id as the value', () => {
    render(
      <ModelDropdownList
        models={[{ name: 'qfmodel', label: 'Qwen3.8-Flash', description: '0.00x Credit' }]}
        activeModel="qfmodel"
        onSelect={vi.fn()}
      />,
    )

    const primary = screen.getByText('Qwen3.8-Flash')
    expect(primary.getAttribute('data-model-id')).toBe('qfmodel')
    expect(primary.hasAttribute('data-model-name')).toBe(true)
    expect(screen.getByRole('option')).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('0.00x Credit')).toBeInTheDocument()
  })

  it('falls back to the id when a row has no backend label', () => {
    render(
      <ModelDropdownList
        models={[{ name: 'plain-id', description: '' }]}
        activeModel=""
        onSelect={vi.fn()}
      />,
    )

    expect(screen.getByText('plain-id').getAttribute('data-model-id')).toBe('plain-id')
  })
})
