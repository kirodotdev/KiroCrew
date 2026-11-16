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
  // (ADVERTISED_LABEL_MAX_CHARS). The label wraps but stops at two lines, so it
  // has to sit in a line-clamped element. The description wraps inside the row
  // (break-words) rather than widening it.
  it('caps an at-bound backend label at two lines, with the id still shown', () => {
    const long = 'N'.repeat(400)
    const desc = 'D'.repeat(400)
    render(<ModelDropdownList
      models={[{ name: 'opus', displayName: long, description: desc }]}
      activeModel="" onSelect={vi.fn()} />)
    const label = screen.getByText(long)
    expect(label.className).toContain('line-clamp-2')
    expect(label.hasAttribute('data-model-name')).toBe(true)
    expect(screen.getByText(desc).className).toContain('break-words')
    const hint = document.querySelector('[data-model-id-hint]')
    expect(hint?.textContent).toBe('opus')
  })

  // Claude Code on Bedrock: rows share a prefix and differ only at the end, in
  // the name ("(1M context)") and the id ("[1m]"). A truncated name or id hides
  // exactly that, so neither may sit in a truncating element; both wrap.
  it('wraps a Bedrock row name and id instead of cutting their ends', () => {
    render(<ModelDropdownList
      models={[
        { name: 'global.anthropic.claude-opus-4-8[1m]', displayName: 'Opus 4.8 (1M context)', description: 'Opus 4.8 for long sessions' },
        { name: 'global.anthropic.claude-opus-4-8', displayName: 'Opus 4.8', description: 'Opus 4.8 · Previous Opus version' },
      ]}
      activeModel="global.anthropic.claude-opus-4-8[1m]" onSelect={vi.fn()} />)
    const name = screen.getByText('Opus 4.8 (1M context)')
    const id = screen.getByText('global.anthropic.claude-opus-4-8[1m]')
    for (const el of [name, id]) {
      expect(el.className).not.toContain('truncate')
      expect(el.className).toMatch(/break-(words|all)/)
    }
    expect(name.parentElement?.className).toContain('flex-wrap')
  })

  it('wraps an unnamed row\'s raw id instead of cutting its end', () => {
    render(<ModelDropdownList
      models={[{ name: 'global.anthropic.claude-sonnet-4-6[1m]' }]}
      activeModel="" onSelect={vi.fn()} />)
    const label = screen.getByText('global.anthropic.claude-sonnet-4-6[1m]')
    expect(label.className).not.toContain('truncate')
    expect(label.className).toContain('break-all')
  })
})
