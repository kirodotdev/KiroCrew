import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { useRef } from 'react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { act, render } from '@testing-library/react'
import ChatMessageList from '../app-sdk/ChatMessageList'
import AssistantMessage from '../pages/chat/AssistantMessage'
import MarkdownRenderer from '../components/MarkdownRenderer'
import TranscriptScrollShell, { PANE_WIDTH_PROPERTY } from '../pages/chat/TranscriptScrollShell'

const TABLE = '| Signal | Value |\n| --- | --- |\n| `sample.daily.messages` | 42 |'
// The CSS matches the renderer's stable root wrapper, not all descendant
// tables: nested list/quote tables and formatted code cards must stay local.
const TABLE_SELECTOR = '[data-role="assistant"] > .message-bubble > [data-image-scope] > div > .markdown-table'
const here = dirname(fileURLToPath(import.meta.url))
const css = readFileSync(resolve(here, '../index.css'), 'utf8')

describe('transcript table breakout contract', () => {
  it('matches top-level tables through the actual shared message renderer', () => {
    const { container } = render(<ChatMessageList messages={[{ role: 'assistant', content: `Before.\n\n${TABLE}\n\nAfter.` }]} running={false} />)
    const table = container.querySelector(TABLE_SELECTOR)
    expect(table).not.toBeNull()
    expect(table?.closest('.chat-message-body')).not.toBeNull()
    expect(css).toContain(`${TABLE_SELECTOR} {`)
    expect(css).toContain(`.chat-message-body:has(${TABLE_SELECTOR}),`)
  })

  it('matches the main-page assistant without depending on the SDK extra wrapper', () => {
    const { container } = render(<div className="chat-message-body"><AssistantMessage content={TABLE} isStreaming={false} /></div>)
    expect(container.querySelector(`.chat-message-body:has(${TABLE_SELECTOR})`)).not.toBeNull()
    const main = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
    expect(main).toContain('chat-message-body flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full')
  })

  it('does not widen nested quotation/list tables or user messages', () => {
    const quoted = TABLE.split('\n').map(line => `> ${line}`).join('\n')
    const listed = `- Nested table\n\n${TABLE.split('\n').map(line => `  ${line}`).join('\n')}`
    const { container } = render(<ChatMessageList messages={[
      { role: 'assistant', content: `${quoted}\n\n${listed}` },
      { role: 'user', content: TABLE },
    ]} running={false} />)
    expect(container.querySelectorAll('table')).toHaveLength(3)
    expect(container.querySelector(TABLE_SELECTOR)).toBeNull()
  })

  it('leaves raw messages and standalone markdown outside the breakout selector', () => {
    const { container } = render(<>
      <MarkdownRenderer content={TABLE} />
      <MarkdownRenderer content={TABLE} rawMode />
    </>)
    expect(container.querySelectorAll('table')).toHaveLength(1)
    expect(container.querySelector(TABLE_SELECTOR)).toBeNull()
  })

  it('sizes against the published pane width, never an inline-size query container', () => {
    // The scroller must not become a containing block that could trap
    // McpAppFrame's in-place `position: fixed` full-screen sheet, so the width
    // reaches the tables as an inherited property, not via a query container.
    expect(css).not.toMatch(/\.chat-container\s*\{[^}]*container(?:-type|-name)?\s*:/)
    expect(css).not.toContain('@container chat-transcript')
    expect(css).toContain(`.chat-container ${TABLE_SELECTOR} {`)
    expect(css).toContain(`width: max(100%, calc(var(${PANE_WIDTH_PROPERTY}, 100%) - 2rem))`)
    expect(css).toContain(`margin-inline: min(0px, calc((100% - (var(${PANE_WIDTH_PROPERTY}, 100%) - 2rem)) / 2))`)
  })
})

describe('transcript scroll shell pane width', () => {
  class FakeResizeObserver {
    static instance: FakeResizeObserver | undefined
    disconnected = false
    constructor(readonly callback: ResizeObserverCallback) { FakeResizeObserver.instance = this }
    observe() {}
    disconnect() { this.disconnected = true }
    fire() { this.callback([], this as unknown as ResizeObserver) }
  }
  let paneWidth = 1220
  let originalResizeObserver: typeof ResizeObserver | undefined
  let clientWidthDescriptor: PropertyDescriptor | undefined

  beforeEach(() => {
    paneWidth = 1220
    FakeResizeObserver.instance = undefined
    originalResizeObserver = globalThis.ResizeObserver
    globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
    clientWidthDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth')
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => paneWidth })
  })
  afterEach(() => {
    globalThis.ResizeObserver = originalResizeObserver as typeof ResizeObserver
    if (clientWidthDescriptor) Object.defineProperty(HTMLElement.prototype, 'clientWidth', clientWidthDescriptor)
    else delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth
  })

  function Shell() {
    const scrollerRef = useRef<HTMLDivElement | null>(null)
    const topSentinelRef = useRef<HTMLDivElement | null>(null)
    const bottomSentinelRef = useRef<HTMLDivElement | null>(null)
    return (
      <TranscriptScrollShell scrollerRef={scrollerRef} onScroll={() => {}} loadingOlder={false}
        virt={{ topSentinelRef, bottomSentinelRef, offsetBefore: 0, offsetAfter: 0 }}>
        <div data-testid="row" />
      </TranscriptScrollShell>
    )
  }

  it('publishes the scroller clientWidth on the scroller and follows resizes', () => {
    const { container, unmount } = render(<Shell />)
    const scroller = container.querySelector<HTMLElement>('.chat-container')!
    expect(scroller.style.getPropertyValue(PANE_WIDTH_PROPERTY)).toBe('1220px')
    // Inheritance down to the rows and the measure farm is a browser fact jsdom
    // does not model; scripts/capture-chat-table-breakout.mjs measures it.

    act(() => {
      paneWidth = 1000
      FakeResizeObserver.instance?.fire()
    })
    expect(scroller.style.getPropertyValue(PANE_WIDTH_PROPERTY)).toBe('1000px')

    unmount()
    expect(FakeResizeObserver.instance?.disconnected).toBe(true)
  })
})

it('versions chat height scopes together without changing scroll-anchor identity', () => {
  const main = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
  const shared = readFileSync(resolve(here, '../chat-core/transcript/VirtualTranscript.tsx'), 'utf8')
  for (const host of [main, shared]) {
    // Colon-delimited: storageGc attributes `vc_heights_<scope>` to the session
    // before the first ':' — see chatTableHeightScope.test.tsx for the GC round trip.
    expect(host).toMatch(/`\$\{[^}]+\}:tables1(?::w|:\$\{)/)
  }
  expect(css).toContain('display: flow-root;')
})
