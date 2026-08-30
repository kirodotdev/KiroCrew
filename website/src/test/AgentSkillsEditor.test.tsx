import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  agentPatch: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import AgentSkillsEditor from '../components/AgentSkillsEditor'

const CATALOG = [
  { key: 'babysit', name: 'babysit', description: 'Monitor a PR', source: 'kirocrew' },
  { key: 'kiro-user/prepare-pr', name: 'prepare-pr', description: 'Ship a PR', source: 'kiro-user' },
  { key: 'widgets', name: 'widgets', description: 'Render HTML', source: 'kirocrew' },
]

function renderEditor(props: Partial<React.ComponentProps<typeof AgentSkillsEditor>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onChange = props.onChange ?? vi.fn()
  const utils = render(
    <QueryClientProvider client={qc}>
      <AgentSkillsEditor
        agentName={props.agentName ?? 'specialist'}
        skills={props.skills ?? []}
        unmanaged={props.unmanaged}
        onChange={onChange}
      />
    </QueryClientProvider>,
  )
  return { ...utils, onChange }
}

beforeEach(() => {
  mockApi.skills.mockReset()
  mockApi.agentPatch.mockReset()
  mockApi.skills.mockResolvedValue(CATALOG)
  mockApi.agentPatch.mockResolvedValue({ ok: true })
})

/** Open the add-skill dropdown once the catalog query has resolved. */
async function openAddMenu() {
  const btn = await screen.findByRole('button', { name: /add skill/i })
  // Add is disabled until the catalog loads (nothing to offer before then).
  await waitFor(() => expect(btn).toBeEnabled())
  fireEvent.click(btn)
}

