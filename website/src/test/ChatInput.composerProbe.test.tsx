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
 *
 * The split-view case locks the consumer side against REAL rendered
 * composers: with two ChatInputs mounted in two `[data-chat-pane]` wrappers
 * (the shape ChatPane renders in the session grid), the lookup must resolve
 * the composer of the pane holding focus, not the first one in document
 * order.
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

  it('with two panes mounted, resolves the composer of the pane holding focus', () => {
    renderWithProviders(
      <>
        <div data-chat-pane="">
          <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />
        </div>
        <div data-chat-pane="">
          <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />
        </div>
      </>,
    )
    const composers = screen.getAllByLabelText('Message input')
    expect(composers).toHaveLength(2)
    // Focus in the SECOND pane: the document-global first match would be
    // composers[0]; the pane-scoped lookup must return composers[1].
    composers[1].focus()
    expect(queryComposer()).toBe(composers[1])
    // And in single-pane terms: focus in the first pane resolves the first.
    composers[0].focus()
    expect(queryComposer()).toBe(composers[0])
  })
})
