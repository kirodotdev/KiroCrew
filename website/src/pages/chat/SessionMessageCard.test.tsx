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

  it('draws an outlined avatar with the arrow, the recipient @tag, and sr-only direction', () => {
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
    const tag = screen.getByTestId('session-card-recipient')
    expect(tag.textContent).toBe('@Parent chat')
    fireEvent.click(tag)
    expect(onOpen).toHaveBeenCalledWith('chat-200')
    expect(screen.getByTestId('session-card-direction-sr').textContent).toBe('Sent to Parent chat')
    // Outgoing shows the sender's own title, not a link to itself.
    expect(screen.queryByTestId('session-card-title-link')).not.toBeInTheDocument()
    expect(screen.getByTestId('session-card-title').textContent).toBe('wf-planner')
  })

  it('keeps the @tag on the first line: the rendered markdown root and its paragraph go inline', () => {
    render(<SessionMessageCard direction="outgoing" sender={sender} recipient={recipient} body="plan is ready for review" />)
    const body = screen.getByTestId('session-card-body')
    // The tag leads the body, before the markdown root.
    expect(body.firstElementChild).toBe(screen.getByTestId('session-card-recipient'))
    // Tailwind arbitrary variants: both the renderer's own root <div> and its <p> must be inline.
    expect(body.className).toMatch(/\[&>div\]:inline/)
    expect(body.className).toMatch(/\[&_p\]:inline/)
  })

  it('leaves an incoming body as normal blocks', () => {
    render(<SessionMessageCard direction="incoming" sender={sender} body="hello" />)
    expect(screen.getByTestId('session-card-body').className).not.toMatch(/:inline/)
  })
})
