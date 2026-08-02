import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { CollapsibleMessage } from '../pages/SchedulePage'

describe('CollapsibleMessage', () => {
  it('shows truncated preview collapsed by default', () => {
    const long = 'a'.repeat(120)
    const { container } = render(<CollapsibleMessage message={long} />)
    expect(container.querySelector('pre')).toBeNull()
    expect(screen.getByText(/a+…$/)).toBeInTheDocument()
  })

  it('expands to show full message in <pre> on click', () => {
    const msg = 'line one\n  indented line two'
    const { container } = render(<CollapsibleMessage message={msg} />)
    fireEvent.click(container.querySelector('button')!)
    const pre = container.querySelector('pre')
    expect(pre).not.toBeNull()
    expect(pre!.textContent).toBe(msg)
    expect(pre!.className).toContain('whitespace-pre-wrap')
  })

  // collapse round-trip
  it('collapses back to preview on second click', () => {
    const msg = 'a'.repeat(120)
    const { container } = render(<CollapsibleMessage message={msg} />)
    const btn = container.querySelector('button')!
    fireEvent.click(btn)
    expect(container.querySelector('pre')).not.toBeNull()
    fireEvent.click(btn)
    expect(container.querySelector('pre')).toBeNull()
    expect(screen.getByText(/a+…$/)).toBeInTheDocument()
  })

  // short messages (≤80 chars, else branch)
  it('collapses whitespace for short messages without ellipsis', () => {
    const { container } = render(<CollapsibleMessage message={'line one\n  line two'} />)
    expect(container.textContent).toContain('line one line two')
    expect(container.textContent).not.toContain('…')
  })

  it('stops click propagation on pre so row handler does not fire', () => {
    let bubbled = false
    const { container } = render(
      // Test-only wrapper that records click bubbling; not a real UI control.
      // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions
      <div onClick={() => { bubbled = true }}>
        <CollapsibleMessage message="hello" />
      </div>
    )
    fireEvent.click(container.querySelector('button')!)
    bubbled = false
    fireEvent.click(container.querySelector('pre')!)
    expect(bubbled).toBe(false)
  })

  it('sanitizes LLM output (redacts AWS access keys)', () => {
    const leaky = 'hi AKIAIOSFODNN7EXAMPLE bye'
    const { container } = render(<CollapsibleMessage message={leaky} />)
    expect(container.textContent).not.toContain('AKIAIOSFODNN7EXAMPLE')
    fireEvent.click(container.querySelector('button')!)
    expect(container.querySelector('pre')!.textContent).not.toContain('AKIAIOSFODNN7EXAMPLE')
  })

  it('redacts exfiltration URLs in both states', () => {
    const leaky = 'see https://attacker.example.com/exfil?data=' + 'A'.repeat(50)
    const { container } = render(<CollapsibleMessage message={leaky} />)
    expect(container.textContent).not.toContain('attacker.example.com/exfil?data=AAAA')
    expect(container.textContent).toContain('[REDACTED: suspicious URL')
    fireEvent.click(container.querySelector('button')!)
    expect(container.querySelector('pre')!.textContent).not.toContain('attacker.example.com/exfil?data=AAAA')
  })
})
