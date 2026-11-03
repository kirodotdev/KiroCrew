/**
 * The MCP and Skill browser modals cap their results column (and the skill
 * preview caps its bundle file list at max-h-40). With the scrollbar hidden
 * until scroll, a mouse user saw a list that just ended. Each scroller now
 * carries the MORE_BELOW_MASK fade while `useScrollEdgesY` reports rows below
 * the fold, and drops it when the list fits.
 *
 * jsdom does no layout, so each scroller's geometry is stubbed by its id or
 * test id.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const { mockApi, MockApiError } = vi.hoisted(() => {
  class MockApiError extends Error {
    readonly status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  }
  return {
    mockApi: {
      mcpDiscover: vi.fn(),
      mcpDiscoverDetail: vi.fn(),
      mcpDiscoverInstall: vi.fn(),
      discoverSkills: vi.fn(),
      previewDiscoveredSkill: vi.fn(),
      installDiscoveredSkill: vi.fn(),
    },
    MockApiError,
  }
})
vi.mock('../api/client', () => ({ api: mockApi, ApiError: MockApiError }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))
vi.mock('../components/SkillDirectoryBrowser', () => ({ SkillMetaStrip: () => null }))

import McpBrowserModal from '../components/McpBrowserModal'
import SkillBrowserModal from '../components/SkillBrowserModal'
import { MORE_BELOW_MASK } from '../components/ModelEffortDropdown'

const MASK_CLASS = MORE_BELOW_MASK.split(' ')[0]

class InertObserver { observe() {} unobserve() {} disconnect() {} }

/** Hidden pixels below the fold, per scroller id or test id. */
let hiddenBelow: Record<string, number> = {}
const keyOf = (el: HTMLElement) => el.dataset.testid || el.id

const proto = HTMLElement.prototype
const saved = {
  scrollHeight: Object.getOwnPropertyDescriptor(proto, 'scrollHeight'),
  clientHeight: Object.getOwnPropertyDescriptor(proto, 'clientHeight'),
}

beforeEach(() => {
  hiddenBelow = {}
  vi.stubGlobal('ResizeObserver', InertObserver)
  Object.defineProperty(proto, 'clientHeight', { configurable: true, get() { return 200 } })
  Object.defineProperty(proto, 'scrollHeight', {
    configurable: true,
    get(this: HTMLElement) { return 200 + (hiddenBelow[keyOf(this)] ?? 0) },
  })
  Object.values(mockApi).forEach(m => m.mockReset())
})
afterEach(() => {
  vi.unstubAllGlobals()
  for (const [k, d] of Object.entries(saved)) {
    if (d) Object.defineProperty(proto, k, d)
    else delete (proto as unknown as Record<string, unknown>)[k]
  }
})

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const server = (id: string) => ({
  id, name: id, title: id, description: 'd', provider: 'official', display_provider: 'MCP Registry',
  version: '1', repo_url: '', installed: false, methods: ['npx'], deprecated: false,
})
const skill = (id: string) => ({
  id, name: id, description: 'd', provider: 'skillsh', display_provider: 'skills.sh',
  repo_url: '', author: 'a', installed: false, tags: [], installs: 1,
})

describe('MCP browser results column', () => {
  it.each([
    ['fades the bottom when servers sit below the fold', 300, true],
    ['shows no fade when the list fits', 0, false],
  ])('%s', async (_name, hidden, faded) => {
    hiddenBelow['mcp-results-list'] = hidden
    mockApi.mcpDiscover.mockResolvedValue({ results: [server('a'), server('b')], providers: ['official'] })
    wrap(<McpBrowserModal open onClose={() => {}} />)
    const list = await screen.findByRole('listbox')
    expect(list.id).toBe('mcp-results-list')
    await waitFor(() => expect(list.classList.contains(MASK_CLASS)).toBe(faded))
  })
})

describe('Skill browser', () => {
  it.each([
    ['fades the results column when skills sit below the fold', 300, true],
    ['shows no fade on a results column that fits', 0, false],
  ])('%s', async (_name, hidden, faded) => {
    hiddenBelow['skill-results-list'] = hidden
    mockApi.discoverSkills.mockResolvedValue({ results: [skill('a'), skill('b')], providers: ['skillsh'] })
    mockApi.previewDiscoveredSkill.mockResolvedValue({ name: 'a', description: 'd' })
    wrap(<SkillBrowserModal open onClose={() => {}} />)
    const list = await screen.findByRole('listbox')
    expect(list.id).toBe('skill-results-list')
    await waitFor(() => expect(list.classList.contains(MASK_CLASS)).toBe(faded))
  })

  it.each([
    ['fades the bundle file list when files sit below the fold', 300, true],
    ['shows no fade on a bundle file list that fits', 0, false],
  ])('%s', async (_name, hidden, faded) => {
    hiddenBelow['skill-bundle-file-list'] = hidden
    mockApi.discoverSkills.mockResolvedValue({ results: [skill('a')], providers: ['skillsh'] })
    mockApi.previewDiscoveredSkill.mockResolvedValue({
      name: 'a', description: 'd', content: '# A',
      files: Array.from({ length: 30 }, (_, i) => `f${i}.md`), file_count: 30,
    })
    wrap(<SkillBrowserModal open onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('option', { name: 'a' }))
    const files = await screen.findByTestId('skill-bundle-file-list')
    await waitFor(() => expect(files.classList.contains(MASK_CLASS)).toBe(faded))
  })
})
