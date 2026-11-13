import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { CommentThreadPopover } from '../components/CommentThreadPopover'
import { i18nT } from '../i18n/t'
import type { ArtifactComment } from '../types'

// Drive the voice result at the button boundary; positioning and notices are real.
vi.mock('../components/CommentVoiceButton', () => ({
  CommentVoiceButton: ({ onError }: { onError: (message: string | null) => void }) => (
    <button onClick={() => onError('Transcription failed')}>Fail dictation</button>
  ),
}))

const root: ArtifactComment = {
  id: 'r', origin: 'local', scope: 'private', author: 'alex', is_agent: false,
  body: 'a thread', thread_id: 'r', status: 'open', sync_state: 'local_only',
  created_at: '2026-06-10T00:00:00Z', updated_at: '2026-06-10T00:00:00Z',
}

afterEach(() => { vi.restoreAllMocks() })

describe('CommentThreadPopover voice notice positioning', () => {
  it('re-clamps a bottom-edge card when the voice notice appears and disappears', () => {
    // happy-dom has no layout: height follows the committed notice DOM.
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) {
      return this.getAttribute('role') === 'dialog' ? (this.querySelector('[role="alert"]') ? 160 : 100) : 0
    })
    render(
      <CommentThreadPopover
        comments={[root]} rootId="r"
        // Neither side of this tall anchor fits, so the viewport clamp applies.
        rect={{ x: 40, y: 40, w: 100, h: window.innerHeight - 80 }}
        onClose={vi.fn()} onReply={vi.fn()} onResolve={vi.fn()} onMarkReview={vi.fn()}
        onReopen={vi.fn()} onDelete={vi.fn()}
      />,
    )
    const card = screen.getByRole('dialog')
    const bottom = window.innerHeight - 8
    expect(Number.parseFloat(card.style.top) + card.offsetHeight).toBe(bottom)

    fireEvent.click(screen.getByRole('button', { name: 'Fail dictation' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Transcription failed')
    expect(card.offsetHeight).toBe(160)
    expect(Number.parseFloat(card.style.top) + card.offsetHeight).toBe(bottom)

    fireEvent.click(screen.getByRole('button', { name: i18nT('components.errorNotice.dismiss') }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(card.offsetHeight).toBe(100)
    expect(Number.parseFloat(card.style.top) + card.offsetHeight).toBe(bottom)
  })
})
