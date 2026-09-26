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
        beforeSave={props.beforeSave}
        pendingChain={props.pendingChain}
        onSavePending={props.onSavePending}
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
    const { onChange } = renderEditor({ skills: ['babysit', 'widgets'] })
    fireEvent.click(await screen.findByRole('button', { name: /remove skill babysit/i }))

    await waitFor(() =>
      expect(mockApi.agentPatch).toHaveBeenCalledWith('specialist', { skills: ['widgets'] }),
    )
    await waitFor(() => expect(onChange).toHaveBeenCalledWith('specialist', ['widgets']))
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
    // The catalog cannot express these, so there is no picker row to put one back.
    // They are shown so an agent that loads more than the chips suggest is explained,
    // but the backend on base owns their removal — the editor does not offer it.
    renderEditor({ skills: [], unmanaged: ['skill://~/.kiro/skills/*/SKILL.md'] })
    await waitFor(() =>
      expect(screen.getByText('skill://~/.kiro/skills/*/SKILL.md')).toBeInTheDocument(),
    )
    // A wildcard mapping is still a mapping — the empty state must not claim
    // the agent has none.
    expect(screen.queryByText(/No skills mapped/i)).not.toBeInTheDocument()
    // No remove control on an unmanaged URI: only the picker-managed chips carry one.
    expect(screen.queryByRole('button', { name: /remove skill/i })).not.toBeInTheDocument()
  })

  it('disables Add when every catalog skill is already mapped', async () => {
    renderEditor({ skills: CATALOG.map(s => s.key) })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /add skill/i })).toBeDisabled(),
    )
  })

  it('routes the save through beforeSave and reports the resolved target', async () => {
    // Blueprint semantics: editing from a crew forks a private copy first, so
    // the PATCH must hit the forked name and onChange must report THAT name —
    // not agentName — or the caller keeps tracking the shared template.
    const beforeSave = vi.fn().mockResolvedValue('atlas-crewA')
    mockApi.agentPatch.mockResolvedValue({ ok: true, skills: ['widgets'] })
    const { onChange } = renderEditor({ agentName: 'atlas', skills: [], beforeSave })
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))

    await waitFor(() => expect(beforeSave).toHaveBeenCalled())
    await waitFor(() =>
      expect(mockApi.agentPatch).toHaveBeenCalledWith('atlas-crewA', { skills: ['widgets'] }),
    )
    expect(mockApi.agentPatch).not.toHaveBeenCalledWith('atlas', { skills: ['widgets'] })
    await waitFor(() => expect(onChange).toHaveBeenCalledWith('atlas-crewA', ['widgets']))
  })

  it('writes to agentName directly when no beforeSave is given', async () => {
    // The Agent Templates tab passes no beforeSave: the save targets the agent
    // itself, with no fork indirection.
    const { onChange } = renderEditor({ agentName: 'atlas', skills: [] })
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))

    await waitFor(() =>
      expect(mockApi.agentPatch).toHaveBeenCalledWith('atlas', { skills: ['widgets'] }),
    )
    await waitFor(() => expect(onChange).toHaveBeenCalledWith('atlas', ['widgets']))
  })
})

describe('shared instant-save chain (GPT round-26)', () => {
  it('serializes saves onto the provided chain and reports pending state', async () => {
    // The owner (the template pane) drains this one chain before publish and
    // fences the publish button on the pending report — both must be fed.
    const pendingChain = { current: Promise.resolve() as Promise<unknown> }
    const onSavePending = vi.fn()
    let releasePatch: (v: unknown) => void = () => {}
    mockApi.agentPatch.mockImplementationOnce(
      () => new Promise(resolve => { releasePatch = resolve }),
    )
    renderEditor({ agentName: 'atlas', skills: ['grill'], pendingChain, onSavePending })

    // Remove the mapped chip -> a save starts and is held open.
    fireEvent.click(await screen.findByRole('button', { name: /Remove/ }))
    await waitFor(() =>
      expect(mockApi.agentPatch).toHaveBeenCalledWith('atlas', { skills: [] }),
    )
    await waitFor(() => expect(onSavePending).toHaveBeenCalledWith(true))

    // The chain does NOT settle while the save is in the air…
    let settled = false
    void pendingChain.current.then(() => { settled = true })
    await new Promise(resolve => setTimeout(resolve, 30))
    expect(settled).toBe(false)

    // …and settles once it lands, with pending reported back to false.
    releasePatch({ ok: true })
    await waitFor(() => expect(settled).toBe(true))
    await waitFor(() => expect(onSavePending).toHaveBeenCalledWith(false))
  })
})

