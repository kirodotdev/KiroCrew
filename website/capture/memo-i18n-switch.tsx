/**
 * Evidence for the memo()+i18nT() language-switch fix (#5225).
 *
 * THE PROBLEM: `LanguageProvider` repaints a language change with
 * `cloneElement(children)`, which reaches the ROOT element only. A `memo()`
 * boundary whose props did not change bails out, and standalone `i18nT()`
 * subscribes to nothing — so the memoized subtree kept rendering the previous
 * catalog. The fix: each memoized component calls `useLanguageGeneration()`,
 * whose `useSyncExternalStore` subscription re-renders it on `languageChanged`.
 *
 * The scene mounts the REAL `PastedChip` (`export default memo(PastedChip)`,
 * the issue's archetype) with a stable `block` prop, next to `BareMemoChip`,
 * a replica of the PRE-FIX shape (memo + i18nT, no subscription). One frame is
 * captured before the switch (both English) and one after (the fixed chip in
 * Chinese, the pre-fix replica still stuck in English) — the after frame is
 * its own before/after comparison. The rows are harness chrome, labelled as
 * such. `window.__switch()` performs the switch; `window.__texts()` exposes
 * both texts so the driver ASSERTS the fix and the reproduction rather than
 * assuming them.
 */
import { memo } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { initI18n } from '../src/i18n/all'
import { LanguageProvider, useLanguage } from '../src/i18n/LanguageProvider'
import { i18nT } from '../src/i18n/t'
import PastedChip from '../src/components/PastedChip'
import type { PasteBlock } from '../src/utils/pasteTokens'
import '../src/index.css'

document.documentElement.setAttribute('data-theme', 'kiro-dark')
initI18n('en')

const BLOCK: PasteBlock = {
  id: 'cap-1',
  seq: 1,
  lines: 12,
  content: 'line one\nline two\nline three',
}

/** The PRE-FIX shape: memo + standalone i18nT, no language subscription. */
const BareMemoChip = memo(function BareMemoChip({ block }: { block: PasteBlock }) {
  return (
    <span data-cap="bare" style={{ color: 'var(--accent)' }}>
      {i18nT('components.pastedChip.paste_lines', { seq: block.seq, count: block.lines })}
    </span>
  )
})

declare global {
  interface Window {
    __switch: () => void
    __texts: () => { fixed: string; bare: string }
  }
}

function Scene() {
  const { setLanguage } = useLanguage()
  window.__switch = () => setLanguage('zh-CN')
  window.__texts = () => ({
    fixed: document.querySelector('[data-cap="fixed"]')?.textContent ?? '',
    bare: document.querySelector('[data-cap="bare"]')?.textContent ?? '',
  })
  const row: React.CSSProperties = {
    display: 'flex', alignItems: 'center', gap: 16, padding: '10px 0',
    borderBottom: '1px solid var(--border)',
  }
  const label: React.CSSProperties = { width: 340, color: 'var(--muted)', fontSize: 13 }
  return (
    <div style={{ padding: 24, background: 'var(--bg)', color: 'var(--text)', fontFamily: 'sans-serif', minHeight: 200 }}>
      <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 8 }}>
        harness chrome — labels are not part of the product UI
      </div>
      <div style={row}>
        <span style={label}>real PastedChip (memo + useLanguageGeneration)</span>
        <span data-cap="fixed"><PastedChip block={BLOCK} /></span>
      </div>
      <div style={row}>
        <span style={label}>pre-fix replica (memo, no subscription)</span>
        <BareMemoChip block={BLOCK} />
      </div>
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <LanguageProvider><Scene /></LanguageProvider>
  </QueryClientProvider>,
)
