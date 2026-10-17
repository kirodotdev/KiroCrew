/**
 * Capture harness for issue #13850 — Settings search governance: Decisions
 * (Jev) judge controls hidden when `capabilities.decisions` is not offered.
 *
 * Shows the in-page Settings search box with query "which judge" in two states:
 *   enabled  — `decisions_enabled: true`  → "Which judge answers" appears
 *   disabled — `decisions_enabled: false` → no results / entry is absent
 *
 * The `decisions_enabled` answer comes from `/api/dashboard/config`. This
 * harness intercepts that call so no gateway is required.
 *
 * Usage:
 *   node scripts/capture-13850-settings-search-governance.mjs [base] [out-dir]
 *   Needs: npx vite --host 127.0.0.1 --port 5199 (from website/)
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

// Resolve scene and theme from the query string so the capture script
// can render both states from the same HTML entry point.
const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
/** 'enabled' | 'disabled' — whether decisions_enabled is true */
const scene = params.get('scene') || 'disabled'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Stub /api/dashboard/config and /api/tips so the governance hook resolves
// without a live gateway.  Every other /api/ call returns an empty object.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = (typeof input === 'string' ? input : input instanceof URL ? input.href : input.url)
  const path = (() => { try { return new URL(url, location.origin).pathname } catch { return url } })()
  if (path === '/api/dashboard/config') {
    return Promise.resolve(new Response(
      JSON.stringify({ decisions_enabled: scene === 'enabled' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
  }
  if (path === '/api/tips') {
    return Promise.resolve(new Response(
      JSON.stringify({ enabled_config: true }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
  }
  if (path.startsWith('/api/')) {
    return Promise.resolve(new Response(
      JSON.stringify({}),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
  }
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

initI18n(params.get('lang') || 'en')

// Import SettingsSearch lazily after i18n is initialised so every label
// resolves from the English catalog on first render.
const { default: SettingsSearch } = await import('../src/pages/settings/SettingsSearch')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings/developer']}>
        <div
          data-capture-root
          style={{
            width: 320,
            padding: 16,
            background: 'var(--bg)',
            color: 'var(--text)',
          }}
        >
          <p
            style={{
              fontSize: 11,
              marginBottom: 8,
              color: 'var(--text-muted)',
              fontFamily: 'monospace',
            }}
          >
            {`decisions_enabled: ${scene === 'enabled'}`}
          </p>
          <SettingsSearch />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
