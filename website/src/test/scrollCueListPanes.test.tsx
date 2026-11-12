/**
 * The list panes in the Prompts, Steering and Skills tabs are capped at the
 * pane's height. With the scrollbar hidden until scroll, a mouse user saw a
 * list that just ended. Each list now carries the MORE_BELOW_MASK fade while
 * `useScrollEdgesY` reports rows below the fold, and drops it when the list
 * fits.
 *
 * The fade sits on the inner scroller (the listbox), not on the bordered pane:
 * a mask clips everything outside the masked element, so on the pane it would
 * also erase the border.
 *
 * jsdom does no layout, so the listbox's geometry is stubbed.
 */
import type { ReactElement } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const mockApi = vi.hoisted(() => ({
  prompts: vi.fn(),
  promptDetail: vi.fn(),
  steeringFiles: vi.fn(),
  steeringFile: vi.fn(),
  skills: vi.fn(),
  skill: vi.fn(),
}))
const StubApiError = vi.hoisted(() => class ApiError extends Error {
  status = 0
  body = ''
})
vi.mock('../api/client', () => ({ api: mockApi, ApiError: StubApiError }))
vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))
vi.mock('../store', () => ({
  store: { getState: () => ({ chat: { activeSlot: '' } }), dispatch: vi.fn(), subscribe: () => () => {} },
  useAppDispatch: () => vi.fn(),
  useAppSelector: () => [],
}))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))
vi.mock('../components/SkillDirectoryBrowser', () => ({ default: () => <div>browser</div> }))

import PromptsTab from '../pages/overview/PromptsTab'
import SteeringTab from '../pages/overview/SteeringTab'
import SkillsTab from '../pages/overview/SkillsTab'
import { MORE_BELOW_MASK } from '../components/ModelEffortDropdown'

const MASK_CLASS = MORE_BELOW_MASK.split(' ')[0]

class InertObserver { observe() {} unobserve() {} disconnect() {} }

/** Pixels hidden below the fold of every listbox. */
let hiddenBelow = 0

const proto = HTMLElement.prototype
const saved = {
  scrollHeight: Object.getOwnPropertyDescriptor(proto, 'scrollHeight'),
  clientHeight: Object.getOwnPropertyDescriptor(proto, 'clientHeight'),
}

beforeEach(() => {
  hiddenBelow = 0
  vi.stubGlobal('ResizeObserver', InertObserver)
  Object.defineProperty(proto, 'clientHeight', { configurable: true, get() { return 200 } })
  Object.defineProperty(proto, 'scrollHeight', {
    configurable: true,
    get(this: HTMLElement) { return 200 + (this.getAttribute('role') === 'listbox' ? hiddenBelow : 0) },
  })
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.prompts.mockResolvedValue([
    { name: 'my-prompt', fullName: 'my-prompt', description: 'mine', path: '~/.kiro/prompts/my-prompt.md', package: '', source: 'global' },
  ])
  mockApi.promptDetail.mockResolvedValue({ content: 'body', redacted: false, hash: 'a'.repeat(64) })
  mockApi.steeringFiles.mockResolvedValue({
    files: [{ key: 'user/personal.md', name: 'personal.md', rel: 'personal.md', source: 'user', path: '~/.kiro/steering/personal.md', size: 12, description: 'Personal', inclusion: 'always', inclusion_declared: '', file_match_pattern: '' }],
    roots: [{ source: 'user', path: '~/.kiro/steering', exists: true }],
    project: '~/proj',
  })
  mockApi.steeringFile.mockResolvedValue({ key: 'user/personal.md', content: '# Personal', path: '~/.kiro/steering/personal.md', source: 'user' })
  mockApi.skills.mockResolvedValue([
    { key: 'foo', name: 'foo', description: 'a foo skill', source: 'kirocrew', loaded_by_agents: [] },
  ])
  mockApi.skill.mockResolvedValue({ name: 'foo', content: '---\nname: foo\n---\nbody', hash: 'h-foo' })
})
afterEach(() => {
  vi.unstubAllGlobals()
  for (const [k, d] of Object.entries(saved)) {
    if (d) Object.defineProperty(proto, k, d)
    else delete (proto as unknown as Record<string, unknown>)[k]
  }
})

function renderTab(tab: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(<QueryClientProvider client={qc}><MemoryRouter>{tab}</MemoryRouter></QueryClientProvider>)
}

const CASES = [
  { tab: 'PromptsTab', el: <PromptsTab />, list: 'Prompts' },
  { tab: 'SteeringTab', el: <SteeringTab />, list: 'Steering files' },
  { tab: 'SkillsTab', el: <SkillsTab />, list: 'Skills' },
]

describe.each(CASES)('$tab list pane scroll cue', ({ el, list }) => {
  it('fades the bottom while rows sit below the fold', async () => {
    hiddenBelow = 120
    renderTab(el)
    const box = await screen.findByRole('listbox', { name: list })
    await waitFor(() => expect(box.className).toContain(MASK_CLASS))
    // The bordered pane itself is never masked.
    const pane = box.parentElement as HTMLElement
    expect(pane.className).toContain('border')
    expect(pane.className).not.toContain(MASK_CLASS)
  })

  it('shows no fade when the list fits', async () => {
    renderTab(el)
    const box = await screen.findByRole('listbox', { name: list })
    await waitFor(() => expect(box.querySelector('[aria-current], [role="option"]')).not.toBeNull())
    expect(box.className).not.toContain(MASK_CLASS)
  })
})
