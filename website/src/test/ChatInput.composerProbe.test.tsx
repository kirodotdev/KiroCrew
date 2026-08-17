/**
 * The composer's stable focus probe.
 *
 * `queryComposer()` (and through it every focus-the-composer path: keyboard
 * shortcuts, new-chat flows, widget prefill, quote-to-compose) resolves the
 * textarea by `data-composer-input`. This locks the producer side of that
 * contract: the rendered ChatInput textarea must carry the attribute and be
 * exactly what the helper returns. The aria-label is translated at runtime, so
 * losing the attribute would not fail any label-based query in an English test
 * run — it would only no-op focus for non-English users in production.
 */
import { describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import ChatInput from '../components/ChatInput'
import { queryComposer } from '../pages/chat/composerFocus'
import { renderWithProviders } from './helpers'

describe('composer focus probe', () => {
  it('the rendered composer textarea is the element queryComposer resolves', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    const textarea = screen.getByLabelText('Message input')
    expect(textarea).toHaveAttribute('data-composer-input')
    expect(queryComposer()).toBe(textarea)
  })
})
