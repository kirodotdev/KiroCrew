/**
 * A Bot's chat draws a blocked link the way the chat page does (#36862).
 *
 * The Bot's chat is a `ChatPane`, whose rows go through the SHARED bubble
 * (`renderAssistantBubble`). That bubble must hand the reply's
 * `meta.blocked_links` to the markdown renderer, or each removed link stays as
 * its raw placeholder text with no chip, no card and no "Allow for this host".
 * A host drawing another gateway's rows names no local slot and keeps the
 * plain text, as it does for the lock tags.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/react'
import type { ChatMessage } from '../types'
import { api } from '../api/client'
import { mergeRenderers, renderAssistantBubble, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import { setAllowHold } from '../components/RedactionCards'

vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))

afterEach(() => {
  cleanup()
  setAllowHold(`member-radar|${REPLY.ts}`, HOST, null)
  vi.restoreAllMocks()
})

const HOST = 'reviews.corp.example'
const PH = `[REDACTED: suspicious URL to ${HOST}]`
const RECORD = {
  domain: HOST,
  rule: 'exfil_query_length',
  path: '/reviews',
  query_chars: 290,
  url: `https://${HOST}/reviews?filter=abc`,
  url_withheld: null,
}

const REPLY: ChatMessage = {
  role: 'assistant',
  content: `The page: ${PH}`,
  cls: '',
  ts: '2026-11-13T09:00:00Z',
  meta: { blocked_links: [RECORD] },
} as ChatMessage

const ctx = (messages: ChatMessage[]): MessageRenderContext => ({
  index: 0,
  messages,
  running: false,
  key: 'k0',
  hideCardOwnedOAuth: false,
  autoDeniedIds: new Set<string>(),
  wrapper: (children) => children,
  row: (children) => children,
})

describe("a blocked link in a Bot's chat", () => {
  it.each([true, false])('draws the card and reloads its reply after Allow (Bot: %s)', async (crewmate) => {
    const allow = vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
    const revoke = vi.spyOn(api, 'redactionRevokeHost').mockResolvedValue({ ok: true, removed: true })
    const events = vi.spyOn(window, 'dispatchEvent')
    const entry = resolveRenderer(REPLY, mergeRenderers(createTranscriptRenderers({
      slot: 'member-radar', revealSlot: 'member-radar', crewmate: crewmate ? { name: 'Radar' } : undefined, crewmateTranscript: () => [REPLY],
    })))
    expect(entry?.id).toBe('assistant')
    const { getByTestId, container, rerender } = render(<>{entry!.render(REPLY, ctx([REPLY]))}</>)
    expect(container.textContent).not.toContain(PH)
    expect(getByTestId('blocked-link-target').textContent).toBe(HOST)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(getByTestId('blocked-link-card').textContent).toContain(`Destination: ${HOST}`)
    fireEvent.click(getByTestId('blocked-link-allow'))
    fireEvent.click(getByTestId('blocked-link-allow-confirmed'))
    await waitFor(() => expect(events).toHaveBeenCalledWith(expect.objectContaining({
      type: 'mc:redaction-hosts-changed', detail: expect.objectContaining({ slot: 'member-radar', messageTs: REPLY.ts }),
    })))
    expect(allow).toHaveBeenCalledWith('member-radar', HOST)
    await waitFor(() => expect(getByTestId('blocked-link-feedback')).toHaveTextContent(`Allowed ${HOST} for this workspace`))
    // The server restores the URL and removes its record. Both pane renderers
    // pass redactionSlot without slotKey, but must keep the same reply's Undo.
    const restored = { ...REPLY, content: `The page: ${RECORD.url}`, meta: {} }
    rerender(<>{entry!.render(restored, ctx([restored]))}</>)
    expect(getByTestId('blocked-link-feedback')).toHaveTextContent(`Allowed ${HOST} for this workspace`)
    expect(container.querySelector(`a[href="${RECORD.url}"]`)).not.toBeNull()
    fireEvent.click(getByTestId('blocked-link-undo'))
    await waitFor(() => expect(getByTestId('blocked-link-feedback')).toHaveTextContent('Undone'))
    expect(revoke).toHaveBeenCalledWith('default', HOST)
  })

  it('stays plain text on a host that names no local slot (another gateway)', () => {
    const { queryByTestId, container } = render(<>{renderAssistantBubble(REPLY, ctx([REPLY]))}</>)
    expect(queryByTestId('blocked-link-chip')).toBeNull()
    expect(container.textContent).toContain(PH)
  })
})
