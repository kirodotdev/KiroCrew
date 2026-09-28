/**
 * The bug, rendered.
 *
 * The per-row elapsed/tool counter glued hardcoded English around the count:
 * `` ` · ${a.toolCount} tool${a.toolCount > 1 ? 's' : ''}` ``. The noun stayed
 * English in all 11 non-English locales, and the manual `> 1` ternary hardcoded
 * English plural rules (Russian needs 4 forms, Chinese 1). No catalog value
 * could fix it because the string never went through `i18nT()` — the same
 * defect class the plural codemod removed elsewhere, and the unfixed sibling
 * of the stalled-row sentence #4020 already converted in this component.
 *
 * These tests mount the real component under a non-English language and read
 * what the user would read. Same shape as DesignCritiqueComposer.i18n.test.tsx.
 */

import { describe, it, expect, vi, afterAll } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, sseSubagentSpawn, sseSubagentTool } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    spawnDelete: vi.fn().mockResolvedValue({}),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
}))

import SubagentProgressBar from '../pages/chat/SubagentProgressBar'
// `/all` for the 11 authored catalogs this file reads: `../i18n` registers
// English only.
import { i18next } from '../i18n/all'

const SLOT = 'test-slot'
const KEY = 'pages.chat.subagentProgressBar.tool'

function renderWithToolCount(toolCount: number) {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(setActiveSlot(SLOT))
  store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'a1', task: 'task a1', agent: 'agent-a1' }))
  store.dispatch(sseSubagentTool({ slot: SLOT, id: 'a1', tool: 'npx vitest run', tool_count: toolCount }))
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <SubagentProgressBar slot={SLOT} />
      </Provider>
    </QueryClientProvider>,
  )
}

/** The counter span: `<elapsed><s-suffix> · <tool counter>`. */
function counterText(container: HTMLElement): string {
  const span = [...container.querySelectorAll('span')].find(s => /·/.test(s.textContent || '') && s.className.includes('tabular-nums'))
  expect(span, 'no elapsed/tool counter span rendered').toBeTruthy()
  return span!.textContent || ''
}

afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('SubagentProgressBar — per-row tool counter localisation', () => {
  it('renders the English singular and plural forms, unchanged', async () => {
    await i18next.changeLanguage('en')
    const one = renderWithToolCount(1)
    expect(counterText(one.container)).toMatch(/^\d+s · 1 tool$/)
    one.unmount()
    const five = renderWithToolCount(5)
    expect(counterText(five.container)).toMatch(/^\d+s · 5 tools$/)
    five.unmount()
  })

  it('renders the localised counter in Chinese, not raw English', async () => {
    await i18next.changeLanguage('zh-CN')
    const expected = i18next.t(KEY, { count: 3 }) as string
    // Guard the guard: if the catalog lacked the key, i18next would fall back
    // to English and this test would pass while the bug persisted.
    expect(expected).not.toMatch(/\btools?\b/)

    const { container } = renderWithToolCount(3)
    const text = counterText(container)
    expect(text).toContain(` · ${expected}`)
    expect(text).not.toMatch(/\btools?\b/)
  })

  it('localises the counter in every authored language, singular and plural', async () => {
    for (const lang of ['bn', 'de', 'es', 'fr', 'hi', 'it', 'ja', 'ko', 'pt', 'ru', 'zh-CN']) {
      await i18next.changeLanguage(lang)
      for (const count of [1, 5]) {
        const expected = i18next.t(KEY, { count }) as string
        // Every language must resolve its own form — an English fallback means
        // the catalog is missing a plural category for this language.
        expect(expected, `${lang} count=${count} fell back to English`).not.toMatch(/\btools?\b/)
        const { container, unmount } = renderWithToolCount(count)
        expect(counterText(container), `${lang} count=${count}`).toContain(` · ${expected}`)
        unmount()
      }
    }
  })
})
