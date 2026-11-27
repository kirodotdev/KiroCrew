import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { useState } from 'react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

// Escape in the message box never stops a running turn; the Stop button does
// (docs/decisions/2026-11-24-escape-in-the-message-box-does-not-stop-the-turn.md).
// The rich-text editor's twin is in ChatInput.lexicalKeys.test.tsx.

function Host(props: Partial<React.ComponentProps<typeof ChatInput>>) {
  const [value, setValue] = useState('')
  return <ChatInput value={value} onChange={setValue} onSend={vi.fn()} connected={true} {...props} />
}

const textarea = () => screen.getByRole('textbox') as HTMLTextAreaElement

describe('ChatInput: Escape does not stop the running turn', () => {
  it('a bare Escape while a turn runs does not stop it; the Stop button does', () => {
    const onStop = vi.fn()
    renderWithProviders(<Host isRunning onStop={onStop} />)
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(onStop).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('stop-button-armed'))
    expect(onStop).toHaveBeenCalledTimes(1)
  })

  it('the Stop button tooltip names no key', () => {
    renderWithProviders(<Host isRunning onStop={vi.fn()} />)
    expect(screen.getByTestId('stop-button-armed')).toHaveAttribute('title', 'Stop generation')
  })
})
