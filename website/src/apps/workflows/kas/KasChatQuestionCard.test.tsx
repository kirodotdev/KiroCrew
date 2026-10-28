/**
 * G2 — the chat's OWN Kiro Agent question card answers through the chat approve
 * route, not Allow / Reject. These fail if the card went back to sending a
 * trust/approve verb, or if parseKasQuestion stopped gating which permission
 * cards are question cards.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'

import { api } from '../../../api/client'
import { renderWithProviders } from '../../../test/helpers'
import { kasQuestion } from '../../../test/kasFixtures'
import { KasChatQuestionCard } from './KasChatQuestionCard'
import { parseKasQuestion } from '../../../components/chat-input/approval'

let approve: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  approve = vi.spyOn(api, 'approveChatSlot').mockResolvedValue({ ok: true })
})
afterEach(() => vi.restoreAllMocks())

describe('KasChatQuestionCard — answered in the composer (G2)', () => {
  it('answers through POST approve with action=answer, request_id and the chosen answers', async () => {
    renderWithProviders(<KasChatQuestionCard slot="chat-abc" requestId="req-7" question={kasQuestion({ key: 'req-7' })} />)
    const send = screen.getByRole('button', { name: /^send$/i })
    expect(send).toBeDisabled()
    fireEvent.click(screen.getByLabelText(/Everything flagged/))
    fireEvent.click(screen.getByLabelText('Lint'))
    fireEvent.click(screen.getByLabelText('Typecheck'))
    expect(send).toBeEnabled()
    fireEvent.click(send)
    await waitFor(() =>
      expect(approve).toHaveBeenCalledWith('chat-abc', 'answer', {
        request_id: 'req-7',
        answers: { scope: 'Everything flagged', checks: ['Lint', 'Typecheck'] },
      }),
    )
  })

  it('Dismiss sends action=rejected with the request id and no answers', async () => {
    renderWithProviders(<KasChatQuestionCard slot="chat-abc" requestId="req-7" question={kasQuestion({ key: 'req-7' })} />)
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    await waitFor(() => expect(approve).toHaveBeenCalledWith('chat-abc', 'rejected', { request_id: 'req-7' }))
  })
})

describe('parseKasQuestion — only a well-formed kas_question is a question card', () => {
  const good = { key: 'req-1', title: 'Pick one', questions: [{ id: 'a', prompt: 'A?', options: [{ label: 'x' }] }] }

  it('accepts an object or a JSON string', () => {
    expect(parseKasQuestion(good)?.key).toBe('req-1')
    expect(parseKasQuestion(JSON.stringify(good))?.questions).toHaveLength(1)
  })

  it('rejects a missing, malformed, keyless or empty payload (so the ordinary Allow/Reject card shows)', () => {
    expect(parseKasQuestion(undefined)).toBeNull()
    expect(parseKasQuestion('not json')).toBeNull()
    expect(parseKasQuestion({ questions: good.questions })).toBeNull()
    expect(parseKasQuestion({ key: 'x', questions: [] })).toBeNull()
  })
})