/* ── Colliding package copies ── */

const DIGEST_A = 'a'.repeat(32)
const DIGEST_B = 'b'.repeat(32)
const KEY_A = `package/${DIGEST_A}:code-review/SKILL.md`
const KEY_B = `package/${DIGEST_B}:code-review/SKILL.md`

/** Two package rows sharing one display name, differing only in the directories above it. */
function colliding(pathA: string, pathB: string) {
  return [
    { key: KEY_A, name: 'code-review', description: 'Review a change', source: 'package', path: pathA },
    { key: KEY_B, name: 'code-review', description: 'Review a change', source: 'package', path: pathB },
  ]
}

describe('disambiguating colliding package copies', () => {
  it('labels each colliding copy by the directories above the skill', async () => {
    mockApi.skills.mockResolvedValue(
      colliding(
        '/home/u/.kiro/skills/papyrus-writer/code-review/SKILL.md',
        '/home/u/.kiro/skills/atlas-tools/code-review/SKILL.md',
      ),
    )
    renderEditor({ skills: [KEY_A, KEY_B] })

    expect(await screen.findByText('Located in skills/papyrus-writer')).toBeInTheDocument()
    expect(screen.getByText('Located in skills/atlas-tools')).toBeInTheDocument()
  })

  it('leaves a name carried by only one copy unqualified', async () => {
    // The qualifier exists for ambiguity, so an ordinary skill must not grow one.
    mockApi.skills.mockResolvedValue([
      { key: 'package/cccc:memory/SKILL.md', name: 'memory', source: 'package', path: '/home/u/.kiro/skills/memory/SKILL.md' },
    ])
    renderEditor({ skills: ['package/cccc:memory/SKILL.md'] })

    await waitFor(() => expect(screen.getByText('memory')).toBeInTheDocument())
    expect(screen.queryByText(/Located in/)).not.toBeInTheDocument()
  })

  it('widens the window past two segments when the nearest two are identical', async () => {
    // A fixed two-segment window renders these twins identically, which is the case the
    // widening exists for: they diverge only ABOVE it.
    mockApi.skills.mockResolvedValue(
      colliding(
        '/opt/one/shared/pack/skills/code-review/SKILL.md',
        '/opt/two/shared/pack/skills/code-review/SKILL.md',
      ),
    )
    renderEditor({ skills: [KEY_A, KEY_B] })

    expect(await screen.findByText('Located in one/shared/pack')).toBeInTheDocument()
    expect(screen.getByText('Located in two/shared/pack')).toBeInTheDocument()
  })

  it('invents no distinction when the path cannot separate the copies', async () => {
    // Two copies at one path have no distinguishing segment, so the group-level widening
    // declines and both fall back to the same shared location rather than a made-up one.
    mockApi.skills.mockResolvedValue(
      colliding('/opt/pack/skills/code-review/SKILL.md', '/opt/pack/skills/code-review/SKILL.md'),
    )
    renderEditor({ skills: [KEY_A, KEY_B] })

    await waitFor(() => expect(screen.getAllByText('code-review')).toHaveLength(2))
    expect(screen.getAllByText('Located in opt/pack')).toHaveLength(2)
  })

  it('splits a Windows path on backslashes', async () => {
    // Without the backslash the whole path is ONE segment that ends in `.md`, which the
    // filter drops -- leaving no label on either copy.
    mockApi.skills.mockResolvedValue(
      colliding(
        'C:\\Users\\u\\.kiro\\skills\\alpha\\code-review\\SKILL.md',
        'C:\\Users\\u\\.kiro\\skills\\beta\\code-review\\SKILL.md',
      ),
    )
    renderEditor({ skills: [KEY_A, KEY_B] })

    expect(await screen.findByText('Located in skills/alpha')).toBeInTheDocument()
    expect(screen.getByText('Located in skills/beta')).toBeInTheDocument()
  })

  it('drops a trailing skills segment, which is the same for every root', async () => {
    // Keeping it would spend one of the two slots on a constant and render twins alike.
    mockApi.skills.mockResolvedValue(
      colliding(
        '/srv/alpha-bundle/skills/code-review/SKILL.md',
        '/srv/beta-bundle/skills/code-review/SKILL.md',
      ),
    )
    renderEditor({ skills: [KEY_A, KEY_B] })

    expect(await screen.findByText('Located in srv/alpha-bundle')).toBeInTheDocument()
    expect(screen.getByText('Located in srv/beta-bundle')).toBeInTheDocument()
  })

  it('elides the middle of a long label, keeping the head and the tail', async () => {
    // End-truncation would hide the one segment that tells these two apart.
    mockApi.skills.mockResolvedValue(
      colliding(
        '/srv/organization-wide-shared-bundles/team-alpha/skills/code-review/SKILL.md',
        '/srv/organization-wide-shared-bundles/team-beta/skills/code-review/SKILL.md',
      ),
    )
    renderEditor({ skills: [KEY_A, KEY_B] })

    const shown = await screen.findByText(/^Located in .*team-alpha$/)
    const where = shown.textContent!.replace('Located in ', '')
    expect(where).toContain(String.fromCharCode(0x2026))
    expect(where.length).toBeLessThanOrEqual(28)
    expect(where.startsWith('organization-')).toBe(true)
  })

  it('shows the full label when eliding would collapse two copies into one string', async () => {
    // These tails differ ONLY inside the region the ellipsis replaces, so eliding both
    // yields one string and the disambiguator would name neither copy.
    mockApi.skills.mockResolvedValue(
      colliding(
        '/r/aaaaaaaaaaaa/1Xbbbbbbbbbbbbbb/skills/code-review/SKILL.md',
        '/r/aaaaaaaaaaaa/2Xbbbbbbbbbbbbbb/skills/code-review/SKILL.md',
      ),
    )
    renderEditor({ skills: [KEY_A, KEY_B] })

    expect(
      await screen.findByText('Located in aaaaaaaaaaaa/1Xbbbbbbbbbbbbbb'),
    ).toBeInTheDocument()
    expect(screen.getByText('Located in aaaaaaaaaaaa/2Xbbbbbbbbbbbbbb')).toBeInTheDocument()
    expect(screen.queryByText(new RegExp(String.fromCharCode(0x2026)))).not.toBeInTheDocument()
  })

  it('separates the two colliding copies in the picker as well as on the chip', async () => {
    // The chip must say what the picker row said, or the user cannot tell which they bound.
    mockApi.skills.mockResolvedValue(
      colliding(
        '/home/u/.kiro/skills/papyrus-writer/code-review/SKILL.md',
        '/home/u/.kiro/skills/atlas-tools/code-review/SKILL.md',
      ),
    )
    renderEditor({ skills: [] })
    await openAddMenu()

    await waitFor(() =>
      expect(screen.getByText('Located in skills/papyrus-writer')).toBeInTheDocument(),
    )
    expect(screen.getByText('Located in skills/atlas-tools')).toBeInTheDocument()
  })
})

