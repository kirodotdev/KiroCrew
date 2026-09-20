/**
 * Evidence for the Import-cookies control (browser cookie import).
 *
 * Mounts the REAL CookieDialog and CookieChip from WebPreviewCookies.tsx with
 * the REAL i18n keys, so the frame photographs the shipped markup and strings.
 * The `cookies` hook result is stubbed (no gateway) — the components are pure
 * over it.
 *
 *   ?scene=dialog — the import dialog (explanation, file picker, paste, actions).
 *   ?scene=chip   — the imported-cookies status chip in a mock toolbar.
 */
import { createRoot } from 'react-dom/client'

import { CookieDialog, CookieChip } from '../src/components/WebPreviewCookies'
import { initI18n } from '../src/i18n'
import { applyFallbackTheme } from '../src/apps/mochi/src/shared/themes'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') ?? 'dialog'

document.documentElement.setAttribute('data-theme', 'kiro-dark')
applyFallbackTheme()
initI18n('en')

const SUMMARY = {
  cookie_count: 34,
  domains: ['example.com', 'api.example.com', 'auth.example.com', 'cdn.example.com'],
  earliest_expiry: Math.floor(Date.now() / 1000) + 19 * 3600,
  imported_at: Math.floor(Date.now() / 1000),
}

// A stub of the useBrowserCookies result. The components only read these fields.
const stub = (present: boolean) => ({
  data: { present, summary: present ? SUMMARY : null, config_path: '/home/u/.kiro/crew/browser-storage-state.json' },
  pending: false,
  error: null,
  forbidden: false,
  importCookies: async () => ({ ok: true as const, summary: SUMMARY, hot_load: { loaded: [], failed: {} } }),
  importing: false,
  importError: null,
  lastImport: null,
  clear: async () => ({ ok: true as const, present: false as const }),
  clearing: false,
  clearError: null,
}) as unknown as Parameters<typeof CookieDialog>[0]['cookies']

function Scene() {
  if (scene === 'chip') {
    return (
      <div
        data-capture-root
        style={{ width: 420, background: 'var(--bg-elevated)', padding: 8 }}
        className="flex items-center gap-1 border-b border-border"
      >
        <CookieChip cookies={stub(true)} />
      </div>
    )
  }
  return (
    <div data-capture-root style={{ width: 520, height: 440, background: 'var(--bg)', color: 'var(--text)' }}>
      <CookieDialog
        cookies={stub(false)}
        viewRunning={false}
        open
        onClose={() => { /* capture only */ }}
        onImported={() => { /* capture only */ }}
      />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
