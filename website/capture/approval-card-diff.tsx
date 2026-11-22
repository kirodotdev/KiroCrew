/**
 * Isolated capture entry for an edit tool's diff on the two approval cards that
 * print a tool input in their own block: a Crew Mode worker's approval card and
 * the Command Center's attention card.
 *
 * Mounts the REAL CrewApprovalCards and AttentionCard against the real
 * stylesheet, theme tokens and i18n catalog. The worker list read is answered
 * by the capture script's route stub, so no gateway or token is involved.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import CrewApprovalCards from '../src/components/crew/CrewApprovalCards'
import AttentionCard from '../src/pages/chat/command-center/AttentionCard'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

const diff = [
  '--- /home/user/projects/kiro-crew/website/src/components/crew/CrewApprovalCards.tsx',
  '+++ /home/user/projects/kiro-crew/website/src/components/crew/CrewApprovalCards.tsx',
  '@@ -131,4 +131,4 @@',
  '   // The whole input the server sent, never a cut: Approve authorizes all of it,',
  '   // so the person must be able to read all of it (the region scrolls).',
  "-  const input = approval.tool_input || ''",
  "+  const input = approval.tool_input ?? '' // a long changed line that runs past the right edge of the card to show how it wraps",
  '   const headingId = `crew-approval-${worker}-${approval.request_id}`',
].join('\n')

store.dispatch(sseSlots([
  { key: 'root', title: 'Conductor', crew_mode_root: true },
  {
    key: 'w1', title: 'Fix the approval card', crew_root: 'root', pending_approval: true,
    pending_approval_info: {
      origin: 'native', request_id: 'r1', request_mid: 'm1', tool: 'Replace in File',
      tool_input: diff, tool_kind: 'edit', tool_purpose: 'Use ?? for the input fallback',
    },
  },
] as unknown as ChatSlot[]))

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div
          data-capture-root
          style={{ width: 560, margin: '16px auto', background: 'var(--bg)', padding: 16, display: 'flex', flexDirection: 'column', gap: 16 }}
        >
          <CrewApprovalCards root="root" />
          <AttentionCard
            item={{ id: 'approval:w1:r1', kind: 'approval', slot: 'w1', native: true, approvalMode: 'normal',
              approval: { id: 'r1', request_mid: 'm1', slot: 'dashboard:w1', tool: 'Replace in File', tool_input: diff, tool_purpose: 'Use ?? for the input fallback' } }}
            title="Fix the approval card"
          />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
