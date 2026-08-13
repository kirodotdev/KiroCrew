/**
 * Evidence for the destructive-confirm operand quoting (#4657).
 *
 * THE PROBLEM: two destructive confirm prompts interpolate a user-supplied
 * name BARE — `Reset {{name}}?` and `Delete {{file}}?` — so a name that
 * happens to be an ordinary word blends into the sentence: a pet named
 * "Everything" produced "Reset Everything?" and the reader cannot tell where
 * the name ends and the question resumes.
 *
 * Two scenes, selected with ?scene=:
 *
 *   ?scene=mochi-reset — mounts the REAL ChatPanel. The pet name comes
 *     through the REAL config path (`getMochiConfig` → `resolvePetName`): the
 *     harness only stubs the `/api/apps/mochi/settings` HTTP response so the
 *     pet is named "Everything", the ordinary word that makes the defect
 *     legible. The Playwright driver then opens the real context menu and
 *     clicks the real Reset item, so the dialog photographed is the shipped
 *     one — markup, strings, and interpolation all production code.
 *
 *   ?scene=papyrus-delete — Papyrus asks via native `window.confirm`
 *     (PapyrusPage.tsx), which has no DOM to photograph. The STRING is real:
 *     the scene calls the REAL i18nT with the REAL key and interpolation
 *     (`apps.papyrus.workspace.delete_file_confirm`, file="Everything") and
 *     renders it inside clearly-labelled harness chrome shaped like a native
 *     confirm, so the frame shows the exact text the OS dialog displays.
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n'
import { i18nT } from '../src/i18n/t'
import { applyFallbackTheme } from '../src/apps/mochi/src/shared/themes'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') ?? 'mochi-reset'

/** The ordinary-word name that makes the bare-operand defect legible: the
 *  before-frame reads "Reset Everything?" — indistinguishable from a sentence
 *  about resetting everything. */
const PET_NAME = 'Everything'

document.documentElement.setAttribute('data-theme', 'kiro-dark')
applyFallbackTheme()
initI18n('en')

if (scene === 'mochi-reset') {
  // Stub ONLY the settings read the pet-name path goes through; every other
  // request keeps its real behaviour (and simply fails harmlessly on the
  // capture server — the renderer guards every bridge call).
  const realFetch = window.fetch.bind(window)
  window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    if (url.includes('/api/apps/mochi/settings')) {
      return Promise.resolve(new Response(
        JSON.stringify({ activeAppearance: '', catPreset: null, petName: PET_NAME }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ))
    }
    return realFetch(input, init)
  }) as typeof window.fetch
  // ChatPanel is imported lazily so the fetch stub is installed before any of
  // its mount-time effects run.
  const { ChatPanel } = await import('../src/apps/mochi/src/renderer/ChatPanel')
  createRoot(document.getElementById('root')!).render(
    <div data-capture-root style={{ width: 420, height: 480, background: 'var(--bg)', color: 'var(--text)' }}>
      <ChatPanel />
    </div>,
  )
} else {
  // papyrus-delete: real string, harness-drawn native-confirm chrome.
  const message = i18nT('apps.papyrus.workspace.delete_file_confirm', { file: PET_NAME })
  createRoot(document.getElementById('root')!).render(
    <div data-capture-root style={{ width: 420, padding: 24, background: 'var(--bg)', color: 'var(--text)', boxSizing: 'border-box' }}>
      {/* Harness chrome: window.confirm has no DOM; this frame shows the real
          message text the native dialog carries. */}
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 8 }}>
        native confirm (harness rendering) — message text is production i18n output
      </div>
      <div style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)', borderRadius: 10, padding: '16px 20px', boxShadow: '0 8px 24px var(--shadow)' }}>
        <div data-confirm-message style={{ fontSize: 13, color: 'var(--text)', marginBottom: 16 }}>{message}</div>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <div style={{ background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 6, padding: '5px 12px', fontSize: 12 }}>Cancel</div>
          <div style={{ background: 'var(--danger)', borderRadius: 6, padding: '5px 12px', color: '#fff', fontSize: 12, fontWeight: 600 }}>OK</div>
        </div>
      </div>
    </div>,
  )
}
