import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import ChatFooter from '../pages/chat/ChatFooter'

const base = { running: false, stopping: false, state: '', lastRole: '', avatar: '/logo.png', botName: 'KiroCrew' }

describe('ChatFooter', () => {
  it('returns null when not running', () => {
    const { container } = render(<ChatFooter {...base} />)
    expect(container.innerHTML).toBe('')
  })

  it('returns null when streaming', () => {
    const { container } = render(<ChatFooter {...base} running={true} lastRole="streaming" />)
    expect(container.innerHTML).toBe('')
  })

  it('returns null when tool_running', () => {
    const { container } = render(<ChatFooter {...base} running={true} state="tool_running" lastRole="user" />)
    expect(container.innerHTML).toBe('')
  })

  it('returns null when lastRole is tool', () => {
    const { container } = render(<ChatFooter {...base} running={true} lastRole="tool" />)
    expect(container.innerHTML).toBe('')
  })

  it('shows stopping indicator', () => {
    render(<ChatFooter {...base} running={true} stopping={true} lastRole="user" />)
    expect(screen.getByText('Stopping…')).toBeInTheDocument()
  })

  it('shows compacting indicator', () => {
    render(<ChatFooter {...base} running={true} state="compacting" lastRole="user" />)
    expect(screen.getByText(/Compacting…/)).toBeInTheDocument()
  })

  it('shows icon carousel when running normally', () => {
    const { container } = render(<ChatFooter {...base} running={true} lastRole="user" />)
    expect(container.querySelector('.csb4')).toBeInTheDocument()
  })
})
