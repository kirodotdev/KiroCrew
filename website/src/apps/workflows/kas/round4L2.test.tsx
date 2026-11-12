/**
 * Gap 13 (rich `_kiro/userInput` questions) and gap 3 (the
 * Edit & resend guard while a Kiro Workflows run is live). Each fails when its
 * fix is removed: the recommended answer is not pre-selected, sub-options are
 * not sent, the guard lets an edit over a live run through, or the disabled
 * edit still opens the editor.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import type { KasQuestionItem } from '../../../api/client'
import { renderWithProviders } from '../../../test/helpers'
import UserMessage from '../../../pages/chat/UserMessage'
import { KasQuestionForm, answerOf, initialDraft } from './KasQuestionCard'
import { kasRewindGuard } from './kasRewindGuard'

const rich: KasQuestionItem = {
  id: 'tc-1',
  prompt: 'How should I ship it?',
  options: [
    { label: 'Draft PR', description: 'Open it as a draft' },
    {
      label: 'Full PR',
      description: 'Ready for review',
      recommended: true,
      subOptionsLabel: 'Include:',
      subOptions: [{ label: 'Tests', description: 'unit tests' }, { label: 'Docs' }],
    },
  ] as KasQuestionItem['options'],
  multiSelect: false,
  freeText: false,
}

describe('rich questions (gap 13): the answer the form sends', () => {
  it('opens on the recommended answer with every sub-option on', () => {
    const draft = initialDraft(rich)
    expect(draft.picked).toEqual(['Full PR'])
    expect(answerOf(rich, draft)).toEqual(['Full PR', 'Tests', 'Docs'])
  })

  it('leaves out a sub-option switched off, and sends a plain label for an option without any', () => {
    expect(answerOf(rich, { picked: ['Full PR'], text: '', off: { 'Full PR': ['Tests'] } })).toEqual(['Full PR', 'Docs'])
    expect(answerOf(rich, { picked: ['Draft PR'], text: '' })).toBe('Draft PR')
  })

  it('pre-selects nothing when no answer is recommended', () => {
    const plain: KasQuestionItem = { ...rich, options: [{ label: 'A' }, { label: 'B' }] }
    expect(initialDraft(plain).picked).toEqual([])
  })
})

describe('rich questions (gap 13): the question form', () => {
  it('shows the badge and sub-options, is ready to send, and sends the toggles', () => {
    const submit = vi.fn()
    renderWithProviders(<KasQuestionForm question={{ key: 'req-1', questions: [rich] }} title="Q" pending={false} onSubmit={submit} onDismiss={() => {}} />)
    expect(screen.getByTestId('kas-question-recommended')).toHaveTextContent(/recommended/i)
    expect(screen.getByText('Ready for review')).toBeInTheDocument()
    const send = screen.getByRole('button', { name: /^send$/i })
    expect(send).toBeEnabled()
    fireEvent.click(screen.getByLabelText('Docs'))
    fireEvent.click(send)
    expect(submit).toHaveBeenCalledWith({ 'tc-1': ['Full PR', 'Tests'] })
  })

  it('asks a free-form question with a text box', () => {
    const submit = vi.fn()
    const free: KasQuestionItem = { id: 'tc-2', prompt: 'Which ticket?', options: [], multiSelect: false, freeText: true }
    renderWithProviders(<KasQuestionForm question={{ key: 'req-2', questions: [free] }} title="Q" pending={false} onSubmit={submit} onDismiss={() => {}} />)
    const send = screen.getByRole('button', { name: /^send$/i })
    expect(send).toBeDisabled()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'JIRA-12' } })
    fireEvent.click(send)
    expect(submit).toHaveBeenCalledWith({ 'tc-2': 'JIRA-12' })
  })
})

describe('the Edit & resend guard (gap 3)', () => {
  const at = '2026-05-21T16:00:02Z'
  const run = (status: 'running' | 'paused' | 'completed', createdAt: string | null, slotKey = 'chat-1') => ({ slotKey, createdAt, status })

  it('blocks while a run started at or after the message is live, a paused one included', () => {
    expect(kasRewindGuard([run('running', '2026-05-21T16:00:05Z')], 'chat-1', at)).toBe('blocked')
    expect(kasRewindGuard([run('paused', at)], 'chat-1', at)).toBe('blocked')
    expect(kasRewindGuard([run('running', null)], 'chat-1', at)).toBe('blocked')
  })

  it('notes an ended run, and ignores older runs and other chats', () => {
    expect(kasRewindGuard([run('completed', '2026-05-21T16:00:05Z')], 'chat-1', at)).toBe('ended')
    expect(kasRewindGuard([run('running', '2026-05-21T15:00:00Z')], 'chat-1', at)).toBe('none')
    expect(kasRewindGuard([run('running', '2026-05-21T16:00:05Z', 'chat-2')], 'chat-1', at)).toBe('none')
  })

  const renderContent = (content: string) => <span data-testid="content">{content}</span>

  it('a blocked message keeps its edit control, disabled, with the reason', () => {
    render(<UserMessage content="original" renderContent={renderContent} canEdit onEditResend={() => {}} editBlockedReason="Stop the workflow first." />)
    const pencil = screen.getByTitle('Stop the workflow first.')
    expect(pencil).toBeDisabled()
    fireEvent.click(pencil)
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('the note rides the editor when the runs have ended', () => {
    render(<UserMessage content="original" renderContent={renderContent} canEdit onEditResend={() => {}} editNote="Editing won't undo it." />)
    fireEvent.click(screen.getByTitle('Edit & Resend'))
    expect(screen.getByTestId('edit-note')).toHaveTextContent("Editing won't undo it.")
  })
})
