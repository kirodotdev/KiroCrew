/**
 * Isolated capture entry for the built-in store row's provenance line.
 *
 * WHY ISOLATED: the interesting difference lives in ONE line of `AppListRow`,
 * and reaching it through the full SPA needs a live gateway whose registry
 * response actually carries catalog builtin rows. A half-stubbed app shell
 * renders its error boundary, which is worse evidence than none.
 *
 * WHAT IS FAITHFUL: the component is the real `AppListRow`, and both row shapes
 * carry values taken VERBATIM from the two real sources —
 *   - "before" = what local synthesis produces from this wheel's
 *     `src/kiro_crew/apps/builtins/<name>/app.json`
 *   - "after"  = what the live catalog publishes for the same app
 *     (https://apps.crew.kiro.dev/official-registry.json)
 * Nothing here is invented: artwork, tags and version are byte-identical across
 * the two sources (measured), so `author` is the whole visible delta and these
 * frames show exactly that.
 *
 * Scene + theme come from the query string: ?scene=before&theme=dark
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
// ThemeProvider reads installed theme packs through react-query, so it needs a
// client in scope even though nothing here fetches.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// Initialise i18next exactly as main.tsx does. Importing the module only DEFINES
// initI18n — without calling it, every label in the frame is blank, which
// silently produces screenshots that misrepresent the real UI.
import { initI18n } from '../src/i18n/all'
// Without the app's own stylesheet every Tailwind class in the row is inert:
// the frame renders unstyled, the icon tile loses its size constraint and fills
// the shot, and the theme tokens never paint. A screenshot in that state
// misrepresents the UI more than it documents it.
import '../src/index.css'
// AppIcon repaints a first-party /app-assets/ SVG from theme tokens, so it calls
// useTheme and throws without the real provider. Wrapping in the REAL provider
// (not a stub) keeps the icon in the frame identical to production.
import { ThemeProvider } from '../src/hooks/useTheme'
import AppListRow from '../src/components/appstore/AppListRow'
import type { RegistryApp } from '../src/components/appstore/types'

initI18n()

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'before'
// No `theme` param on purpose: the REAL ThemeProvider resolves the theme from
// the user's own preference and applies it to <html> itself, so a param here
// would be overwritten and the frames would carry a claim the harness does not
// control. Whatever theme the provider picks is the theme the store shows.
document.body.style.padding = '16px'
document.body.style.width = '1000px'

/**
 * Fields identical on both paths (measured against the live catalog).
 *
 * `iconUrl` is deliberately NOT here: it is per-app, and sharing one app's icon
 * across every row made three of the four frames show artwork belonging to a
 * different app.
 */
const shared = {
  version: '1.0.0',
  tags: ['productivity'],
  installed: true,
  enabled: true,
  origin: 'builtin',
  lifecycle: 'locked',
  provenance: 'builtin' as const,
  verified: true,
}

/**
 * Four rows spanning both outcomes. `author` is the only field that differs
 * between the two scenes, because artwork, tags and version are byte-identical
 * across the two sources (measured against the live catalog).
 *
 * `catalogAuthor` is what the published document states AFTER
 * KiroCrewApps PR #21 restored the four person-authored built-ins: the org
 * spelling is corrected for org-authored apps, and an individual's attribution
 * is preserved rather than flattened.
 */
const ROWS: {
  name: string
  displayName: string
  iconUrl: string
  localAuthor: string
  catalogAuthor: string
}[] = [
  // `iconUrl` is each app's own value, copied from its app.json — the same
  // string the store serves, so the tile in the frame is the tile in the UI.
  { name: 'projects', displayName: 'Projects', iconUrl: '/app-assets/projects/icon.svg', localAuthor: 'kirocrew', catalogAuthor: 'Kiro Crew' },
  { name: 'meetings', displayName: 'Meetings', iconUrl: '/app-assets/meetings/icon.svg', localAuthor: 'adunuthu', catalogAuthor: 'adunuthu' },
  { name: 'papyrus', displayName: 'Papyrus', iconUrl: '/app-assets/papyrus/icon.svg', localAuthor: 'tricatte', catalogAuthor: 'tricatte' },
  { name: 'pptx-maker', displayName: 'PPTX Maker', iconUrl: '/app-assets/pptx-maker/icon.svg', localAuthor: 'sktok', catalogAuthor: 'sktok' },
]

function rowFor(r: (typeof ROWS)[number]): RegistryApp {
  return {
    ...shared,
    name: r.name,
    displayName: r.displayName,
    iconUrl: r.iconUrl,
    description: 'Description comes from the i18n catalog for a first-party built-in.',
    author: scene === 'after' ? r.catalogAuthor : r.localAuthor,
  } as RegistryApp
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <ThemeProvider>
      <MemoryRouter>
        <div data-testid="capture-root" style={{ display: 'grid', gap: 8 }}>
          {ROWS.map(r => (
            <AppListRow
              key={r.name}
              app={rowFor(r)}
              onOpen={() => {}}
              onGet={() => {}}
              onUpdate={() => {}}
              onEnable={() => {}}
            />
          ))}
        </div>
      </MemoryRouter>
    </ThemeProvider>
  </QueryClientProvider>,
)
