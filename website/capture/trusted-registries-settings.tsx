/**
 * Isolated capture entry for Settings → Security → "Registry trust".
 *
 * WHY ISOLATED: reaching /settings/security/registries through the full SPA
 * needs a live gateway plus a dashboard credential — without one the shell
 * renders the Kiro CLI prerequisite gate instead of Settings, which is worse
 * evidence than none. This mounts the REAL `SecurityPanel` against the REAL
 * stylesheet and theme tokens, so the rail row and the section render exactly
 * as production draws them; only the gateway reads are replaced, seeded into
 * the same query keys the panel reads (`['trusted-registries']` for the card,
 * plus the three the rail summarises from).
 *
 * Scene + theme come from the query string: ?scene=rows&theme=dark
 *   rows          — one trusted and one untrusted operator registry
 *   confirm       — the grant confirm dialog open for the untrusted row
 *   empty         — no hand-added registries at all
 *   not-served    — a row a build-pinned registry outranks ("Not used"), a
 *                   generic not-served row ("Not listed") and an orphan grant
 *                   ("Not configured" + Remove trust)
 *   change-failed — a grant rejected with a long backend detail, so the
 *                   change_failed notice wraps its ~200-char detail inside the card
 *   corrupt       — the corrupt-keystone notice
 *   unavailable   — the card after a failed read: notice + Retry
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { SecurityPanel } from '../src/pages/settings/SecurityPanel'
import { api } from '../src/api/client'
import { ApiError } from '../src/api/client'
import type { TrustedRegistryRow } from '../src/api/client'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'rows'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// example.test throughout: these strings are baked into a committed PNG, where
// no text scanner can see them.
const REGISTRIES: TrustedRegistryRow[] = [
  {
    name: 'team-apps',
    repo: 'https://git.example.test/team/apps-index.git',
    branch: 'main',
    host: 'git.example.test',
    trusted: true,
    served: true,
    granted: true,
  },
  {
    name: 'community',
    repo: 'https://git.example.test/community/index.git',
    branch: 'main',
    host: 'git.example.test',
    trusted: false,
    served: true,
    granted: false,
  },
]

// The not-served scene adds a row whose name a build-pinned registry already
// uses: the merge serves neither, so it shows the "Not used — …" note instead
// of a grant/revoke control.
const NOT_SERVED_REGISTRIES: TrustedRegistryRow[] = [
  REGISTRIES[0],
  {
    name: 'acme',
    repo: 'https://git.example.test/other/acme-index.git',
    branch: 'main',
    host: 'git.example.test',
    trusted: false,
    served: false,
    granted: false,
    not_served_reason: 'pinned_name',
  },
  // A row the merge does not serve for a reason the panel has no specific copy
  // for: the generic "Not listed" note, no control.
  {
    name: 'legacy',
    repo: 'https://git.example.test/old/legacy-index.git',
    branch: 'main',
    host: 'git.example.test',
    trusted: false,
    served: false,
    granted: false,
  },
  // An orphan grant: its config row was removed, the grant is still stored, so
  // it is listed with only "Remove trust".
  {
    name: 'https://git.example.test/gone/removed-index',
    repo: 'https://git.example.test/gone/removed-index',
    branch: '',
    host: 'git.example.test',
    trusted: false,
    served: false,
    granted: true,
    not_served_reason: 'not_configured',
  },
]

/** Opens the grant confirm for the untrusted row, so the shot shows the dialog
 *  a reviewer would otherwise have to click for. */
function OpenGrantConfirm() {
  useEffect(() => {
    const t = setInterval(() => {
      // The row testid is now `trusted-registry-<repo>#<name>`, so match by the
      // `<repo>#` prefix rather than the bare repo.
      const row = document.querySelector(
        '[data-testid^="trusted-registry-https://git.example.test/community/index.git#"]',
      )
      const btn = row?.querySelector('button') as HTMLElement | undefined
      if (btn) {
        clearInterval(t)
        btn.click()
      }
    }, 50)
    return () => clearInterval(t)
  }, [])
  return null
}

