/**
 * Isolated capture entry for a ```error-report fence inside a user chat bubble
 * (capture/error-report-fence.html).
 *
 * WHY ISOLATED: the prompt under test is what the dashboard's error toast sends
 * when the user clicks "Ask the agent", so photographing it live means first
 * provoking a backend error against a running gateway. The bubble is a pure
 * function of `content`, so handing UserMessage the prompt the real builder
 * (utils/errorReport.prompt.ts) produces, rendered through the real
 * renderUserContent -> MarkdownRenderer -> useBlockAssembler -> CodeBlock path,
 * reaches the exact production pixels with no gateway.
 *
 * The same file renders in both checkouts, which is what makes the before/after
 * pair honest: on the base commit the fence is misparsed (label "error", clipped
 * line, phantom empty "code" block); on this branch it is one "error-report"
 * block whose line wraps. Nothing about the harness differs between the shots.
 *
 * Theme via query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n'
import UserMessage from '../src/pages/chat/UserMessage'
import { renderUserContent } from '../src/pages/chat/ChatPageMessageContent'
import { buildErrorPrompt } from '../src/utils/errorReport.prompt'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// The shape a real "Ask the agent" click produces: translated lead line, the
// untrusted-data note, then the fenced fact block carrying one long message.
const PROMPT = buildErrorPrompt(
  {
    message:
      "[Errno 16] Device or resource busy: '/home/user/.kiro/crew/scratch/runtime-9796a4b0/remote-default-main-baseline'",
  },
  'This error just came up in the Kiro Crew dashboard. Diagnose the root cause and fix it.',
)

const renderContent = (content: string, meta: Record<string, unknown> | undefined) =>
  renderUserContent({ content, meta, onFileOpen: () => {} })

function Scene() {
  return (
    <div data-capture-root className="bg-bg p-5 flex flex-col gap-2" style={{ width: 720 }}>
      <div className="text-[11px] text-muted font-mono">user bubble — the error toast's "Ask the agent" prompt</div>
      <div className="flex flex-col items-end group/msg">
        <UserMessage content={PROMPT} meta={{ mid: 'm-1' }} timestamp="13:27" renderContent={renderContent} />
      </div>
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Scene />
    </QueryClientProvider>
  </MemoryRouter>,
)
