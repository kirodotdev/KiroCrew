/**
 * The Crew Mode dispatch hint row (`meta.kind === 'crew_dispatch_hint'`): the
 * gateway writes one `notice` row into a Crew Mode conductor's chat per person
 * turn in which it started a worker (`crew_dispatch_hint.py`). The shared row
 * set draws it localized, with a button that opens the Crew tab when the host
 * can; every other notice keeps the plain notice card.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import i18next from 'i18next'
import '../i18n/all'
import type { ReactElement } from 'react'
import { mergeRenderers, resolveRenderer, defaultMessageRenderers, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import { CREW_DISPATCH_HINT_KIND } from '../pages/chat/CrewDispatchHintCard'
import type { ChatMessage } from '../types'

const SERVER_TEXT = 'Crew started work. Open the Crew tab in the right-side panel to follow it.'

const hint = (): ChatMessage => ({
  role: 'notice', content: SERVER_TEXT, cls: 'msg msg-info', meta: { kind: CREW_DISPATCH_HINT_KIND },
})

const ctx: MessageRenderContext = {
  index: 0,
  messages: [],
  running: false,
  key: 'k0',
  hideCardOwnedOAuth: false,
  autoDeniedIds: new Set<string>(),
  wrapper: (children) => children,
  row: (children) => children,
}

function draw(m: ChatMessage, opts: Parameters<typeof createTranscriptRenderers>[0] = {}) {
  const entry = resolveRenderer(m, mergeRenderers(createTranscriptRenderers(opts)))
  return { id: entry?.id, node: entry?.render(m, ctx) as ReactElement }
}

describe('Crew Mode dispatch hint row', () => {
  it('draws the hint with a button that opens the Crew tab', () => {
    const onOpenCrewWorkers = vi.fn()
    const { id, node } = draw(hint(), { onOpenCrewWorkers })
    expect(id).toBe('crew_dispatch_hint')
    render(node)
    expect(screen.getByTestId('crew-dispatch-hint')).toHaveTextContent(SERVER_TEXT)
    fireEvent.click(screen.getByTestId('crew-dispatch-hint-open'))
    expect(onOpenCrewWorkers).toHaveBeenCalledTimes(1)
  })

  it('draws the text alone when the host cannot open the panel (a pane)', () => {
    render(draw(hint()).node)
    expect(screen.getByTestId('crew-dispatch-hint')).toBeInTheDocument()
    expect(screen.queryByTestId('crew-dispatch-hint-open')).toBeNull()
  })

  it('speaks the person\'s language', async () => {
    try {
      await i18next.changeLanguage('zh-CN')
      render(draw(hint(), { onOpenCrewWorkers: () => {} }).node)
      expect(screen.getByText('团队已开始工作。打开右侧面板的“团队”标签查看进度。')).toBeInTheDocument()
      expect(screen.getByTestId('crew-dispatch-hint-open')).toHaveTextContent('打开团队标签')
    } finally {
      await i18next.changeLanguage('en')
    }
  })

  it('leaves every other notice on the plain notice card', () => {
    const plain: ChatMessage = { role: 'notice', content: 'This session was handed over.', cls: 'msg msg-info' }
    const { id, node } = draw(plain, { onOpenCrewWorkers: () => {} })
    expect(id).toBe('notice')
    render(node)
    expect(screen.queryByTestId('crew-dispatch-hint')).toBeNull()
    expect(screen.getByTestId('notice-card')).toHaveTextContent('This session was handed over.')
  })

  it('a non-notice row carrying the kind is not a hint', () => {
    const assistant: ChatMessage = { role: 'assistant', content: 'hi', cls: 'msg msg-a', meta: { kind: CREW_DISPATCH_HINT_KIND } }
    expect(draw(assistant).id).not.toBe('crew_dispatch_hint')
  })

  it('a store-free surface still shows the gateway\'s own text', () => {
    const entry = resolveRenderer(hint(), defaultMessageRenderers)
    expect(entry?.id).toBe('notice')
    render(entry!.render(hint(), ctx) as ReactElement)
    expect(screen.getByTestId('notice-card')).toHaveTextContent(SERVER_TEXT)
  })
})
