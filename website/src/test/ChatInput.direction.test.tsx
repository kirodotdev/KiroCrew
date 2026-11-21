import { fireEvent, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import ChatInput from '../components/ChatInput'
import { renderWithProviders } from './helpers'

const draft = 'שלום @agent (123)\nEnglish שלום\n123 — עברית'

function Host() {
  const [value, setValue] = useState(draft)
  return <ChatInput value={value} onChange={setValue} onSend={vi.fn()} />
}

describe('composer content direction', () => {
  it('uses native auto direction and paragraph layout on the textarea and paste mirror', () => {
    const { container } = renderWithProviders(<Host />)
    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    const mirror = container.querySelector('[aria-hidden][data-composer-typo]')!
    for (const element of [input, mirror]) {
      expect(element).toHaveAttribute('dir', 'auto')
      expect(element).toHaveStyle({ unicodeBidi: 'plaintext', textAlign: 'start' })
    }
    expect(input.value).toBe(draft)
    expect(mirror.textContent).toBe(draft)
    input.setSelectionRange(5, 11)
    expect(input.value.slice(input.selectionStart, input.selectionEnd)).toBe('@agent')
    fireEvent.change(input, { target: { value: `${draft}\nעוד` } })
    expect(input.value).toBe(`${draft}\nעוד`)
    expect(mirror.textContent).toBe(input.value)
    expect(document.documentElement.getAttribute('dir')).not.toBe('rtl')
  })
})
