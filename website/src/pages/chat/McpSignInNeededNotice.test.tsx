import { afterEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import { i18next } from '../../i18n/all'
import McpSignInNeededNotice, { isMcpSignInNeededNotice } from './McpSignInNeededNotice'
import { createTranscriptRenderers } from './transcriptRenderers'
import type { ChatMessage } from '../../types'

const ROW: ChatMessage = {
  role: 'notice',
  content: '⚠ notes (an MCP server you added) needs you to sign in before its tools work in this chat. Send a message that names notes, such as "Sign in to notes".',
  meta: { kind: 'mcp_sign_in_needed', servers: ['notes'], more: 0 },
}

const SEVERAL: ChatMessage = {
  ...ROW,
  content: '⚠ These MCP servers you added need you to sign in …',
  meta: { kind: 'mcp_sign_in_needed', servers: ['s0', 's1', 's2', 's3', 's4'], more: 2 },
}

function draw(m: ChatMessage) {
  return render(
    <MemoryRouter>
      <McpSignInNeededNotice content={m.content} meta={m.meta} />
    </MemoryRouter>,
  )
}

function noticeText(): string {
  return screen.getByTestId('notice-card-text').textContent ?? ''
}

describe('McpSignInNeededNotice', () => {
  afterEach(async () => {
    await i18next.changeLanguage('en')
  })

  it('says what the server is and names the chat way in, with no link', () => {
    draw(ROW)
    const text = noticeText()
    expect(text).toContain('notes (an MCP server you added) needs you to sign in before its tools work in this chat.')
    expect(text.trim().endsWith('Send a message that names notes, such as "Sign in to notes".')).toBe(true)
    expect(text).not.toContain('MCP Servers')
    expect(screen.queryAllByRole('link')).toHaveLength(0)
  })

  it('draws the sentence in the reader language from the row meta', async () => {
    await i18next.changeLanguage('de')
    draw(ROW)
    const text = noticeText()
    expect(text).toContain('notes (ein MCP-Server, den du hinzugefügt hast) braucht eine Anmeldung')
    expect(text).toContain('bevor seine Werkzeuge in diesem Chat funktionieren')
    expect(text).not.toContain('Tools')
    expect(text.trim().endsWith('zum Beispiel „Bei notes anmelden“.')).toBe(true)
    expect(screen.queryAllByRole('link')).toHaveLength(0)
  })

  it('names several servers and counts the rest in the reader language', async () => {
    await i18next.changeLanguage('de')
    draw(SEVERAL)
    const text = noticeText()
    expect(text).toContain('Diese MCP-Server, die du hinzugefügt hast, brauchen eine Anmeldung')
    expect(text).toContain('s0, s1, s2, s3, s4 und 2 weitere.')
    expect(text).toContain('Jeder braucht eine eigene Anmeldung.')
    expect(text.trim().endsWith('zum Beispiel „Bei s0 anmelden“.')).toBe(true)
    expect(text).not.toContain('Tools')
    expect(screen.queryAllByRole('link')).toHaveLength(0)
  })

  it('says each of several servers needs its own sign-in', () => {
    draw(SEVERAL)
    const text = noticeText()
    // Naming several in one message still starts one sign-in at a time.
    expect(text).toContain('Each one needs its own sign-in. Send a message that names it, such as "Sign in to s0"')
    expect(text).not.toContain('names one of them')
  })

  it('lets a long server name wrap instead of running past a narrow chat', () => {
    const name = 'n'.repeat(128)
    draw({ ...ROW, meta: { ...ROW.meta, servers: [name] } })
    const text = screen.getByTestId('notice-card-text')
    expect(text.textContent).toContain(name)
    expect(text.querySelectorAll('.whitespace-nowrap')).toHaveLength(0)
  })

  it('shows the row text when the meta names no server', () => {
    draw({ ...ROW, meta: { kind: 'mcp_sign_in_needed' } })
    expect(noticeText()).toContain('notes (an MCP server you added) needs you to sign in')
  })

  it('claims only a notice row of its kind', () => {
    expect(isMcpSignInNeededNotice(ROW)).toBe(true)
    expect(isMcpSignInNeededNotice({ ...ROW, meta: { kind: 'crew_paused_notice' } })).toBe(false)
    expect(isMcpSignInNeededNotice({ ...ROW, meta: undefined })).toBe(false)
    expect(isMcpSignInNeededNotice({ ...ROW, role: 'assistant' })).toBe(false)
  })

  it('is the row the chat page draws for that kind, ahead of the plain notice', () => {
    const renderers = createTranscriptRenderers({})
    const claimed = renderers.find(r => r.roles.includes('notice') && (r.match ? r.match(ROW) : true))
    expect(claimed?.id).toBe('mcp_sign_in_needed')
  })
})
