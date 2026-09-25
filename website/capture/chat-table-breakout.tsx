import { useRef } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ChatMessageList from '../src/app-sdk/ChatMessageList'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import McpAppFrame from '../src/components/McpAppFrame'
import { ThemeProvider } from '../src/hooks/useTheme'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import TranscriptScrollShell from '../src/pages/chat/TranscriptScrollShell'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

// Real shared message renderer and scroll shell, without a live gateway.
// The fixed left column makes pane width differ from viewport width.
const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'light'
document.documentElement.dataset.theme = `kiro-${theme}`
document.documentElement.dataset.mode = theme
const content = [
  '## Usage signals',
  '',
  'Keep this paragraph at the existing reading width. Tables can use the otherwise empty gutters without changing how ordinary messages read. The prose before and after the table must stay aligned.',
  '',
  '| # | Status | Priority | Subject | Signal | Type | Purpose | Key attributes |',
  '| --- | --- | --- | --- | --- | --- | --- | --- |',
  ...[
    ['credits', 'Credit analysis', 'Total credits spent today. Compare with the daily quota.', '`client.type`, `usage.limit`, `overage.cap`'],
    ['overage_credits', 'Credit analysis', 'Credits spent above the daily plan limit.', '`client.type`, `overage.enabled`'],
    ['messages', 'Usage', 'Messages sent today, a rough gauge of activity.', '`client.type`, `date`'],
    ['conversations', 'Usage', 'Chat threads started today, separate from message count.', '`client.type`, `date`'],
  ].map(([signal, subject, purpose, attrs], i) => `| ${i + 1} | Live | Shipped | ${subject} | \`sample.daily.${signal}\` | Daily counter | ${purpose} | ${attrs} |`),
  '',
  'This paragraph after the table stays at the same width as the paragraph above it.',
  '',
  '> A table inside a quotation keeps the quotation’s width.',
  '>',
  '> | Nested signal | Value |',
  '> | --- | --- |',
  '> | daily.messages | 42 |',
].join('\n')

function Message() {
  const source = params.has('tableOnly') ? content.slice(content.indexOf('| #'), content.indexOf('\n\nThis paragraph')) : content
  if (params.get('host') === 'main') {
    // ChatPage's row chain, including TurnBlock's collapsed-section wrapper.
    // Use the real AssistantMessage; only the page-owned wrapper is a fixture.
    return <div style={{ overflow: 'hidden' }}>
      <div className="mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width)' }}>
        <div data-content-column="" className="px-4 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width)' }}>
          <div className="group flex flex-col min-w-0">
            <div className="chat-message-body flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full">
              <AssistantMessage content={source} isStreaming={false} />
              {params.has('mcp') && <McpApp />}
            </div>
          </div>
        </div>
      </div>
    </div>
  }
  return <ChatMessageList messages={[{ role: 'assistant', content: source, ts: '2026-09-18T17:00:00Z' }]} running={false} />
}

// The real McpAppFrame where ToolCallLine mounts it: inside a transcript row,
// below the message body. Its full-screen sheet is `position: fixed` on the
// frame's own wrapper (never portaled), so any ancestor that becomes a
// containing block for fixed descendants — a `container-type` scroller — would
// centre the sheet on the scroller and clip it. The app keeps a click counter
// so the capture can prove the iframe (and its state) survives open/close.
const queryClient = new QueryClient()
function McpApp() {
  return <QueryClientProvider client={queryClient}><ThemeProvider>
    <McpAppFrame payload={{
      session_key: 'capture', tool_call_id: 'call-1', server: 'capture', tool: 'counter', spool_id: 'spool-1', csp: null, permissions: null,
      html: '<!doctype html><html><body style="margin:0;font:16px sans-serif"><button id="count" style="font:inherit;padding:8px 16px" onclick="this.textContent=String(Number(this.textContent)+1)">0</button></body></html>',
    }} />
  </ThemeProvider></QueryClientProvider>
}

function Scene() {
  const scrollerRef = useRef<HTMLDivElement>(null)
  const topSentinelRef = useRef<HTMLDivElement>(null)
  const bottomSentinelRef = useRef<HTMLDivElement>(null)
  return <div className="flex h-screen bg-bg text-text">
    <aside className="hidden md:block w-[280px] shrink-0 border-r border-border bg-panel p-4 text-muted">Sessions</aside>
    <main className="flex flex-col flex-1 min-w-0 min-h-0" style={{ '--mc-content-width': params.get('cap') || '800px' } as React.CSSProperties}>
      <TranscriptScrollShell scrollerRef={scrollerRef} onScroll={() => {}} virt={{ topSentinelRef, bottomSentinelRef, offsetBefore: 0, offsetAfter: 0 }} loadingOlder={false} headerSpacer={false}>
        <div data-live-fixture><Message /></div>
        {/* MeasureFarm's zero-height box, inside the real scroll shell. */}
        <div data-farm-fixture style={{ height: 0, overflow: 'hidden', visibility: 'hidden', pointerEvents: 'none', overflowAnchor: 'none' }}><Message /></div>
        <div data-user-fixture>
          <ChatMessageList messages={[{ role: 'user', content: '| Prompt table | Value |\n| --- | --- |\n| Keep inside bubble | 42 |', ts: '2026-09-18T16:59:00Z' }]} running={false} />
        </div>
      </TranscriptScrollShell>
      <div data-composer-fixture className="mx-auto w-full px-4 py-2" style={{ maxWidth: 'var(--mc-content-width)' }}>
        <div className="rounded-xl border border-border bg-card text-card-fg p-3">Composer — unchanged width</div>
      </div>
    </main>
    <div data-outside-fixture className="hidden"><MarkdownRenderer content={content} /></div>
  </div>
}

await initI18n()
createRoot(document.getElementById('root')!).render(<Scene />)
