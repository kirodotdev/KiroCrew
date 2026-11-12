/** Isolated capture entry for the publication badge provider name (#32280).
 *
 * WHY ISOLATED: the library needs published artifacts, one with a failed sync,
 * and a publish-provider list. Here the REAL `LibraryMasonry` card and the REAL
 * `ArtifactRow` mount over a fetch stub that answers those reads:
 *
 * - top: two gallery cards, one published to "Acme Drive", one whose sync
 *   failed (its `last_error` renders through `ErrorNotice`).
 * - bottom: the same two artifacts as library table rows.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import { LibraryMasonry, type GridEntry } from '../src/pages/ArtifactsPageGallery'
import { ArtifactRow } from '../src/components/library/LibraryTable'
import type { Artifact } from '../src/types'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const pub = (last_error = '') => ({
  provider: 'acme_drive', visibility: 'PRIVATE', shared_with: [], auto_sync: true,
  last_synced_kirocrew_version: 1, version_map: {}, published_at: '2026-11-07T00:00:00Z',
  published_by: 'me', last_error, notice: '',
})
const mk = (slug: string, name: string, last_error = '') => ({
  slug, name, kind: 'markdown', version: 2, source: 'chat', description: 'Capture fixture', tags: [],
  pinned: false, created_at: '2026-11-07T00:00:00Z', updated_at: '2026-11-07T01:00:00Z',
  publication: pub(last_error),
}) as unknown as Artifact
const ok = mk('weekly-report', 'Weekly report')
const failed = mk('roadmap', 'Roadmap', 'destination conflict: a newer version exists in Acme Drive')
const CONTENT = '# Weekly report\n\nShipped the review workflow.\n\n- Pilot with one team\n- Measure adoption'

/** API stub: the provider list, each artifact's content, `{}` for any other read. */
const passThrough = window.fetch.bind(window)
window.fetch = (input, init) => {
  const href = String(input instanceof Request ? input.url : input)
  if (!href.includes('/api/')) return passThrough(input, init)
  const body = href.includes('/api/artifacts/publish-providers')
    ? { kind: 'widget', providers: [{ name: 'acme_drive', display_name: 'Acme Drive' }] }
    : href.includes(`/api/artifacts/${ok.slug}`) ? { ...ok, content: CONTENT }
      : href.includes(`/api/artifacts/${failed.slug}`) ? { ...failed, content: CONTENT }
        : {}
  return Promise.resolve(Response.json(body))
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const noop = () => {}
const entries: GridEntry[] = [ok, failed].map((art) => ({ kind: 'local', key: art.slug, art }))

function Harness() {
  return (
    <div className="bg-bg text-text" style={{ padding: 16, minHeight: '100vh', boxSizing: 'border-box' }}>
      <div data-testid="gallery" style={{ width: 600 }}>
        <LibraryMasonry
          entries={entries} cols={2} widthRef={noop} scrollerRef={{ current: null }} fillPage={false}
          onOpen={noop} onDelete={noop} deletingSlug={null} onTogglePin={noop} pinningSlug={null}
        />
      </div>
      <table data-testid="table" className="w-full border-collapse mt-4" style={{ maxWidth: 1100 }}>
        <tbody>
          {[ok, failed].map((a) => (
            <ArtifactRow key={a.slug} a={a} onOpen={noop} onDelete={noop} deletingSlug={null} onTogglePin={noop} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
