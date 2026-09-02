/**
 * The numbers drawn ON the Agents page meter bars (context window, plan credits).
 *
 * These labels overlap BOTH the saturated fill and the empty track, and which
 * one sits under a given glyph moves with the value — so a single static colour
 * is unreadable at one end of the range. `text-text-strong` is near-black in
 * every light theme, which is how a 62% context bar shipped as dark-on-purple.
 *
 * The fix paints the row twice with complementary clips: track side in
 * `text-text-strong`, fill side in the fill's own paired foreground token
 * (`--accent-fg` / `--warn-fg` / `--danger-fg`, each theme declaring black or
 * white to suit its own fill). What is pinned here is the pairing — the fill-side
 * colour must follow the SAME threshold ladder as the fill colour, or a bar goes
 * back to low contrast at exactly the values that matter most (>=90% is the
 * compaction warning) — and the geometry, since the two clips must meet at the
 * fill's edge with no gap and no double-painted overlap.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({
  spawnList: vi.fn(),
  sessionsContext: vi.fn(),
  sessionsUsage: vi.fn(),
  agentsInstalled: vi.fn(),
  mcpProbeCache: vi.fn(),
  defaultAgent: vi.fn(),
  agentDetail: vi.fn(),
  agentMetadata: vi.fn(),
  kirocrewAgents: vi.fn(),
  skills: vi.fn(),
  agentPatch: vi.fn(),
  spawnClear: vi.fn(),
  spawnDelete: vi.fn(),
  setDefaultAgent: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../store', () => ({ useAppSelector: (fn: (s: unknown) => unknown) => fn({ dashboard: { status: { sessions: 1, subagents: 0 }, refreshTrigger: 0 } }) }))
vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    displayName: 'ACP',
    capabilities: { agentTemplates: true },
    labels: { sessionProcess: 'ACP subprocess', configFile: 'agent.json' },
    getContextWindow: () => 200_000,
    fetchAvailableModels: () => Promise.resolve([]),
  }),
}))

import AgentsPage from '../pages/AgentsPage'

const KIROCREW = {
  name: 'kirocrew',
  description: 'kirocrew agent',
  source: 'builtin',
  model: 'claude-opus-4.8',
  skills: [],
  mcp_servers: [],
  filename: 'kirocrew.json',
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AgentsPage embedded />
    </QueryClientProvider>,
  )
}

const session = (pct: number) => ({
  key: 'chat-1', name: 'Chat (chat-1)', model: 'claude-opus-4.8',
  context_pct: pct, context_window_tokens: 200_000, prompts: 8,
})

/** The two label layers of the bar that carries `text`, outermost first. */
function layers(text: string): HTMLElement[] {
  return screen.getAllByText(text).map(el => el.parentElement as HTMLElement)
}

beforeEach(() => {
  Object.values(mockApi).forEach(fn => fn.mockReset())
  mockApi.spawnList.mockResolvedValue({ agents: [] })
  mockApi.sessionsContext.mockResolvedValue({ sessions: [] })
  mockApi.sessionsUsage.mockResolvedValue({ usage: null })
  mockApi.agentsInstalled.mockResolvedValue([KIROCREW])
  mockApi.mcpProbeCache.mockResolvedValue([])
  mockApi.agentMetadata.mockResolvedValue({ content: '' })
  mockApi.kirocrewAgents.mockResolvedValue({ agents: [], default_agent: '' })
  mockApi.skills.mockResolvedValue([])
  mockApi.agentDetail.mockResolvedValue({ ...KIROCREW, unmanaged_skills: [] })
})

