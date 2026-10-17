/**
 * Evidence for the pinned app registry `label` / `review` tier.
 *
 * THE PROBLEM: an edition that pins two registries — one its team curates, one
 * contributors list — rendered both identically. The External Registries card
 * read the `trust` field alone, so both got a shield plus "Trusted source", and
 * the App Store SOURCES rail showed the bare registry id with a neutral
 * Database icon. A user could not tell which source anybody had read.
 *
 * The scene mounts the REAL `RegistryManager` and the REAL `CategoryRail`
 * against the real stylesheet, theme tokens and live i18n catalog. Reaching this
 * state in the shell needs a build that actually pins two registries, which the
 * public edition does not; nothing here re-implements either component, its
 * classes, or its strings. `api.listRegistries` is the ONE seam stubbed, with
 * exactly the payload `GET /api/apps/registries` returns.
 *
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../src/api/client'
import RegistryManager from '../src/components/RegistryManager'
import CategoryRail from '../src/components/appstore/CategoryRail'
import { orderByReview } from '../src/components/appstore/registryOrder'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

/** Exactly the `pinned` rows `GET /api/apps/registries` reports, community FIRST
 *  so the frame also documents that the display order is the helper's, not the
 *  backend's. */
const PINNED = [
  {
    name: 'community',
    repo: 'https://git.example.com/kiro/community-app-registry.git',
    branch: 'mainline',
    trust: 'owner',
    label: 'Community apps',
    review: 'community',
  },
  {
    name: 'internal',
    repo: 'https://git.example.com/kiro/app-registry.git',
    branch: 'mainline',
    trust: 'owner',
    label: 'Internal apps',
    review: 'curated',
  },
]

const OPERATOR = [
  { name: 'my-team', repo: 'https://git.example.com/me/my-apps.git', branch: 'main', trust: 'index', label: '', review: '' },
]

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

initI18n('en')

// The one seam: the HTTP read. Everything the components then do with the
// payload — label vs id, badge choice, tip copy, ordering — is shipped code.
api.listRegistries = () => Promise.resolve({ registries: OPERATOR, pinned: PINNED })
api.refreshRegistries = () => Promise.resolve({ ok: true })

/** The rail rows exactly as `useAppsData` builds them: built-in first, then the
 *  registries through the shared order helper, counting by the registry ID. */
const COUNTS: Record<string, number> = { internal: 12, community: 30, 'my-team': 2 }
const SOURCES = [
  { name: '__builtin__', label: 'Built-in Kiro Crew', count: 8, builtin: true },
  ...orderByReview([...PINNED, ...OPERATOR]).map(r => ({
    name: r.name,
    label: r.label || r.name || r.repo,
    count: COUNTS[r.name] ?? 0,
    builtin: false,
    review: r.review,
  })),
]

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <div data-capture-root className="bg-bg text-text p-5 flex items-start gap-6">
      <div data-capture-card className="w-[700px]">
        <RegistryManager />
      </div>
      <div data-capture-rail className="w-[240px] bg-bg-elevated rounded-xl p-3">
        <CategoryRail
          categories={[]}
          total={52}
          selected="all"
          onSelect={() => {}}
          sources={SOURCES}
          selectedSource={null}
          onSelectSource={() => {}}
          onAddSource={() => {}}
        />
      </div>
    </div>
  </QueryClientProvider>,
)
