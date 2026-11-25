import { describe, it, expect, vi } from 'vitest'
import { act, screen, fireEvent } from '@testing-library/react'
import { useState } from 'react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import type { SteerEscape } from '../components/chat-input/steerEscape'
import { LAZY_COMPOSER_MOUNT } from './lazyComposerReady'

// Escape in the message box takes back a steer the running turn has not read
// yet (issue #42197). It never stops the turn
// (docs/decisions/2026-11-24-escape-in-the-message-box-does-not-stop-the-turn.md):
// with or without a steer to take back, the Stop button is the one way to stop.

function Host(props: Partial<React.ComponentProps<typeof ChatInput>>) {
  const [value, setValue] = useState('')
  return <ChatInput value={value} onChange={setValue} onSend={vi.fn()} connected={true} {...props} />
}

const textarea = () => screen.getByRole('textbox') as HTMLTextAreaElement
const steers = (offered: number, reachable = offered): SteerEscape => ({ offered, reachable, takeBack: vi.fn(() => reachable > 0) })

describe('ChatInput: Escape takes back a pending steer', () => {
  it('a bare Escape takes back the steer and does not stop the turn; the Stop button does', () => {
    const onStop = vi.fn()
    const steerEscape = steers(1)
    renderWithProviders(<Host isRunning onStop={onStop} steerEscape={steerEscape} />)
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(steerEscape.takeBack).toHaveBeenCalledTimes(1)
    expect(onStop).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('stop-button-armed'))
    expect(onStop).toHaveBeenCalledTimes(1)
  })

  it('with no steer left, Escape stops nothing and takes nothing back', () => {
    const onStop = vi.fn()
    const steerEscape = steers(0)
    renderWithProviders(<Host isRunning onStop={onStop} steerEscape={steerEscape} />)
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(steerEscape.takeBack).not.toHaveBeenCalled()
    expect(onStop).not.toHaveBeenCalled()
  })

  it('the Stop button tooltip names no key while a steer can be taken back', () => {
    renderWithProviders(<Host isRunning onStop={vi.fn()} steerEscape={steers(1)} />)
    expect(screen.getByTestId('stop-button-armed')).toHaveAttribute('title', 'Stop generation')
  })
})

describe('ChatInput: the take-back hint', () => {
  it('shows while a row offers a take-back', () => {
    renderWithProviders(<Host isRunning onStop={vi.fn()} steerEscape={steers(2)} />)
    expect(screen.getByTestId('escape-steer-take-back-hint')).toHaveTextContent('Esc to take back your message')
  })

  it('shows nothing for a steer no press can take back yet, or with none pending', () => {
    const { unmount } = renderWithProviders(<Host isRunning onStop={vi.fn()} steerEscape={steers(0, 1)} />)
    expect(screen.queryByTestId('escape-steer-take-back-hint')).toBeNull()
    unmount()
    renderWithProviders(<Host isRunning onStop={vi.fn()} steerEscape={steers(0)} />)
    expect(screen.queryByTestId('escape-steer-take-back-hint')).toBeNull()
  })

  it('shows nothing while the message box is typing-locked, where Escape takes nothing back', () => {
    const steerEscape = steers(1)
    renderWithProviders(<Host isRunning onStop={vi.fn()} steerEscape={steerEscape} typingLock={{ holderName: 'Alice' }} />)
    expect(screen.queryByTestId('escape-steer-take-back-hint')).toBeNull()
    fireEvent.keyDown(textarea(), { key: 'Escape', code: 'Escape' })
    expect(steerEscape.takeBack).not.toHaveBeenCalled()
  })
})

describe('ChatInput rich-text editor: Escape takes back a pending steer', () => {
  it('takes back the steer through the editor\'s Escape command, and does not stop the turn', async () => {
    const onStop = vi.fn()
    const steerEscape = steers(1)
    renderWithProviders(<Host isRunning onStop={onStop} steerEscape={steerEscape} lexicalComposer />)
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    expect(input).toHaveAttribute('data-lexical-composer')
    await act(async () => {})
    fireEvent.keyDown(input, { key: 'Escape', code: 'Escape' })
    await act(async () => {})
    expect(steerEscape.takeBack).toHaveBeenCalledTimes(1)
    expect(onStop).not.toHaveBeenCalled()
  })
})
