/**
 * The bug, rendered.
 *
 * Two hardcoded-English plural-glue sites in the wave chip header:
 *
 * 1. The retry button's `aria-label` glued English around the count
 *    (`` `Retry ${n} failed subagent${n > 1 ? 's' : ''}` ``) while the VISIBLE
 *    label two lines down already routed through
 *    `i18nT('…retry_failed_count', { count })`. In every non-English locale the
 *    accessible name therefore disagreed with the visible name — WCAG 2.5.3
 *    (Label in Name): a voice-control user cannot activate the control by the
 *    name they see. The fix routes the aria-label through the SAME key, so the
 *    two names cannot drift apart again.
 *
 * 2. The stop button concatenated a translated verb with a hardcoded English
 *    conditional suffix (`{i18nT('…stop')}{n > 1 ? ' all' : ''}`), rendering
 *    mixed-language text like `停止 all` in zh-CN. This is conditional copy,
 *    not count grammar (zh has a single plural category but still needs the
 *    "Stop"/"Stop all" distinction), so the fix selects between two keys
 *    rather than pluralizing one.
 *
 * These tests mount the real component under a non-English language and read
 * what the user would read. Same shape as SubagentProgressBar.i18n patterns
 * established by the pages.chat.subagentProgressBar conversions before it.
 */

import { describe, it, expect, vi, afterAll } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, sseSubagentSpawn, sseSubagentDone } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    spawnDelete: vi.fn().mockResolvedValue({}),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    spawnRetry: vi.fn().mockResolvedValue({}),
  },
}))

import SubagentProgressBar from '../pages/chat/SubagentProgressBar'
// `/all` for the 11 authored catalogs this file reads: `../i18n` registers
// English only.
import { i18next } from '../i18n/all'

const SLOT = 'test-slot'
const RETRY_KEY = 'pages.chat.subagentProgressBar.retry_failed_count'
const STOP_ALL_KEY = 'pages.chat.subagentProgressBar.stop_all'

/** Two failed agents (retry button, count 2) + two running (Stop all path). */
function renderChip() {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(setActiveSlot(SLOT))
  for (const id of ['f1', 'f2']) {
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id, task: `task ${id}`, agent: `agent-${id}` }))
    store.dispatch(sseSubagentDone({ slot: SLOT, id, elapsed: 1, error: 'boom', outcome: 'failed' }))
  }
  for (const id of ['r1', 'r2']) {
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id, task: `task ${id}`, agent: `agent-${id}` }))
  }
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <SubagentProgressBar slot={SLOT} />
      </Provider>
    </QueryClientProvider>,
  )
}

afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('SubagentProgressBar — retry/stop header localisation', () => {
  it('gives the retry button its visible text as the accessible name (WCAG 2.5.3), in English', async () => {
    await i18next.changeLanguage('en')
    const { container } = renderChip()
    const visible = i18next.t(RETRY_KEY, { count: 2 }) as string
    const btn = [...container.querySelectorAll('button')].find(b => (b.textContent || '').includes(visible))
    expect(btn, 'no retry button rendering the localised retry label').toBeTruthy()
    // No separate aria-label: the accessible name must BE the visible text, so
    // the two can never disagree (the original defect was a hardcoded English
    // aria-label beside a translated visible label).
    expect(btn!.getAttribute('aria-label')).toBeNull()
  })

  it('keeps the accessible name equal to the visible name in every authored language', async () => {
    for (const lang of ['bn', 'de', 'es', 'fr', 'hi', 'it', 'ja', 'ko', 'pt', 'ru', 'zh-CN']) {
      await i18next.changeLanguage(lang)
      const visible = i18next.t(RETRY_KEY, { count: 2 }) as string
      // Guard the guard: an English fallback would make the checks below pass
      // while the catalog is missing the key.
      expect(visible, `${lang} retry label fell back to English`).not.toMatch(/\bfailed\b/)
      const { container, unmount } = renderChip()
      const btn = [...container.querySelectorAll('button')].find(b => (b.textContent || '').includes(visible))
      expect(btn, `${lang}: no retry button rendering the localised label`).toBeTruthy()
      expect(btn!.getAttribute('aria-label'), `${lang}: a separate aria-label reappeared — it can drift from the visible text`).toBeNull()
      unmount()
    }
  })

  it('renders a fully localised "Stop all", never a translated verb glued to English " all"', async () => {
    await i18next.changeLanguage('zh-CN')
    const stopAll = i18next.t(STOP_ALL_KEY) as string
    expect(stopAll, 'zh-CN stop_all fell back to English').not.toMatch(/\ball\b/)
    const { container } = renderChip()
    // Scope to the stop button itself: a container-wide negative would fail on
    // any unrelated Latin "all" in an agent name or task label.
    const btn = [...container.querySelectorAll('button')].find(b => (b.textContent || '').includes(stopAll))
    expect(btn, 'no button rendering the localised Stop-all label').toBeTruthy()
    expect(btn!.textContent, 'the old hardcoded English suffix is back').not.toMatch(/\sall\b/)
  })
})
