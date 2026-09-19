import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
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

  it('removing a chip PATCHes only the removal', async () => {
    // Every mapping the write does not name is the writer's to preserve, so it names one thing.
    renderEditor({ skills: ['babysit', 'widgets'] })
    fireEvent.click(await screen.findByRole('button', { name: /remove skill babysit/i }))

    await waitFor(() =>
      expect(mockApi.agentPatch).toHaveBeenCalledWith('specialist', {
        removed_skill: 'babysit',
      }),
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

  it('asks before removing an unmanaged URI, since the picker cannot put one back', async () => {
    const uri = 'skill://~/.kiro/skills/*/SKILL.md'
    renderEditor({ skills: [], unmanaged: [uri] })
    await waitFor(() => expect(screen.getByText(uri)).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /Remove skill/i }))
    await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument())
    expect(mockApi.agentPatch).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toHaveTextContent(uri)
  })

  it('removes an unmanaged skill:// URI by NAMING it, never by omitting it', async () => {
    renderEditor({ skills: [], unmanaged: ['skill://~/.kiro/skills/*/SKILL.md'] })
    await waitFor(() =>
      expect(screen.getByText('skill://~/.kiro/skills/*/SKILL.md')).toBeInTheDocument(),
    )
    const x = screen.getByRole('button', { name: /Remove skill/i })
    // The action group is capped at two controls, so this one lives in its own region.
    expect(screen.getByTestId('agent-skills-unmanaged-region')).toContainElement(x)
    fireEvent.click(x)
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /^remove/i }))
    await waitFor(() =>
      expect(mockApi.agentPatch).toHaveBeenCalledWith('specialist', {
        // No `skills`: resubmitting this client's managed keys would overwrite whatever a
        // concurrent session mapped since they were read.
        removed_unmanaged_skill: 'skill://~/.kiro/skills/*/SKILL.md',
        unmanaged_skills: ['skill://~/.kiro/skills/*/SKILL.md'],
      }),
    )
    // A wildcard mapping is still a mapping — the empty state must not claim
    // the agent has none.
    expect(screen.queryByText(/No skills mapped/i)).not.toBeInTheDocument()
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
      expect(mockApi.agentPatch).toHaveBeenCalledWith('atlas', { removed_skill: 'grill' }),
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

/** An ApiError carries the structured refusal on `body`; the prose message is separate. */
class RefusalError extends Error {
  body: string
  constructor(message: string, body: string) {
    super(message)
    this.body = body
  }
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

describe('reading the backend refusal from its structured body', () => {
  const CATALOG_WITH_TWINS = colliding(
    '/home/u/.kiro/skills/papyrus-writer/code-review/SKILL.md',
    '/home/u/.kiro/skills/atlas-tools/code-review/SKILL.md',
  )

  it('names the refused key the way the picker labels it, not as a bare digest', async () => {
    // The refusal is whole-PATCH, so the offender need not be the key this call added.
    mockApi.skills.mockResolvedValue(CATALOG_WITH_TWINS)
    mockApi.agentPatch.mockRejectedValue(
      new RefusalError('unknown skills', JSON.stringify({ code: 'skills_unknown', skills: [KEY_A] })),
    )
    renderEditor({ skills: [] })
    await openAddMenu()
    const rows = await screen.findAllByRole('option', { name: /code-review/i })
    fireEvent.click(rows[1])

    const msg = await screen.findByText(/Couldn't save/)
    expect(msg).toHaveTextContent('code-review (skills/papyrus-writer)')
    expect(msg.textContent).not.toContain(DIGEST_A)
  })

  it('tells the user to re-pick when the refused key is the one just chosen', async () => {
    mockApi.skills.mockResolvedValue(CATALOG_WITH_TWINS)
    renderEditor({ skills: [] })
    await openAddMenu()
    const rows = await screen.findAllByRole('option', { name: /code-review/i })
    mockApi.agentPatch.mockRejectedValue(
      new RefusalError('unknown skills', JSON.stringify({ code: 'skills_unknown', skills: [KEY_A] })),
    )
    fireEvent.click(rows[0])

    expect(await screen.findByText(/pick it again from the refreshed list/)).toBeInTheDocument()
  })

  it('falls back to a generic refusal when the body names no usable key', async () => {
    // A removal names no key of its own, so with an empty list there is nothing to name.
    mockApi.skills.mockResolvedValue(CATALOG_WITH_TWINS)
    mockApi.agentPatch.mockRejectedValue(
      new RefusalError('unknown skills', JSON.stringify({ code: 'skills_unknown', skills: [] })),
    )
    renderEditor({ skills: [KEY_A] })
    fireEvent.click(await screen.findByRole('button', { name: /remove skill/i }))

    expect(
      await screen.findByText(/a mapped skill no longer matches an installed copy/),
    ).toBeInTheDocument()
  })

  it('marks the refused mapping as unresolved even while the cached catalog still lists it', async () => {
    mockApi.skills.mockResolvedValue(CATALOG_WITH_TWINS)
    mockApi.agentPatch.mockRejectedValue(
      new RefusalError('unknown skills', JSON.stringify({ code: 'skills_unknown', skills: [KEY_A] })),
    )
    renderEditor({ skills: [KEY_A] })
    fireEvent.click(await screen.findByRole('button', { name: /remove skill/i }))

    await waitFor(() => expect(screen.getByText(/Couldn't save/)).toBeInTheDocument())
    const chip = screen.getByText('code-review').closest('span')!
    expect(chip).toHaveAttribute('title', expect.stringContaining('no longer matches'))
  })

  it('reports a structured body whose code is a different refusal as its own message', async () => {
    mockApi.skills.mockResolvedValue(CATALOG_WITH_TWINS)
    mockApi.agentPatch.mockRejectedValue(
      new RefusalError('agent is read-only', JSON.stringify({ code: 'agent_readonly' })),
    )
    renderEditor({ skills: [KEY_A] })
    fireEvent.click(await screen.findByRole('button', { name: /remove skill/i }))

    expect(await screen.findByText('agent is read-only')).toBeInTheDocument()
    expect(screen.queryByText(/Couldn't save/)).not.toBeInTheDocument()
  })

  it('reports a body that only looks like JSON as its own message', async () => {
    mockApi.skills.mockResolvedValue(CATALOG_WITH_TWINS)
    mockApi.agentPatch.mockRejectedValue(new RefusalError('gateway timeout', '{"code": trunc'))
    renderEditor({ skills: [KEY_A] })
    fireEvent.click(await screen.findByRole('button', { name: /remove skill/i }))

    expect(await screen.findByText('gateway timeout')).toBeInTheDocument()
  })
})