// The backend detail of a coded refusal; the card shows its own short copy for
// the `unknown_registry` code instead.
const LONG_DETAIL =
  'repo is not one of the configured registries — it was removed from the registries '
  + 'editor while this page was open, so there is nothing left to trust; reload the page '
  + 'to see the registries that are configured now'

/** Clicks Grant on the untrusted row, confirms, and lets the mocked grant reject
 *  with a long backend detail, so the shot shows the change_failed notice
 *  wrapping that detail beneath the rows. */
function DriveGrantFailure() {
  useEffect(() => {
    api.grantTrustedRegistry = () =>
      Promise.reject(
        new ApiError(400, 'Bad request', JSON.stringify({ error: LONG_DETAIL, code: 'unknown_registry' })),
      )
    // The failure refetches the snapshot; answer it with the same rows, as a
    // gateway would, rather than the harness's missing backend.
    api.listTrustedRegistries = () => Promise.resolve({ registries: REGISTRIES })
    const t = setInterval(() => {
      // Suffixed testid: match by the `<repo>#` prefix.
      const row = document.querySelector(
        '[data-testid^="trusted-registry-https://git.example.test/community/index.git#"]',
      )
      const grantBtn = row?.querySelector('button') as HTMLElement | undefined
      if (!grantBtn) return
      grantBtn.click()
      // The confirm dialog opens next tick; click its primary (last) button.
      setTimeout(() => {
        const dialog = document.querySelector('[role="dialog"]')
        const btns = dialog ? Array.from(dialog.querySelectorAll('button')) : []
        const ok = btns[btns.length - 1] as HTMLElement | undefined
        if (ok) {
          ok.click()
          clearInterval(t)
        }
      }, 60)
    }, 50)
    return () => clearInterval(t)
  }, [])
  return null
}

// `staleTime: Infinity` + `refetchOnMount: false` matter: every query below
// refetches on mount otherwise, the harness has no gateway to answer it, and
// each failed refetch replaces the seeded snapshot with an error state —
// capturing error cards instead of the scene asked for.
const qc = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false } },
})
// The unavailable scene seeds nothing for the card: its read goes to a gateway
// the harness does not have, fails, and the card renders its read-failure
// notice with Retry.
if (scene !== 'unavailable') qc.setQueryData(['trusted-registries'], {
  registries:
    scene === 'empty'
      ? []
      : scene === 'not-served'
        ? NOT_SERVED_REGISTRIES
        : REGISTRIES,
  // The corrupt scene flags the store damaged: the runtime fails closed (every
  // row untrusted), so the notice + Reset control render above the rows.
  ...(scene === 'corrupt'
    ? {
        corrupt: true,
        corrupt_detail: 'registry_trust.json is not valid JSON: Expecting value: line 1 column 1',
        corrupt_path: '/home/you/.kiro/crew/registry_trust.json',
        registries: REGISTRIES.map(r => ({ ...r, trusted: false })),
      }
    : {}),
})
// The three reads the section rail summarises from. Seeded so the rail renders
// its real counts rather than the blank lines an unread state gets.
qc.setQueryData(['denied-commands'], {
  builtins: [{ id: 'rm-rf-root', pattern: 'rm -rf /', enabled: true }],
  user_added: [],
  disable_all: false,
  effective_count: 1,
  governance_locked: false,
})
qc.setQueryData(['kirocrewConfig'], { agent: { yolo_duration: '6h', apps_allow_third_party: false } })
qc.setQueryData(['tailnet-status'], {
  enabled: false,
  governance_pinned: false,
  host: '',
  origin: '',
  resolved_at: 0,
  state: 'off',
})

await initI18n()

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      {/* `?sub=` is the query-backed selection SecurityPanel uses when the
          Settings host passes no basePath. */}
      <MemoryRouter initialEntries={['/settings?tab=security&sub=registries']}>
        {/* Width mirrors the Settings content column, so the rail and the
            detail pane sit side by side as they do in production. */}
        <div className="bg-bg text-text min-h-screen p-8">
          <div className="max-w-[1080px]" data-capture-root>
            <SecurityPanel />
          </div>
        </div>
        {scene === 'confirm' && <OpenGrantConfirm />}
        {scene === 'change-failed' && <DriveGrantFailure />}
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
