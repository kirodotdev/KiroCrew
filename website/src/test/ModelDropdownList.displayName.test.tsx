/**
 * A row whose backend sent a display name still shows its id.
 *
 * The composer chip and the config file show the id, so the picker row must
 * carry the id too (as a small monospace hint). The monospace font is for ids
 * only: a native label renders in the sans font.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

import ModelDropdownList from '../components/ModelDropdownList'

describe('ModelDropdownList — displayName rows keep their id visible', () => {
  it('shows the backend label in sans font, with the id as a mono hint', () => {
    render(<ModelDropdownList
      models={[{ name: 'opus', displayName: 'Opus 5.5', description: '' }]}
      activeModel="" onSelect={vi.fn()} />)
    const label = screen.getByText('Opus 5.5')
    expect(label.closest('[data-model-name]')).not.toBeNull()
    expect(label.className).not.toContain('font-mono')
    const hint = screen.getByText('opus')
    expect(hint.hasAttribute('data-model-id-hint')).toBe(true)
    expect(hint.className).toContain('font-mono')
  })

  it('shows the id in mono, with no hint, when the row has no displayName', () => {
    render(<ModelDropdownList
      models={[{ name: 'claude-opus-5' }]}
      activeModel="" onSelect={vi.fn()} />)
    const label = screen.getByText('claude-opus-5')
    expect(label.className).toContain('font-mono')
    expect(document.querySelector('[data-model-id-hint]')).toBeNull()
  })

  it('treats a displayName equal to the id as unnamed', () => {
    render(<ModelDropdownList
      models={[{ name: 'gpt-x', displayName: 'gpt-x' }]}
      activeModel="" onSelect={vi.fn()} />)
    const label = screen.getByText('gpt-x')
    expect(label.className).toContain('font-mono')
    expect(document.querySelector('[data-model-id-hint]')).toBeNull()
  })

  // The gateway clips a backend-sent label to 400 characters
  // (ADVERTISED_LABEL_MAX_CHARS), and the label stays on one truncated line, so it
  // has to sit in a truncating element. The description wraps inside the row
  // (break-words) rather than widening it.
  it('keeps an at-bound backend label on one truncated line, with the id still shown', () => {
    const long = 'N'.repeat(400)
    const desc = 'D'.repeat(400)
    render(<ModelDropdownList
      models={[{ name: 'opus', displayName: long, description: desc }]}
      activeModel="" onSelect={vi.fn()} />)
    const label = screen.getByText(long)
    expect(label.className).toContain('truncate')
    expect(label.hasAttribute('data-model-name')).toBe(true)
    expect(screen.getByText(desc).className).toContain('break-words')
    const hint = document.querySelector('[data-model-id-hint]')
    expect(hint?.textContent).toBe('opus')
  })
})
