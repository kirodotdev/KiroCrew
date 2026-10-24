/**
 * Evidence for the Import-cookies control (browser cookie import).
 *
 * Mounts the REAL CookieDialog, CookieChip, CookieMenuItem and
 * CookieClearMenuItem from WebPreviewCookies.tsx with the REAL i18n keys, so the
 * frame photographs the shipped markup and strings. The `cookies` hook result
 * is stubbed (no gateway); the components are pure over it.
 *
 *   ?scene=dialog        the import dialog (opening sentence, How to export, file, paste, actions)
 *   ?scene=chip          the imported-cookies status chip in a mock toolbar (&compact=1 for the Live header)
 *   ?scene=menu          the More actions menu with Import cookies and Clear cookies (&status=fail
 *                        for a failed status read, &clear=fail or &clear=live for a failed clear)
 *   &import=fail|live    what the dialog's Import answers: a refused request, or saved with one
 *                        live session refusing it
 *   &file=fail           every FileReader read fails, for the "couldn't read that file" state
 */
import { createRoot } from 'react-dom/client'
import { MoreHorizontal, Monitor } from 'lucide-react'

import { CookieDialog, CookieChip, CookieMenuItem, CookieClearMenuItem, CookieNewSessionHint } from '../src/components/WebPreviewCookies'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../src/components/ui/dropdown-menu'
import { initI18n } from '../src/i18n'
import { applyFallbackTheme } from '../src/apps/mochi/src/shared/themes'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') ?? 'dialog'

document.documentElement.setAttribute('data-theme', 'kiro-dark')
applyFallbackTheme()
initI18n('en')

if (params.get('file') === 'fail') {
  class FailingReader {
    result: string | null = null
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    readAsText() { setTimeout(() => this.onerror?.(), 0) }
  }
  ;(window as unknown as { FileReader: unknown }).FileReader = FailingReader
}

const SUMMARY = {
  cookie_count: 34,
  domains: ['example.com', 'api.example.com', 'auth.example.com', 'cdn.example.com'],
  earliest_expiry: Math.floor(Date.now() / 1000) + 19 * 3600,
  imported_at: Math.floor(Date.now() / 1000),
}

const importAnswer = async () => {
  if (params.get('import') === 'fail') {
    throw new Error("The computer running Kiro Crew cannot hand cookies to the agent's browser, so nothing was imported")
  }
  const failed: Record<string, string> = params.get('import') === 'live' ? { 'kc-0a1b2c3d': 'timed out' } : {}
  return { ok: true as const, summary: SUMMARY, hot_load: { loaded: ['kc-9f8e7d6c'], failed } }
}

const clearAnswer = async () => {
  if (params.get('clear') === 'fail') throw new Error("Couldn't clear the cookies")
  const failed: Record<string, string> = params.get('clear') === 'live' ? { 'kc-0a1b2c3d': 'timed out' } : {}
  return { ok: true as const, present: false as const, live: { cleared: [], failed } }
}

// A stub of the useBrowserCookies result. The components only read these fields.
const stub = (present: boolean) => ({
  data: { present, summary: present ? SUMMARY : null },
  pending: false,
  error: params.get('status') === 'fail' ? new Error("Couldn't read the imported cookies") : null,
  forbidden: false,
  importCookies: importAnswer,
  importing: false,
  importError: null,
  lastImport: null,
  clear: clearAnswer,
  clearing: false,
  clearError: null,
}) as unknown as Parameters<typeof CookieDialog>[0]['cookies']

function Scene() {
  if (scene === 'chip') {
    const compact = params.get('compact') === '1'
    return (
      <div
        data-capture-root
        style={{ width: 420, background: 'var(--bg-elevated)', padding: 8 }}
        className="flex items-center gap-2 border-b border-border"
      >
        <CookieChip cookies={stub(true)} compact={compact} />
        <CookieNewSessionHint show={params.get('hint') === '1'} />
      </div>
    )
  }
  if (scene === 'menu') {
    const cookies = stub(params.get('present') !== '0')
    return (
      <div data-capture-root style={{ width: 460, height: 260, background: 'var(--bg)', padding: 8 }} className="flex justify-end">
        <DropdownMenu open>
          <DropdownMenuTrigger asChild>
            <button type="button" aria-label="More actions" className="h-7 w-7 inline-flex items-center justify-center rounded-md text-muted bg-transparent border-none">
              <MoreHorizontal size={14} />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-[210px]">
            <DropdownMenuItem>
              <Monitor size={13} className="shrink-0 text-muted" />
              <span>Browser view</span>
            </DropdownMenuItem>
            <CookieMenuItem cookies={cookies} onOpen={() => { /* capture only */ }} />
            <CookieClearMenuItem cookies={cookies} />
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    )
  }
  return (
    <div data-capture-root style={{ width: 560, height: 640, background: 'var(--bg)', color: 'var(--text)' }}>
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
