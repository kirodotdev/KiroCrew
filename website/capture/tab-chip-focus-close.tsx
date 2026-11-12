/**
 * Isolated capture entry for an inactive side-panel tab chip under keyboard
 * focus: its close button is hidden until hover, and keyboard focus in the chip
 * should reveal it the way the session and embed tab strips do.
 *
 * Mounts the REAL TabChip (an active chip and an inactive one) against the real
 * stylesheet; the capture script focuses the inactive chip from the keyboard.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { initI18n } from '../src/i18n/all'
import { TabChip } from '../src/pages/chat/SidePanel'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

await initI18n()

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <div className="bg-bg text-text p-6" data-capture-root>
      <div role="tablist" className="flex items-end gap-1 border-b border-border w-[420px]">
        <TabChip tab={{ title: 'notes.md', kind: 'file', path: 'notes.md' }} active onSelect={() => {}} onClose={() => {}} />
        <TabChip tab={{ title: 'build.log', kind: 'file', path: 'build.log' }} active={false} onSelect={() => {}} onClose={() => {}} testId="inactive-chip" />
      </div>
    </div>
  </QueryClientProvider>,
)
