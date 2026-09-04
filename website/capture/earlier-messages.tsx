/**
 * Isolated capture entry for the top-of-transcript earlier-messages affordance.
 *
 * WHY ISOLATED: capture-older-loading-indicator.mjs already shoots this control in
 * the real shell, but only idle and loading in one theme; this entry adds the failed
 * state and the second theme, which have no other coverage.
 *
 * The marker is mounted above REAL message rows rather than on a blank field,
 * because the thing under review is where it sits relative to a transcript; a
 * control photographed alone shows styling and proves nothing about placement.
 * The rows are the production components against the real stylesheet; only their
 * text is invented.
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// i18next must be initialised or every label renders empty, and the marker is
// nothing but two translated labels — the frame would misrepresent the real UI.
import { initI18n } from '../src/i18n'
import EarlierMessagesBar from '../src/pages/chat/EarlierMessagesBar'
import UserMessage from '../src/pages/chat/UserMessage'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'idle'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

/** The first rows a reader sees under the marker after resuming a session. */
const TRANSCRIPT = [
  { role: 'user', text: 'Can you summarise where we left off?' },
  {
    role: 'assistant',
    text: 'We had narrowed it to the retry path, and you asked me to leave the timeout alone until the metric lands.',
  },
  { role: 'user', text: 'Right — keep it parked for now.' },
]

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <div className="bg-bg text-text" style={{ width: 660, padding: 16 }} data-capture-root>
        <EarlierMessagesBar
          loading={scene === 'loading'}
          failed={scene === 'failed'}
          onLoad={() => {}}
        />
        <div data-testid="capture-transcript">
          {TRANSCRIPT.map((m, i) =>
            m.role === 'user' ? (
              <UserMessage
                key={i}
                content={m.text}
                renderContent={(c) => <MarkdownRenderer content={c} />}
              />
            ) : (
              <AssistantMessage key={i} content={m.text} isStreaming={false} showFooter={false} />
            ),
          )}
        </div>
      </div>
    </QueryClientProvider>
  </MemoryRouter>,
)