describe('an unresolved mapping', () => {
  it('marks a NON-package mapping with no installed copy as unresolved and counts it', async () => {
    // A non-package key the catalog no longer lists is a genuine dead mapping (its source
    // does not silently degrade to empty), so it gets the warn style plus a count line.
    mockApi.skills.mockResolvedValue(CATALOG)
    renderEditor({ skills: ['babysit', 'kiro-workspace/gone/SKILL.md'] })

    await waitFor(() => expect(screen.getByText('babysit')).toBeInTheDocument())
    // The count line at the bottom names the number of dead mappings.
    expect(
      await screen.findByText(/1 mapped skill no longer matches an installed copy/i),
    ).toBeInTheDocument()
  })

  it('does NOT mark an absent package mapping as dead, since /api/skills can degrade to an empty package set', async () => {
    // `GET /api/skills` sources package rows from a timeout-bounded `list_skills()` that
    // degrades to [] with a 200 on timeout, with no completeness signal to the client. A
    // package key missing from that partial response is not evidence the copy is gone, so it
    // must not be flagged — flagging it would tell the user to delete a live mapping.
    mockApi.skills.mockResolvedValue(CATALOG)
    renderEditor({ skills: ['babysit', 'package/deadbeef:code-review/SKILL.md'] })

    await waitFor(() => expect(screen.getByText('babysit')).toBeInTheDocument())
    // No warn count line, and no removal instruction, for the absent package key.
    expect(
      screen.queryByText(/no longer matches an installed copy/i),
    ).not.toBeInTheDocument()
  })

  it('states the reason when the catalog fails to load and disables Add', async () => {
    // A failed load must not look like "you have no skills": it states its reason, and it
    // must not leave the picker enabled offering stale cached options.
    mockApi.skills.mockRejectedValue(new Error('boom'))
    renderEditor({ skills: [] })

    expect(
      await screen.findByText(/Could not load the skill catalog/i),
    ).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /add skill/i })).toBeDisabled(),
    )
  })
})
