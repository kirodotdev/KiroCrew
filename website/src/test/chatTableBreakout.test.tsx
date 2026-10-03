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
const TABLE_SELECTOR = '[data-role="assistant"] > .message-bubble:not([data-bordered]) > [data-image-scope] > div > .markdown-table'
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

  it('contains offscreen copy announcements in the table scroll wrapper', () => {
    const { container } = render(<ChatMessageList messages={[{ role: 'assistant', content: TABLE }]} running={false} />)
    // The scroller is the table root's direct child and owns `overflow-x-auto`;
    // the overflow fade is a `mask-image` on the scroller itself, so no wrapper
    // node sits between the root and the scroller.
    const scroll = container.querySelector(`${TABLE_SELECTOR} > .overflow-x-auto`)
    // Absolute sr-only status spans must have a containing block inside the
    // local scroller, or unclipping the bubble lets them widen the transcript.
    // The browser capture checks scrollWidth; this pins its layout prerequisite
    // without removing the live region that announces successful copies.
    expect(scroll).toHaveClass('relative')
    expect(scroll?.querySelector('[role="status"][aria-live="polite"]')).toHaveClass('sr-only')
  })

  it('keeps every breakout table out of the turn rail\'s band instead of hiding the rail', () => {
    // The rail is static: index.css clears the 40px band the minimap needs
    // (MIN_GUTTER_PX — its 28px button sits 8px in from the edge) on BOTH
    // sides, so the minimap never has to know tables exist. No table probe,
    // drag hold, focus hand-off or table mutation watch in the component.
    const minimap = readFileSync(resolve(here, '../pages/chat/TurnNavigationMinimap.tsx'), 'utf8')
    expect(minimap).toContain('const MIN_GUTTER_PX = 40')
    expect(minimap).not.toContain('.markdown-table')
    expect(minimap).not.toContain('holdOverTables')
    expect(minimap).not.toContain('measureRef')
    // 80px = 2 × MIN_GUTTER_PX: the smallest clearance that keeps a table out
    // of the 8..36px band the button paints into while matching the
    // free-gutter floor the rail itself applies (no wider, so tables keep
    // every pixel the rail does not need). In px, like the constant: a rem
    // clearance shrinks under a browser's smaller root font while the rail's
    // band does not.
    const clearance = css.match(/\.markdown-table \{[^}]*width: max\(100%, calc\(var\(--chat-pane-width, 100%\) - (\d+)px\)\)/)
    expect(clearance).not.toBeNull()
    expect(Number(clearance![1]) / 2).toBe(40)
  })

  it('breakout leaves inline code on the message\'s ordinary wrap rules', () => {
    // A pane-wide `word-break: normal` on table code made an identifier pill
    // unbreakable, so on a phone the column ran past the pane edge and the
    // pill clipped with no scroll cue. Widening a table must not change how
    // its code wraps: no code rule under the breakout selector, and the
    // inherited message rule still governs.
    expect(css).not.toMatch(new RegExp(`${TABLE_SELECTOR.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}[^{]*\\bcode\\b[^{]*\\{`))
    expect(css).toMatch(/\.msg-content :not\(pre\)>code\{[^}]*word-break:break-all/)
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

  it('keeps a bordered (crewmate) bubble\'s table inside its card', () => {
    // A crewmate's chat draws the reply as a bordered card via bubbleClassName;
    // a table breaking out of it paints past the card's edges.
    const { container } = render(<div className="chat-message-body"><AssistantMessage content={TABLE} isStreaming={false} bubbleClassName="bg-card border" /></div>)
    expect(container.querySelector('.message-bubble')).toHaveAttribute('data-bordered')
    expect(container.querySelector('.markdown-table')).not.toBeNull()
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
    expect(css).toContain(`width: max(100%, calc(var(${PANE_WIDTH_PROPERTY}, 100%) - 80px))`)
    expect(css).toContain(`margin-inline: min(0px, calc((100% - (var(${PANE_WIDTH_PROPERTY}, 100%) - 80px)) / 2))`)
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

it('colon-scopes chat heights by width without changing scroll-anchor identity', () => {
  const main = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
  const shared = readFileSync(resolve(here, '../chat-core/transcript/VirtualTranscript.tsx'), 'utf8')
  for (const host of [main, shared]) {
    // Colon-delimited: storageGc attributes `vc_heights_<scope>` to the session
    // before the first ':' — see chatTableHeightScope.test.tsx for the GC round trip.
    expect(host).toMatch(/`\$\{[^}]+\}:tables1(?::w|:\$\{)/)
  }
  expect(css).toContain('display: flow-root;')
})
