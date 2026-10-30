/**
 * The sender avatar beside a delivered message's title wears the colour the
 * person gave THAT session: its custom hex, else its palette index; a session
 * with no colour, or a host with no store, draws a neutral circle.
 */
import { afterEach, describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import SenderAvatar from '../pages/chat/SenderAvatar'
import { createTestStore } from './helpers'
import { generatePalette, getContrastingTextColor } from '../utils/sessionColors'

const withSlots = (slots: unknown[], ui: React.ReactElement) =>
  render(<Provider store={createTestStore({ dashboard: { slots } } as never)}>{ui}</Provider>)

afterEach(() => {
  document.documentElement.style.removeProperty('--accent')
})

describe('SenderAvatar', () => {
  it('wears the sender session’s custom colour, with a readable foreground', () => {
    withSlots([{ key: 'chat-9', color_hex: '#2266cc' }], <SenderAvatar senderKey="chat-9" title="Slack Code" />)
    const avatar = screen.getByTestId('session-seed-sender-avatar')
    expect(avatar).toHaveTextContent('SC')
    expect(avatar).toHaveAttribute('data-session-color', 'true')
    expect(avatar.style.background.toLowerCase()).toMatch(/#2266cc|rgb\(34, 102, 204\)/)
    expect(avatar.style.color).not.toBe('')
    expect(getContrastingTextColor('#2266cc')).toBe('#ffffff')
  })

  it('wears the sender session’s palette colour when it has an index', () => {
    document.documentElement.style.setProperty('--accent', '#7c3aed')
    withSlots([{ key: 'chat-9', color_index: 2 }], <SenderAvatar senderKey="chat-9" title="Slack Code" />)
    const avatar = screen.getByTestId('session-seed-sender-avatar')
    expect(avatar).toHaveAttribute('data-session-color', 'true')
    const want = generatePalette('#7c3aed', 'horizon', '')[2]
    const probe = document.createElement('span')
    probe.style.background = want
    expect(avatar.style.background).toBe(probe.style.background)
  })

  it('is neutral for a session with no colour, another session’s colour never borrowed', () => {
    withSlots([{ key: 'chat-1', color_hex: '#2266cc' }, { key: 'chat-9' }], <SenderAvatar senderKey="chat-9" title="Planner" />)
    const avatar = screen.getByTestId('session-seed-sender-avatar')
    expect(avatar).toHaveAttribute('data-session-color', 'false')
    expect(avatar.className).toContain('bg-border')
  })

  it('is neutral without a store', () => {
    render(<SenderAvatar senderKey="chat-9" title="Planner" />)
    expect(screen.getByTestId('session-seed-sender-avatar')).toHaveAttribute('data-session-color', 'false')
  })
})