describe('AgentSkillsEditor', () => {
  it('shows the empty state when nothing is mapped', async () => {
    renderEditor()
    expect(
      await screen.findByText(/No skills mapped/i),
    ).toBeInTheDocument()
  })

  it('renders a chip per mapped skill using its catalog display name', async () => {
    renderEditor({ skills: ['babysit', 'kiro-user/prepare-pr'] })
    // 'prepare-pr' proves the key -> catalog name lookup, not a raw key echo.
    await waitFor(() => expect(screen.getByText('prepare-pr')).toBeInTheDocument())
    expect(screen.getByText('babysit')).toBeInTheDocument()
    expect(screen.queryByText(/No skills mapped/i)).not.toBeInTheDocument()
  })

  it('adds a skill by PATCHing the full desired key list', async () => {
    const { onChange } = renderEditor({ skills: ['babysit'] })
    await openAddMenu()

    const option = await screen.findByRole('option', { name: /widgets/i })
    fireEvent.click(option)

    await waitFor(() =>
      expect(mockApi.agentPatch).toHaveBeenCalledWith('specialist', {
        skills: ['babysit', 'widgets'],
      }),
    )
    await waitFor(() => expect(onChange).toHaveBeenCalledWith('specialist', ['babysit', 'widgets']))
  })

  it('omits already-mapped skills from the add list', async () => {
    renderEditor({ skills: ['babysit'] })
    await openAddMenu()

    await waitFor(() => expect(screen.getByRole('option', { name: /widgets/i })).toBeInTheDocument())
    expect(screen.queryByRole('option', { name: /babysit/i })).not.toBeInTheDocument()
  })

  it('removing a chip PATCHes the remaining keys', async () => {
    renderEditor({ skills: ['babysit', 'widgets'] })
    fireEvent.click(await screen.findByRole('button', { name: /remove skill babysit/i }))

    await waitFor(() =>
      expect(mockApi.agentPatch).toHaveBeenCalledWith('specialist', { skills: ['widgets'] }),
    )
  })

  it('prefers the server-returned key list over the optimistic one', async () => {
    // The backend is authoritative: it de-dupes and drops entries it cannot
    // resolve, so the UI must adopt its answer rather than the request body.
    mockApi.agentPatch.mockResolvedValue({ ok: true, skills: ['widgets'] })
    const { onChange } = renderEditor({ skills: [] })
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith('specialist', ['widgets']))
  })

  it('surfaces a rejected save instead of showing it as applied', async () => {
    mockApi.agentPatch.mockRejectedValue(new Error('unknown skills'))
    const { onChange } = renderEditor({ skills: [] })
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))

    await waitFor(() => expect(screen.getByText(/unknown skills/i)).toBeInTheDocument())
    expect(onChange).not.toHaveBeenCalled()
  })

  it('reports the agent a save was issued for, so a stale response cannot land on another agent', async () => {
    // The agent name travels with the request and comes back on the callback,
    // so the parent can drop a response that resolved after the selection moved
    // on. Without it, agent A's skills render under agent B and the next edit
    // writes them into B's spec.
    mockApi.agentPatch.mockResolvedValue({ ok: true, skills: ['widgets'] })
    const { onChange } = renderEditor({ agentName: 'agent-a', skills: [] })
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith('agent-a', ['widgets']))
    expect(mockApi.agentPatch).toHaveBeenCalledWith('agent-a', { skills: ['widgets'] })
  })

  it('lists unmanaged skill:// URIs read-only with no remove control', async () => {
    renderEditor({ skills: [], unmanaged: ['skill://~/.kiro/skills/*/SKILL.md'] })
    await waitFor(() =>
      expect(screen.getByText('skill://~/.kiro/skills/*/SKILL.md')).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /^Remove skill/i })).not.toBeInTheDocument()
    // A wildcard mapping is still a mapping — the empty state must not claim
    // the agent has none.
    expect(screen.queryByText(/No skills mapped/i)).not.toBeInTheDocument()
  })

  it('re-enumerates the catalog after a rejected save, so a retry cannot re-send a stale key', async () => {
    // A bundle upgrade under a live editor re-spells every package key, so the cached
    // catalog would have the retry re-send the identical rejected key.
    mockApi.agentPatch.mockRejectedValue(new Error('unknown skills'))
    renderEditor({ skills: [] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalledTimes(1))

    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))

    await waitFor(() => expect(screen.getByText(/unknown skills/i)).toBeInTheDocument())
    await waitFor(() => expect(mockApi.skills.mock.calls.length).toBeGreaterThan(1))
  })

  it('disambiguates two colliding copies that share a name and a description', async () => {
    // Real collisions are two bundles vendoring the SAME skill, so name and description
    // both match and the qualifier is the only field that differs.
    const digestA = '05c564ec5e9e4b7a8c1d2e3f4a5b6c7d'
    const digestB = 'b81f3a9042d75c6e1a4b8d2f6c0e9a37'
    mockApi.skills.mockResolvedValue([
      { key: `package/${digestA}:shared-skill`, name: 'shared-skill', description: 'Reconcile a shipment', source: 'package' },
      { key: `package/${digestB}:shared-skill`, name: 'shared-skill', description: 'Reconcile a shipment', source: 'package' },
      { key: 'widgets', name: 'widgets', description: 'Render HTML', source: 'kirocrew' },
    ])
    renderEditor({ skills: [] })
    await openAddMenu()

    const options = await screen.findAllByRole('option', { name: /shared-skill/ })
    expect(options).toHaveLength(2)
    // Each colliding row carries its own qualifier, so the two are distinguishable.
    expect(options[0].textContent).toContain(digestA.slice(0, 8))
    expect(options[1].textContent).toContain(digestB.slice(0, 8))
    // An uncollided name stays a plain label with no digest noise.
    const plain = await screen.findByRole('option', { name: /widgets/ })
    expect(plain.textContent).not.toMatch(/[0-9a-f]{8}/)
  })

  it('renders a mapped-but-unresolved package key by its readable half, not a raw digest', async () => {
    // A stale key has no catalog row, so the chip fell back to the whole key and showed
    // a 32-hex digest to the user.
    const stale = 'package/05c564ec5e9e4b7a8c1d2e3f4a5b6c7d:shared-skill'
    mockApi.skills.mockResolvedValue([])
    renderEditor({ skills: [stale] })

    await waitFor(() => expect(screen.getByText('shared-skill')).toBeInTheDocument())
    expect(screen.queryByText(stale)).not.toBeInTheDocument()
  })

  it('disables Add when every catalog skill is already mapped', async () => {
    renderEditor({ skills: CATALOG.map(s => s.key) })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /add skill/i })).toBeDisabled(),
    )
  })
})
