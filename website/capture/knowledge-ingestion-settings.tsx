/**
 * Isolated capture entry for the Knowledge → Settings (Ingestion Settings) tab.
 *
 * WHY ISOLATED: reaching /knowledge?tab=settings through the full SPA needs a live
 * gateway plus a dashboard credential; without one the shell renders its prerequisite
 * gate instead of the tab, which is worse evidence than none. This mounts the REAL
 * SettingsTab against the REAL stylesheet, theme tokens and live i18n catalog, with a
 * server snapshot seeded into the same ['kirocrewConfig'] query key the tab reads in
 * production — so the rows, order and copy are the shipped ones.
 *
 * Theme comes from the query string: ?theme=dark|light
 *
 * The seeded config deliberately carries the keys the folder auto-registration
 * feature used (auto_register_project_docs, auto_ingest_chunk_budget, max_sources)
 * as well as the surviving ones. A build that still reads them renders their rows
 * from this snapshot; a build that removed them ignores the extra keys. One harness
 * therefore shoots both the before and the after frame, and the row set in the image
 * is the component's answer rather than the fixture's.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { getAdapter } from '../src/providers/registry'
import { SettingsTab } from '../src/pages/knowledge/SettingsTab'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.dataset.theme = theme
document.documentElement.classList.toggle('dark', theme === 'dark')

const KNOWLEDGE_CONFIG = {
  auto_add_documents: false,
  auto_ingest_artifacts: false,
  // Retired with the folder auto-registration paths; kept in the fixture so the
  // pre-change build renders its own rows from a realistic snapshot.
  auto_register_project_docs: false,
  auto_ingest_chunk_budget: 150,
  max_sources: 50,
  // Surviving knobs.
  embed_rate_limit: 120,
  extraction_model: '',
  extraction_pool_size: 3,
}

// `staleTime: Infinity` + `refetchOnMount: false` matter: the tab's query refetches
// on mount otherwise, the harness has no gateway to answer it, and the failed
// refetch replaces the seeded snapshot with the "Failed to load settings." card.
const qc = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false } },
})
qc.setQueryData(['kirocrewConfig'], { knowledge: KNOWLEDGE_CONFIG })
// The model dropdown reads the shared list through useAvailableModels, whose key is
// scoped by provider id. Seeding it keeps the picker on its real Auto-first shape
// instead of the placeholder a failed fetch would leave.
qc.setQueryData(['available-models', getAdapter().id], [
  { name: 'auto', description: '' },
  { name: 'claude-haiku-4.5', description: 'Fast, cheap extraction' },
  { name: 'claude-sonnet-5', description: 'Default chat model' },
])

await initI18n()

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        {/* Width mirrors the Knowledge page's content column so wrapping matches production. */}
        <div data-capture-root className="bg-bg text-text min-h-screen p-8">
          <div className="max-w-[760px]">
            <SettingsTab />
          </div>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
