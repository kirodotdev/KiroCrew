/**
 * A Bot's chat draws a blocked link the way the chat page does.
 *
 * The Bot's chat is a `ChatPane`, whose rows go through the SHARED bubble
 * (`renderAssistantBubble`). That bubble must hand the reply's
 * `meta.link_blocks` to the markdown renderer, or each blocked link renders
 * live with no chip, no card and no "Allow for this host". A host drawing
 * another gateway's rows names no local slot: its links still draw as chips,
 * and their card offers no Allow, since nothing local can hold it.
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
const RECORD = {
  url: `https://${HOST}/reviews?filter=abc`,
  domain: HOST,
  rule: 'exfil_query_length',
  query_chars: 290,
  withheld: null,
}

const REPLY: ChatMessage = {
  role: 'assistant',
  content: `The page: ${RECORD.url}`,
  cls: '',
  ts: '2026-11-13T09:00:00Z',
  meta: { link_blocks: [RECORD] },
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
    expect(container.querySelector(`a[href="${RECORD.url}"]`)).toBeNull()
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
    // The server serves the same text without its record. Both pane renderers
    // pass redactionSlot without slotKey, but must keep the same reply's Undo.
    const restored = { ...REPLY, meta: {} }
    rerender(<>{entry!.render(restored, ctx([restored]))}</>)
    expect(getByTestId('blocked-link-feedback')).toHaveTextContent(`Allowed ${HOST} for this workspace`)
    expect(container.querySelector(`a[href="${RECORD.url}"]`)).not.toBeNull()
    fireEvent.click(getByTestId('blocked-link-undo'))
    await waitFor(() => expect(getByTestId('blocked-link-feedback')).toHaveTextContent('Undone'))
    expect(revoke).toHaveBeenCalledWith('default', HOST)
  })

  it('draws the chip with no Allow on a host that names no local slot (another gateway)', () => {
    const { getByTestId, queryByTestId, container } = render(<>{renderAssistantBubble(REPLY, ctx([REPLY]))}</>)
    expect(getByTestId('blocked-link-chip')).toBeTruthy()
    expect(container.querySelector(`a[href="${RECORD.url}"]`)).toBeNull()
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(getByTestId('blocked-link-card')).toBeTruthy()
    expect(queryByTestId('blocked-link-allow')).toBeNull()
  })
})
