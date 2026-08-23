/**
 * Evidence capture for the thinking-block / tool-row layout unification.
 *
 * Renders ONE turn fragment the way ChatPage lays it out — assistant text, a
 * tool call (pill header + the REAL ToolDetails, expanded), the REAL
 * ThinkingBlock, closing text — so a before/after pair of this page shows
 * exactly what the change does and nothing else.
 *
 * The ThinkingBlock is the real component and mounts collapsed; the capture
 * script clicks it open. Only the pill HEADER is a replica (ToolCallLine needs
 * the redux store + toolLog wiring a static frame cannot honestly stub); its
 * class strings are copied verbatim from ToolCallLine.tsx.
 *
 * Query params: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { CircleDot } from 'lucide-react'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ToolDetails } from '../src/pages/chat/ToolDetails'
import ThinkingBlock from '../src/pages/chat/ThinkingBlock'
import { PanelRightSolid } from '../src/components/icons/panels'
import { ROW_PILL_BUTTON_CLASS, ROW_PILL_WRAPPER_CLASS } from '../src/pages/chat/rowPill'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme)

const TOOL_PURPOSE = '读取 shareUrl 构造细节以拼出会话深链'
const TOOL_NAME = 'Reading shareUrl.ts:1-45'
const TOOL_OUTPUT = `export function buildShareableUrl(
  slotKey: string,
  title?: string,
  messageTs?: string,
): string {
  const basePath = '/chat'
  const slug = title && title !== slotKey ? toSlug(title) : ''

  const params = new URLSearchParams()
  params.set('sid', slotKey)
  if (messageTs) params.set('msg', messageTs)

  const path = \`\${basePath}\${slug ? '/' + slug : ''}\`
  return \`\${window.location.origin}\${path}?\${params}\`
}`
const THINKING = `I need to construct a URL pointing to the chat interface using the session key, extracting just the relevant portion after the "dashboard_" prefix. Then I'll write a Playwright script to navigate there, locate the steered bubbles, and extract their geometry and HTML structure — though I should be mindful about what happens when opening the dashboard.

Writing Playwright script...`

/** Replica of ToolCallLine's pill header (done/success state). The geometry
 *  tokens come from the SAME rowPill.ts constants the real components render,
 *  so the harness cannot silently drift from the shipped left edge; only the
 *  colors/handlers are replicated (the real ToolCallLine needs redux + toolLog
 *  wiring a static frame cannot honestly stub). */
function ToolPillHeader() {
  return (
    <div className={`inline-flex items-start gap-1 ${ROW_PILL_WRAPPER_CLASS}`}>
      <button className={`inline-flex ${ROW_PILL_BUTTON_CLASS} cursor-pointer hover:brightness-110`}>
        <CircleDot size={12} className="shrink-0 text-ok" style={{ marginTop: '4px' }} />
        <span className="break-words min-w-0 leading-5 text-muted hover:text-text transition-colors">{TOOL_PURPOSE}</span>
      </button>
      <button
        className="pi-morph shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded font-mono text-[12px] leading-tight bg-bg-hover text-muted hover:text-accent hover:bg-accent/10 cursor-pointer transition-colors"
        style={{ marginTop: '1px' }}
      >
        <span className="max-w-[240px] truncate">shareUrl.ts</span>
        <PanelRightSolid size={12} className="shrink-0" />
      </button>
    </div>
  )
}

function Page() {
  return (
    <div className="min-h-screen bg-bg p-8 text-text" style={{ fontFamily: 'var(--sans)' }}>
      <div data-capture-root className="flex flex-col gap-1" style={{ width: 900 }}>
        <div className="text-[14px] leading-6 text-text mb-1">我先读一下分享链接的构造逻辑。</div>
        <div>
          <ToolPillHeader />
          <ToolDetails
            purpose={TOOL_PURPOSE}
            pillLabel={TOOL_PURPOSE}
            toolName={TOOL_NAME}
            input=""
            output={TOOL_OUTPUT}
            auto={false}
            pending={false}
            ts={Date.now()}
            hasEntry
            fmtTime={() => '13:47'}
            barColor="color-mix(in srgb, var(--ok) 70%, transparent)"
            layoutId="evidence-tool"
            flush
          />
        </div>
        <ThinkingBlock content={THINKING} />
        <div className="text-[14px] leading-6 text-text mt-1">接下来我会写 Playwright 脚本来验证。</div>
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Page />)
