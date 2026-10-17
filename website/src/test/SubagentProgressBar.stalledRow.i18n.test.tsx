/**
 * The stalled subagent row composes ONE translated sentence (#3934).
 *
 * It used to be assembled in JSX from i18n fragments glued together with a
 * hardcoded English `" at "`:
 *
 *   {i18nT('…possibly_stalled')}
 *   {a.lastTool ? <span className="font-mono">{` at ${tool}`}</span> : ''}
 *   {typeof idleShown === 'number' ? ` — ${i18nT('…no_activity_for', …)}` : …}
 *
 * Two consequences: the `" at "` stayed English in all 11 non-English locales
 * (a Japanese user read `停止している可能性があります at Running: sleep 600 — …`),
 * and the fragment order was pinned in JSX so no locale could reorder the
 * sentence its grammar requires.
 *
 * These pin the acceptance criteria: no English literal leaks into a non-English
 * locale, each locale orders the sentence itself, the tool name keeps its
 * monospace styling, and the idle figure cannot be truncated away by a long
 * tool name.
 */
import { describe, it, expect, vi, beforeEach, afterAll } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  setActiveSlot, sseSubagentSpawn, sseSubagentTool, sseSubagentStalled,
} from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: { spawnDelete: vi.fn().mockResolvedValue({}), spawnList: vi.fn().mockResolvedValue({ agents: [] }) },
}))

import SubagentProgressBar from '../pages/chat/SubagentProgressBar'
// `/all` for the 11 authored catalogs this file switches between: `../i18n`
// (which the vitest setup file and this component's transitive imports use)
// registers English only, so a bare `import i18next from 'i18next'` here would
// silently fall back to English on every `changeLanguage(nonEnglish)` call.
import { i18next } from '../i18n/all'

const SLOT = 'test-slot'
const TOOL = 'Running: sleep 600'

function stalledRow({ tool = TOOL, idleSecs }: { tool?: string; idleSecs?: number } = {}) {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(setActiveSlot(SLOT))
  store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'a1', task: 'task a1', agent: 'agent-a1' }))
  if (tool) store.dispatch(sseSubagentTool({ slot: SLOT, id: 'a1', tool, tool_count: 3 }))
  store.dispatch(sseSubagentStalled({ slot: SLOT, id: 'a1', stalled: true, idle_secs: idleSecs }))
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const { container } = render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}><SubagentProgressBar slot={SLOT} /></Provider>
    </QueryClientProvider>,
  )
  return container
}

/** The warning line's full text, tool name included. */
function stallLine(container: HTMLElement): string {
  // Scoped to the agent ROW: `.text-warn` also matches the header's stalled
  // count badge, which would otherwise answer for the sentence under test.
  const row = container.querySelector('[data-testid="subagent-row"]')
  const warn = row?.querySelector('.text-warn')
  return warn?.textContent ?? ''
}

beforeEach(() => vi.clearAllMocks())
afterAll(async () => { await i18next.changeLanguage('en') })

describe('stalled subagent row — sentence composition', () => {
  it('reads as one English sentence, unchanged', async () => {
    await i18next.changeLanguage('en')
    expect(stallLine(stalledRow({ idleSecs: 117 })))
      .toContain(`possibly stalled at ${TOOL} — no activity for 117s`)
  })

  // Every shipped non-English locale. Derived from a list rather than one
  // spot-check, so a locale whose catalog entry loses the placeholder is caught
  // by name instead of hiding behind a passing `ja` case.
  const LOCALES = ['zh-CN', 'hi', 'es', 'fr', 'bn', 'pt', 'ru', 'de', 'ja', 'ko', 'it']

  it.each(LOCALES)('leaks no English glue into %s', async tag => {
    await i18next.changeLanguage(tag)
    const line = stallLine(stalledRow({ idleSecs: 117 }))
    // The tool name itself is a command, not prose, so it is excluded before
    // asking whether any English remains.
    const prose = line.replace(TOOL, '')
    expect(prose).not.toMatch(/\bat\b/)
    // The sentence still has to be a sentence: the tool and the figure both
    // reached it, in whatever order this locale put them.
    expect(line).toContain(TOOL)
    expect(line).toContain('117')
  })

  it.each(LOCALES)('renders translated prose, not the English fallback, in %s', async tag => {
    await i18next.changeLanguage(tag)
    const localised = stallLine(stalledRow({ idleSecs: 117 }))
    await i18next.changeLanguage('en')
    const english = stallLine(stalledRow({ idleSecs: 117 }))
    expect(localised).not.toBe(english)
  })

  it('lets a locale order the tool BEFORE the stall wording', async () => {
    // ja/ko/hi/bn/zh-CN place the tool first, which the old JSX-pinned fragment
    // order made impossible to express. Proves the reorder is actually reachable
    // and not merely permitted in principle.
    await i18next.changeLanguage('ja')
    const line = stallLine(stalledRow({ idleSecs: 117 }))
    expect(line.indexOf(TOOL)).toBeLessThan(line.indexOf('117'))
  })

  it('keeps the tool name monospaced and the surrounding prose not', async () => {
    await i18next.changeLanguage('ja')
    const container = stalledRow({ idleSecs: 117 })
    const mono = [...container.querySelectorAll('span')].find(s => s.textContent === TOOL)
    expect(mono).toBeTruthy()
    expect(mono!.className).toContain('font-mono')
    expect(mono!.parentElement!.className).not.toContain('font-mono')
  })

  it('truncates only the tool name, so the idle figure cannot be clipped away', () => {
    // The whole sentence used to be `truncate`, so a long tool name could clip
    // off the very number that justifies the warning.
    const container = stalledRow({ tool: 'x'.repeat(400), idleSecs: 117 })
    const mono = [...container.querySelectorAll('span')].find(s => s.textContent === 'x'.repeat(400))
    expect(mono!.className).toContain('truncate')
    // The figure sits in its own non-shrinking span, so it survives the clip.
    const figure = [...container.querySelectorAll('span')].find(s => (s.textContent || '').includes('117s'))
    expect(figure).toBeTruthy()
    expect(figure!.className).not.toContain('truncate')
  })

  it('still renders a whole sentence with no tool name', async () => {
    await i18next.changeLanguage('en')
    const line = stallLine(stalledRow({ tool: '', idleSecs: 117 }))
    expect(line).toContain('possibly stalled — no activity for 117s')
  })

  it('still renders a whole sentence with no idle span (older gateway)', async () => {
    await i18next.changeLanguage('en')
    const line = stallLine(stalledRow({}))
    expect(line).toContain(`possibly stalled at ${TOOL} — no activity`)
    // The fallback must not render a hole where the number would go.
    expect(line).not.toContain('undefined')
    expect(line).not.toContain('{{secs}}')
  })

  it('renders a whole sentence with neither tool nor span', async () => {
    await i18next.changeLanguage('en')
    const line = stallLine(stalledRow({ tool: '' }))
    expect(line).toContain('possibly stalled — no activity')
    expect(line).not.toContain('undefined')
  })
})
