import { fireEvent, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ChatInput from '../components/ChatInput'
import { loadChatConfig, saveChatConfig } from '../pages/chat/ChatSettings'
import { defaultProps } from './ChatInputTestUtils'
import { renderWithProviders } from './helpers'

const sent = [{ text: 'first' }, { text: 'second' }]

afterEach(() => {
  localStorage.removeItem('mc-chat-config')
})

// Settings -> Chat -> Composer -> Recall Sent Messages with Arrow Keys (#17498).
describe('ChatInput arrow-key prompt history setting', () => {
  it('recalls the newest sent message on ↑ in an empty composer by default', () => {
    const onChange = vi.fn()
    renderWithProviders(<ChatInput {...defaultProps} value="" onChange={onChange} sentMessages={sent} />)
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'ArrowUp' })
    expect(onChange).toHaveBeenLastCalledWith('second')
  })

  it('leaves ↑ alone in a read-only (disabled) composer', () => {
    const onChange = vi.fn()
    const onEditLastRequest = vi.fn()
    renderWithProviders(<ChatInput {...defaultProps} value="" disabled onChange={onChange} onEditLastRequest={onEditLastRequest} sentMessages={sent} />)
    const ta = screen.getByLabelText('Message input')
    fireEvent.keyDown(ta, { key: 'ArrowUp' })
    fireEvent.keyDown(ta, { key: 'ArrowUp', ctrlKey: true, metaKey: true })
    fireEvent.keyDown(ta, { key: 'ArrowUp', ctrlKey: true })
    fireEvent.keyDown(ta, { key: 'ArrowUp', metaKey: true })
    expect(onChange).not.toHaveBeenCalled()
    expect(onEditLastRequest).not.toHaveBeenCalled()
  })

  it('leaves ↑ to the caret when the setting is off', () => {
    saveChatConfig({ ...loadChatConfig(), arrowKeyPromptHistory: false })
    const onChange = vi.fn()
    renderWithProviders(<ChatInput {...defaultProps} value="" onChange={onChange} sentMessages={sent} />)
    const notPrevented = fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'ArrowUp' })
    expect(notPrevented).toBe(true)
    expect(onChange).not.toHaveBeenCalled()
  })
})
