/**
 * SessionMessageCard — the card for a message this session SENT another
 * session. One test per behaviour the transcript relies on.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import SessionMessageCard from './SessionMessageCard'

describe('SessionMessageCard', () => {
  const sender = { key: 'chat-wf-step', title: 'wf-planner' }
  const recipient = { key: 'chat-200', title: 'Parent chat' }

  it('says the direction in visible text: "This session · Sent to @recipient"', () => {
    const onOpen = vi.fn()
    render(<SessionMessageCard sender={sender} recipient={recipient} body="plan is ready for review" onOpenSession={onOpen} />)
    expect(screen.getByTestId('session-message-card').getAttribute('data-direction')).toBe('outgoing')
    // The sender is named "This session", not by a title that reads as another
    // sender; the title stays on hover. Never a link to itself.
    const title = screen.getByTestId('session-card-title')
    expect(title.textContent).toBe('This session')
    expect(title).toHaveAttribute('title', 'wf-planner')
    expect(title.tagName).not.toBe('BUTTON')
    // The direction is visible text, not an sr-only span or an icon.
    const direction = screen.getByTestId('session-card-direction')
    expect(direction.textContent).toBe('Sent to @Parent chat')
    expect(direction.closest('.sr-only')).toBeNull()
    const tag = screen.getByTestId('session-card-recipient')
    expect(direction).toContainElement(tag)
    // The tag says it opens the recipient, so it does not read as a send.
    expect(tag).toHaveAttribute('title', 'Open Parent chat')
    fireEvent.click(tag)
    expect(onOpen).toHaveBeenCalledWith('chat-200')
  })

  it('draws no avatar: nothing beside "This session" reads as another name', () => {
    render(<SessionMessageCard sender={sender} recipient={recipient} body="hi" />)
    expect(screen.queryByTestId('session-card-avatar')).not.toBeInTheDocument()
    expect(screen.getByTestId('session-message-card').textContent).not.toContain('WF')
  })

  it('without onOpenSession the @tag is plain text, not a link', () => {
    render(<SessionMessageCard sender={sender} recipient={recipient} body="hi" />)
    expect(screen.getByTestId('session-card-recipient').tagName).toBe('SPAN')
  })

  it('keeps the body to the message alone: the @tag sits in the header, not the body', () => {
    render(<SessionMessageCard sender={sender} recipient={recipient} body="plan is ready for review" />)
    const body = screen.getByTestId('session-card-body')
    expect(body).not.toContainElement(screen.getByTestId('session-card-recipient'))
    expect(body.textContent).toBe('plan is ready for review')
  })

  it('clamps a long body and toggles Show more / Show less', () => {
    render(<SessionMessageCard sender={sender} recipient={recipient} body={'x'.repeat(400)} />)
    expect(screen.getByTestId('session-card-body').className).toContain('line-clamp-2')
    const toggle = screen.getByTestId('session-card-toggle')
    expect(toggle.textContent).toBe('Show more')
    fireEvent.click(toggle)
    expect(screen.getByTestId('session-card-body').className).not.toContain('line-clamp-2')
    expect(screen.getByTestId('session-card-toggle').textContent).toBe('Show less')
  })

  it('a short body offers no toggle, and no time draws no meta line', () => {
    render(<SessionMessageCard sender={sender} recipient={recipient} body="ok" />)
    expect(screen.queryByTestId('session-card-toggle')).not.toBeInTheDocument()
    expect(screen.queryByTestId('session-card-meta')).not.toBeInTheDocument()
  })
})