describe('meter bar labels adapt to the fill underneath them', () => {
  it('splits the row at the fill edge: track side strong, fill side on-accent', async () => {
    mockApi.sessionsContext.mockResolvedValue({ sessions: [session(62.4)] })
    renderPage()

    await waitFor(() => expect(screen.getAllByText('62.4%')).toHaveLength(2))
    const [track, fill] = layers('62.4%')

    expect(track.className).toContain('text-text-strong')
    expect(fill.className).toContain('text-accent-fg')
    // Complementary clips meeting at 62.4%: no gap, no overlap.
    expect(track.style.clipPath).toBe('inset(0 0 0 62.4%)')
    expect(fill.style.clipPath).toBe('inset(0 37.6% 0 0)')
  })

  it('uses the warn foreground once the fill turns warn', async () => {
    mockApi.sessionsContext.mockResolvedValue({ sessions: [session(75)] })
    renderPage()

    await waitFor(() => expect(screen.getAllByText('75.0%')).toHaveLength(2))
    const [, fill] = layers('75.0%')
    expect(fill.className).toContain('text-warn-fg')
    expect(fill.className).not.toContain('text-accent-fg')
  })

  it('uses the danger foreground once the fill turns danger', async () => {
    mockApi.sessionsContext.mockResolvedValue({ sessions: [session(95)] })
    renderPage()

    await waitFor(() => expect(screen.getAllByText('95.0%')).toHaveLength(2))
    const [, fill] = layers('95.0%')
    expect(fill.className).toContain('text-danger-fg')
  })

  // A bar can be empty (0% context, or a fresh billing period). The fill is
  // floored at 1% so it stays visible as a dot; the clips must use the SAME
  // floor, or the label would be painted in the on-fill colour over bare track.
  it('clips to the fill floor, not the raw value, when the bar is empty', async () => {
    mockApi.sessionsContext.mockResolvedValue({ sessions: [{ ...session(0), prompts: 3 }] })
    renderPage()

    await waitFor(() => expect(screen.getAllByText('0.0%')).toHaveLength(2))
    const [track, fill] = layers('0.0%')
    expect(track.style.clipPath).toBe('inset(0 0 0 1%)')
    expect(fill.style.clipPath).toBe('inset(0 99% 0 0)')
  })

  // Overage: the bar is pinned at 100% while the label reads past it. The clip
  // has to follow the BAR, so the whole label sits in the on-fill colour.
  it('paints the whole label on-fill for an over-plan credits bar', async () => {
    mockApi.sessionsUsage.mockResolvedValue({
      usage: { plan: 'KIRO PRO', credits_used: 80_237, credits_plan: 10_000, credits_overage: 70_237 },
    })
    renderPage()

    await waitFor(() => expect(screen.getAllByText('802%')).toHaveLength(2))
    const [track, fill] = layers('802%')
    expect(fill.className).toContain('text-danger-fg')
    expect(fill.style.clipPath).toBe('inset(0 0% 0 0)')
    expect(track.style.clipPath).toBe('inset(0 0 0 100%)')
  })

  // `context_pct` is a plain number off the wire and the page clamps it to 100
  // for the bar; the clip has to land on the same 100, and an `inset()` past
  // 100% would be invalid — a dropped clip paints BOTH layers full-width, one
  // over the other, which is the failure this component exists to avoid.
  it('clamps a pct the backend sent out of range instead of dropping the clip', async () => {
    mockApi.sessionsContext.mockResolvedValue({ sessions: [session(140)] })
    renderPage()

    await waitFor(() => expect(screen.getAllByText('100.0%')).toHaveLength(2))
    const [track, fill] = layers('100.0%')
    expect(fill.style.clipPath).toBe('inset(0 0% 0 0)')
    expect(track.style.clipPath).toBe('inset(0 0 0 100%)')
  })

  it('falls back to the fill floor when pct is not a number', async () => {
    mockApi.sessionsContext.mockResolvedValue({ sessions: [session(Number.NaN)] })
    renderPage()

    // fmtPercent renders a non-finite ratio as an em dash.
    await waitFor(() => expect(screen.getAllByText('—')).toHaveLength(2))
    const [track, fill] = layers('—')
    expect(track.style.clipPath).toBe('inset(0 0 0 1%)')
    expect(fill.style.clipPath).toBe('inset(0 99% 0 0)')
  })

  // The fill animates over 700ms (1000ms for credits). If the clip snapped to
  // the new value immediately, the label would spend that window painted in the
  // on-fill colour over track that has not filled yet — the original bug, just
  // transient.
  it('moves the cut on the same transition as the fill it follows', async () => {
    mockApi.sessionsContext.mockResolvedValue({ sessions: [session(62.4)] })
    renderPage()

    await waitFor(() => expect(screen.getAllByText('62.4%')).toHaveLength(2))
    for (const layer of layers('62.4%')) {
      expect(layer.className).toContain('transition-[clip-path,color]')
      expect(layer.className).toContain('duration-700')
    }
  })

  // The duplicate exists only to be looked at. Leaving it in the accessibility
  // tree would read every figure on the card twice.
  it('hides the duplicated layer from assistive tech', async () => {
    mockApi.sessionsContext.mockResolvedValue({ sessions: [session(62.4)] })
    renderPage()

    await waitFor(() => expect(screen.getAllByText('62.4%')).toHaveLength(2))
    const [track, fill] = layers('62.4%')
    expect(track).not.toHaveAttribute('aria-hidden')
    expect(fill).toHaveAttribute('aria-hidden', 'true')
  })
})
