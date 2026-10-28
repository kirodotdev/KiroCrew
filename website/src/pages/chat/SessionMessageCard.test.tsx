/**
 * SessionMessageCard — the one cross-session card, both directions, plus its
 * pure avatar helpers. One test per surface-independent behaviour the three
 * surfaces rely on.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import SessionMessageCard from './SessionMessageCard'
import { AVATAR_PALETTE, senderAvatarColor, senderAvatarIndex, senderInitials } from '../../utils/sessionColors'

describe('sender avatar helpers', () => {
  it('senderInitials: two words → first letter of each, uppercase', () => {
    expect(senderInitials('Slack Code')).toBe('SC')
    expect(senderInitials('romain dura')).toBe('RD')
  })
  it('senderInitials: one word → first two letters', () => {
    expect(senderInitials('planner')).toBe('PL')
    expect(senderInitials('X')).toBe('X')
  })
  it('senderInitials: skips a leading @/# so a handle still yields letters', () => {
    expect(senderInitials('@bolichen97')).toBe('BO')
    expect(senderInitials('#general room')).toBe('GR')
  })
  it('senderInitials: empty falls back to ?', () => {
    expect(senderInitials('')).toBe('?')
    expect(senderInitials('   ')).toBe('?')
  })
  it('senderAvatarIndex is deterministic and in palette range', () => {
    const a = senderAvatarIndex('chat-300-1791983459')
    expect(a).toBe(senderAvatarIndex('chat-300-1791983459'))
    expect(a).toBeGreaterThanOrEqual(0)
    expect(a).toBeLessThan(AVATAR_PALETTE.length)
    // An empty key folds to the first colour rather than throwing.
    expect(senderAvatarIndex('')).toBe(0)
  })
  it('senderAvatarColor: a palette fill with a readable black/white foreground', () => {
    const { bg, fg } = senderAvatarColor('chat-42')
    expect(AVATAR_PALETTE).toContain(bg)
    expect(['#ffffff', '#100f0f']).toContain(fg)
  })
})

describe('SessionMessageCard incoming', () => {
  const sender = { key: 'chat-300-1791983459', title: 'Slack Code' }

  it('draws a filled avatar, the sender title as a link, meta, and opens the sender', () => {
    const onOpen = vi.fn()
    render(
      <SessionMessageCard
        direction="incoming"
        sender={sender}
        body="the base moved, rebase before you push"
        agent="kirocrew-worker"
        runName="slack stack"
        time="4:12 PM"
        onOpenSession={onOpen}
      />,
    )
    const card = screen.getByTestId('session-message-card')
    expect(card.getAttribute('data-direction')).toBe('incoming')
    expect(screen.getByTestId('session-card-avatar').getAttribute('data-outgoing')).toBe('false')
    expect(screen.getByTestId('session-card-avatar').textContent).toBe('SC')
    const link = screen.getByTestId('session-card-title-link')
    expect(link.textContent).toBe('Slack Code')
    fireEvent.click(link)
    expect(onOpen).toHaveBeenCalledWith('chat-300-1791983459')
    expect(screen.getByTestId('session-card-meta').textContent).toBe('kirocrew-worker · slack stack · 4:12 PM')
    // Incoming carries no recipient tag.
    expect(screen.queryByTestId('session-card-recipient')).not.toBeInTheDocument()
  })

  it('clamps a long body and toggles Show more / Show less', () => {
    render(
      <SessionMessageCard direction="incoming" sender={sender} body={'x'.repeat(400)} />,
    )
    const body = screen.getByTestId('session-card-body')
    expect(body.className).toContain('line-clamp-2')
    const toggle = screen.getByTestId('session-card-toggle')
    expect(toggle.textContent).toBe('Show more')
    fireEvent.click(toggle)
    expect(screen.getByTestId('session-card-body').className).not.toContain('line-clamp-2')
    expect(screen.getByTestId('session-card-toggle').textContent).toBe('Show less')
  })

  it('a short body offers no toggle', () => {
    render(<SessionMessageCard direction="incoming" sender={sender} body="ok" />)
    expect(screen.queryByTestId('session-card-toggle')).not.toBeInTheDocument()
  })

  it('a step waiting on the person carries the Needs you pill', () => {
    render(<SessionMessageCard direction="incoming" sender={sender} body="plan ready" needsYou />)
    expect(screen.getByTestId('session-card-needs-you').textContent).toBe('Needs you')
  })

  it('without onOpenSession the title is plain text, not a link', () => {
    render(<SessionMessageCard direction="incoming" sender={sender} body="hi" />)
    expect(screen.queryByTestId('session-card-title-link')).not.toBeInTheDocument()
    expect(screen.getByTestId('session-card-title').textContent).toBe('Slack Code')
  })
})

describe('SessionMessageCard outgoing', () => {
  const sender = { key: 'chat-wf-step', title: 'wf-planner' }
  const recipient = { key: 'chat-200', title: 'Parent chat' }

  it('says the direction in visible text: "This session · Sent to @recipient"', () => {
    const onOpen = vi.fn()
    render(
      <SessionMessageCard
        direction="outgoing"
        sender={sender}
        recipient={recipient}
        body="plan is ready for review"
        onOpenSession={onOpen}
      />,
    )
    expect(screen.getByTestId('session-message-card').getAttribute('data-direction')).toBe('outgoing')
    expect(screen.getByTestId('session-card-avatar').getAttribute('data-outgoing')).toBe('true')
    // The sender is named "This session", not by a title that reads as another
    // sender; the title stays on hover. Never a link to itself.
    const title = screen.getByTestId('session-card-title')
    expect(title.textContent).toBe('This session')
    expect(title).toHaveAttribute('title', 'wf-planner')
    expect(screen.queryByTestId('session-card-title-link')).not.toBeInTheDocument()
    // The direction is visible text, not an sr-only span or an icon.
    const direction = screen.getByTestId('session-card-direction')
    expect(direction.textContent).toBe('Sent to @Parent chat')
    expect(direction.closest('.sr-only')).toBeNull()
    const tag = screen.getByTestId('session-card-recipient')
    expect(direction).toContainElement(tag)
    fireEvent.click(tag)
    expect(onOpen).toHaveBeenCalledWith('chat-200')
  })

  it('draws no direction icon: the avatar holds only the initials', () => {
    render(<SessionMessageCard direction="outgoing" sender={sender} recipient={recipient} body="hi" />)
    const avatar = screen.getByTestId('session-card-avatar')
    expect(avatar.querySelector('svg')).toBeNull()
    expect(avatar.textContent).toBe('WF')
  })

  it('keeps the body to the message alone: the @tag sits in the header, not the body', () => {
    render(<SessionMessageCard direction="outgoing" sender={sender} recipient={recipient} body="plan is ready for review" />)
    const body = screen.getByTestId('session-card-body')
    expect(body).not.toContainElement(screen.getByTestId('session-card-recipient'))
    expect(body.textContent).toBe('plan is ready for review')
  })

  it('an incoming card carries no direction line', () => {
    render(<SessionMessageCard direction="incoming" sender={sender} body="hello" />)
    expect(screen.queryByTestId('session-card-direction')).not.toBeInTheDocument()
  })
})
