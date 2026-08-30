import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  agentPatch: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import AgentSkillsEditor from '../components/AgentSkillsEditor'

/** The refusal the gateway actually sends: a 400 whose body carries the machine code. */
function unknownSkillsRejection(...skills: string[]) {
  return Object.assign(new Error('unknown skills'), {
    body: JSON.stringify({ error: 'unknown skills', skills, code: 'skills_unknown' }),
  })
}

const CATALOG = [  { key: 'babysit', name: 'babysit', description: 'Monitor a PR', source: 'kirocrew' },
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
  return { ...utils, onChange, qc }
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
    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith('specialist', ['babysit', 'widgets']))
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

  it('surfaces a mapping the write preserved instead of reporting a bare success', async () => {
    // The backend keeps a skill URI whose key will not invert, and its response names it in
    // unmanaged_skills. Without passing that back, a removal the config still holds looks done.
    const survivor = 'skill://~/.kiro/skills/stale/SKILL.md'
    const onChange = vi.fn()
    mockApi.agentPatch.mockResolvedValue({
      ok: true,
      skills: ['babysit'],
      unmanaged_skills: [survivor],
    })

    renderEditor({ skills: ['babysit', 'widgets'], onChange })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    fireEvent.click(await screen.findByRole('button', { name: /remove skill widgets/i }))

    await waitFor(() => expect(onChange).toHaveBeenCalled())
    expect(onChange).toHaveBeenLastCalledWith('specialist', ['babysit'], [survivor])
  })

  it('marks a server-refused key immediately, without waiting for the catalog refetch', async () => {
    // The catalog is cached, so at that moment the stale copy still lists the refused key and
    // the mark the remove-blocked sentence names would not be rendered yet.
    const refusedKey = 'widgets'
    mockApi.agentPatch.mockImplementation(async () => {
      throw unknownSkillsRejection(refusedKey)
    })

    renderEditor({ skills: [refusedKey, 'babysit'] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    expect(screen.queryByText(/This no longer matches an installed copy/i)).toBeNull()

    fireEvent.click(await screen.findByRole('button', { name: /remove skill babysit/i }))

    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalled())
    expect(await screen.findByText(/This no longer matches an installed copy/i))
      .toBeInTheDocument()
  })

  it('clears the blocker alone, leaving the pick to be re-picked', async () => {
    const stale = 'stale-chip'
    mockApi.agentPatch.mockImplementation(async (_agent: string, body: { skills: string[] }) => {
      if (body.skills.includes(stale)) throw unknownSkillsRejection(stale)
      return { ok: true, skills: body.skills }
    })

    renderEditor({ skills: [stale] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledTimes(1))

    fireEvent.click(
      await screen.findByRole('button', { name: new RegExp(`remove skill ${stale}`, 'i') }),
    )
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledTimes(2))
    const sent = mockApi.agentPatch.mock.calls.at(-1)?.[1] as { skills: string[] }
    // Held across the refusal's catalog invalidation, an unqualified package key can name a
    // different bundle by now, so it is not re-sent.
    expect(sent.skills).not.toContain('widgets')
    expect(sent.skills).not.toContain(stale)
    await openAddMenu()
    expect(await screen.findByRole('option', { name: /widgets/i })).toBeInTheDocument()
  })

  it('names the blocker unambiguously in the notice when twins collide', async () => {
    // Both rows carry the same name, so a bare interpolation reads "remove shared-skill
    // first; shared-skill will then be added" -- one word standing for two different files.
    const stale = 'stale-chip'
    const twins = [
      {
        key: 'package/aaa11111:shared-skill',
        name: 'shared-skill',
        description: 'twin',
        source: 'package',
        path: '/opt/ed/packages/PkgA/eventId-ALPHA/skills/shared-skill/SKILL.md',
      },
      {
        key: 'package/bbb22222:shared-skill',
        name: 'shared-skill',
        description: 'twin',
        source: 'package',
        path: '/opt/ed/packages/PkgB/eventId-OMEGA/skills/shared-skill/SKILL.md',
      },
    ]
    mockApi.skills.mockResolvedValue([...CATALOG, ...twins])
    mockApi.agentPatch.mockImplementation(async (_agent: string, body: { skills: string[] }) => {
      if (body.skills.includes(stale)) throw unknownSkillsRejection(stale)
      return { ok: true, skills: body.skills }
    })

    renderEditor({ skills: [stale] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    await openAddMenu()
    fireEvent.click((await screen.findAllByRole('option', { name: /shared-skill/i }))[0])
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledTimes(1))

    // The notice names what the user must ACT on. It cannot name the pick: the write that
    // clears the blocker no longer applies one, so naming it would promise an add again.
    const notice = document.body.textContent ?? ''
    expect(notice).toMatch(/stale-chip/)
    expect(notice).not.toMatch(/will then be added/i)
  })

  it('carries nothing when an unrelated removal hits the same blocker', async () => {
    // Every removal write clears only what it removes, the one that fails again on the
    // blocker included: no path re-sends a key the catalog may have re-pointed.
    const stale = 'stale-chip'
    mockApi.agentPatch.mockImplementation(async (_agent: string, body: { skills: string[] }) => {
      if (body.skills.includes(stale)) throw unknownSkillsRejection(stale)
      return { ok: true, skills: body.skills }
    })

    renderEditor({ skills: [stale, 'babysit'] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledTimes(1))

    fireEvent.click(await screen.findByRole('button', { name: /remove skill babysit/i }))
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledTimes(2))

    fireEvent.click(
      await screen.findByRole('button', { name: new RegExp(`remove skill ${stale}`, 'i') }),
    )
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledTimes(3))
    const sent = mockApi.agentPatch.mock.calls.at(-1)?.[1] as { skills: string[] }
    expect(sent.skills).not.toContain('widgets')
    expect(sent.skills).not.toContain(stale)
  })

  it('keeps the differing tail of a long disambiguator visible', async () => {
    // CSS end-truncation cuts the differing tail VISUALLY, so the elision must be in the
    // string for a reader -- or this test -- to see it at all.
    const twins = [
      {
        key: 'package/aaa:shared',
        name: 'shared',
        description: 'twin',
        source: 'package',
        path: '/opt/ed/packages/PkgA/eventId-aaaaaaaaaaaaaaaaaaaa-ALPHA/skills/shared/SKILL.md',
      },
      {
        key: 'package/bbb:shared',
        name: 'shared',
        description: 'twin',
        source: 'package',
        path: '/opt/ed/packages/PkgA/eventId-aaaaaaaaaaaaaaaaaaaa-OMEGA/skills/shared/SKILL.md',
      },
    ]
    mockApi.skills.mockResolvedValue([...CATALOG, ...twins])

    renderEditor({ skills: ['package/aaa:shared', 'package/bbb:shared'] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())

    const alpha = await screen.findByText(/ALPHA/)
    const omega = await screen.findByText(/OMEGA/)
    // Non-vacuity: without elision these labels are 39 chars, so a passing assertion below
    // must be the elision rather than a short label that never needed it.
    for (const el of [alpha, omega]) {
      expect(el.textContent).toContain('\u2026')
    }
    expect(alpha.textContent).not.toEqual(omega.textContent)
  })

  it('does not advertise a combined remove-and-add anywhere on the chip', async () => {
    const stale = 'stale-chip'
    mockApi.agentPatch.mockImplementation(async (_agent: string, body: { skills: string[] }) => {
      if (body.skills.includes(stale)) throw unknownSkillsRejection(stale)
      return { ok: true, skills: body.skills }
    })

    renderEditor({ skills: [stale] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalled())

    // The ✕ no longer adds anything, so neither the chip nor its control may say it does.
    expect(screen.queryByText(/remove to add/i)).toBeNull()
    const btn = await screen.findByRole('button', {
      name: new RegExp(`remove skill ${stale}`, 'i'),
    })
    expect(btn.getAttribute('title') ?? '').not.toMatch(/and add/i)
  })

  it('leaves an unrelated chip a plain remove while a pick is held', async () => {
    const stale = 'stale-chip'
    mockApi.agentPatch.mockImplementation(async (_agent: string, body: { skills: string[] }) => {
      if (body.skills.includes(stale)) throw unknownSkillsRejection(stale)
      return { ok: true, skills: body.skills }
    })

    renderEditor({ skills: [stale, 'babysit'] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledTimes(1))

    const other = await screen.findByRole('button', { name: /^remove skill babysit$/i })
    fireEvent.click(other)
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledTimes(2))

    const sent = mockApi.agentPatch.mock.calls.at(-1)?.[1] as { skills: string[] }
    expect(sent.skills).not.toContain('widgets')
    expect(sent.skills).not.toContain('babysit')
  })

  it('refetches the agent detail on a refusal so a second stale chip can be cleared', async () => {
    mockApi.agentPatch.mockImplementation(async () => {
      throw unknownSkillsRejection('widgets')
    })

    const { qc } = renderEditor({ skills: ['widgets', 'babysit'] })
    // Seeded under the key the app registers, so this asserts a real query was invalidated:
    // a key nothing is registered under invalidates nothing yet still satisfies a spy check.
    qc.setQueryData(['agentDetail', 'specialist'], { name: 'specialist' })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    fireEvent.click(await screen.findByRole('button', { name: /remove skill babysit/i }))

    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalled())
    await waitFor(() =>
      expect(qc.getQueryState(['agentDetail', 'specialist'])?.isInvalidated).toBe(true),
    )
  })

  it('promises no add in the blocked notice, because the write applies none', async () => {
    const stale = 'stale-chip'
    mockApi.agentPatch.mockImplementation(async (_a: string, body: { skills: string[] }) => {
      if (body.skills.includes(stale)) throw unknownSkillsRejection(stale)
      return { ok: true, skills: body.skills }
    })

    renderEditor({ skills: [stale] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))

    // The blocked notice tells the user to clear the blocker; the pick is theirs to re-choose
    // from the refreshed list, so a promise to add it would contradict the write.
    expect(await screen.findByText(/remove stale-chip/i)).toBeInTheDocument()
    expect(screen.queryByText(/will then be added/i)).toBeNull()
  })

  it('names only the removal, on the control and in the notice alike', async () => {
    const stale = 'stale-chip'
    mockApi.agentPatch.mockImplementation(async (_a: string, body: { skills: string[] }) => {
      if (body.skills.includes(stale)) throw unknownSkillsRejection(stale)
      return { ok: true, skills: body.skills }
    })

    renderEditor({ skills: [stale] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalled())

    // The control does one thing, so it claims one thing -- and so does the notice.
    const btn = await screen.findByRole('button', {
      name: new RegExp(`remove skill ${stale}`, 'i'),
    })
    expect(btn.getAttribute('aria-label') ?? '').not.toMatch(/and add/i)
    expect(document.body.textContent ?? '').toMatch(/remove stale-chip/i)
  })

  it('does not rebind a stale key onto a different package shipping the same relative path', async () => {
    // A rel-keyed remap would bind PkgA's mapping to PkgB's file and PATCH that URI into the
    // agent config. A stale key carries a digest, not a package name, so nothing proves them one.
    const otherPackageKey = 'package/aaaabbbbccccddddaaaabbbbccccdddd:notes'
    const staleKey = 'package/1111222233334444111122223333444:notes'
    mockApi.skills.mockResolvedValue([
      ...CATALOG,
      {
        key: otherPackageKey,
        name: 'notes',
        description: 'Notes',
        source: 'package',
        package: 'PkgB',
      },
    ])
    mockApi.agentPatch.mockImplementation(async (_agent: string, body: { skills: string[] }) => {
      const installed = new Set([...CATALOG.map((s: { key: string }) => s.key), otherPackageKey])
      const unknown = body.skills.filter(k => !installed.has(k))
      if (unknown.length) throw unknownSkillsRejection(...unknown)
      return { ok: true, skills: body.skills }
    })

    renderEditor({ skills: [staleKey, 'widgets'] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    fireEvent.click(await screen.findByRole('button', { name: /remove skill widgets/i }))

    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalled())
    const sent = mockApi.agentPatch.mock.calls.at(-1)?.[1] as { skills: string[] }
    expect(sent.skills).not.toContain(otherPackageKey)
    expect(sent.skills).toContain(staleKey)
  })

  it('submits an uninstalled stale key rather than silently dropping its mapping', async () => {
    // No live row carries this rel, so there is nothing to re-key onto. Submitting it lets
    // the server refuse and the notice name it; dropping it would delete the mapping.
    const installed = new Set(CATALOG.map((s: { key: string }) => s.key))
    mockApi.agentPatch.mockImplementation(async (_agent: string, body: { skills: string[] }) => {
      const unknown = body.skills.filter(k => !installed.has(k))
      if (unknown.length) throw unknownSkillsRejection(...unknown)
      return { ok: true, skills: body.skills }
    })

    const goneA = 'package/deadbeefcafe1234deadbeefcafe1234:gone-a'
    const goneB = 'package/beefdeadcafe4321beefdeadcafe4321:gone-b'
    renderEditor({ skills: [goneA, goneB, 'widgets'] })
    expect(await screen.findByText(/2 mapped skills no longer match an installed copy/i))
      .toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: /remove skill gone-a/i }))

    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalled())
    const sent = mockApi.agentPatch.mock.calls.at(-1)?.[1] as { skills: string[] }
    expect(sent.skills).toContain(goneB)
    expect(sent.skills).not.toContain(goneA)
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
    mockApi.agentPatch.mockRejectedValue(unknownSkillsRejection())
    const { onChange } = renderEditor({ skills: [] })
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))

    await waitFor(() =>
      expect(screen.getByText(/pick it again/i)).toBeInTheDocument()
    )
    expect(screen.queryByText(/unknown skills/i)).toBeNull()
    // With several chips mapped, a notice naming none of them cannot say WHICH pick failed.
    expect(screen.getByText(/widgets/)).toBeInTheDocument()
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
        removed_unmanaged_skills: ['skill://~/.kiro/skills/*/SKILL.md'],
        unmanaged_skills: ['skill://~/.kiro/skills/*/SKILL.md'],
      }),
    )
    // A wildcard mapping is still a mapping — the empty state must not claim
    // the agent has none.
    expect(screen.queryByText(/No skills mapped/i)).not.toBeInTheDocument()
  })

  it('re-enumerates the catalog after a rejected save, so a retry cannot re-send a stale key', async () => {
    // A bundle upgrade under a live editor re-spells every package key, so the cached
    // catalog would have the retry re-send the identical rejected key.
    mockApi.agentPatch.mockRejectedValue(unknownSkillsRejection())
    renderEditor({ skills: [] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalledTimes(1))

    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))

    await waitFor(() =>
      expect(screen.getByText(/pick it again/i)).toBeInTheDocument()
    )
    expect(screen.queryByText(/unknown skills/i)).toBeNull()
    await waitFor(() => expect(mockApi.skills.mock.calls.length).toBeGreaterThan(1))
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
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledWith('atlas', { skills: [] }))
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

describe('twin picker rows without a package field', () => {
  it('announces the distinguishing path, not a hex digest', async () => {
    // Deliberately NO `package` field, which is the case the edition may not supply, and
    // identical name AND description, so nothing but a disambiguator can tell them apart.
    mockApi.skills.mockResolvedValue([
      {
        key: 'package/aaaaaaaa1111111111111111111111111:shared-skill',
        name: 'shared-skill',
        description: 'A shared skill',
        source: 'package',
        path: '/editions/PkgA/skills/shared-skill/SKILL.md',
      },
      {
        key: 'package/bbbbbbbb2222222222222222222222222:shared-skill',
        name: 'shared-skill',
        description: 'A shared skill',
        source: 'package',
        path: '/editions/PkgB/skills/shared-skill/SKILL.md',
      },
    ])
    renderEditor({ skills: [] })
    await openAddMenu()

    const options = await screen.findAllByRole('option')
    expect(options).toHaveLength(2)

    const announced = options.map(o => (o.textContent ?? '').trim())
    // Non-vacuity: both rows must really carry the shared name, or the fixture is not the
    // twin case this pins.
    expect(announced.every(t => t.includes('shared-skill'))).toBe(true)
    expect(new Set(announced).size).toBe(2)
    // The readable location is what a person can act on; a bare 8-hex tail is not.
    expect(announced.some(t => t.includes('PkgA'))).toBe(true)
    expect(announced.some(t => t.includes('PkgB'))).toBe(true)
    for (const t of announced) {
      expect(t).not.toMatch(/\b[0-9a-f]{8}\b/)
    }
  })
})

describe('an unresolved mapping', () => {
  it('signals by an icon, not by colour alone', async () => {
    const { container } = renderEditor({ skills: ['package/deadbeefcafe1234:gone-skill'] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    // lucide renders an svg per icon and tags it with its own name, so the alert icon's
    // presence is checkable without asserting on colour classes.
    await waitFor(() =>
      expect(container.querySelector('svg.lucide-triangle-alert, svg.lucide-alert-triangle'))
        .not.toBeNull()
    )
  })
})

describe('a mapped chip while the catalog is still unknown', () => {
  it('does not claim not-installed until the query has succeeded', async () => {
    let release: (rows: unknown[]) => void = () => {}
    mockApi.skills.mockReturnValue(new Promise(res => { release = res as typeof release }))
    renderEditor({ skills: ['widgets'] })

    // The catalog is in flight: an empty list here is not evidence the skill is gone.
    await waitFor(() => expect(screen.getByText('widgets')).toBeInTheDocument())
    expect(screen.queryByText(/not currently installed/i)).toBeNull()

    release(CATALOG)
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
  })
})

describe('the rejection notice', () => {
  it('names the key the backend refused, not whatever was last attempted', async () => {
    // The refusal is whole-PATCH, so on an ADD the offender can be a DIFFERENT stale key
    // than the one just picked. Re-picking cannot clear it -- every add re-sends the same
    // mapped key and is refused identically -- so the advice must name the removal.
    mockApi.agentPatch.mockRejectedValue(unknownSkillsRejection('kiro-user/prepare-pr'))
    renderEditor({ skills: ['kiro-user/prepare-pr'] })
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))

    await waitFor(() =>
      expect(screen.getByText(/remove prepare-pr \(marked with a warning\) first/i)).toBeInTheDocument()
    )
    const notice = screen.getByText(/remove prepare-pr \(marked with a warning\) first/i).textContent ?? ''
    expect(notice).toContain('prepare-pr')
    // The pick may be named as what FOLLOWS the removal; what this forbids is naming it as
    // the thing to remove, which is the mis-blame, and offering a re-pick as the remedy.
    expect(notice).not.toMatch(/remove widgets/i)
    expect(notice).not.toMatch(/has changed/i)
    expect(notice.trimStart().startsWith(':')).toBe(false)
    // "Pick this skill again" would be advice that cannot succeed here.
    expect(screen.queryByText(/pick it again/i)).toBeNull()
  })

  it('does advise a re-pick when the refused key IS the one just picked', async () => {
    // The one case re-picking can fix: the pick itself is gone from the catalog.
    mockApi.agentPatch.mockRejectedValue(unknownSkillsRejection('widgets'))
    renderEditor({ skills: [] })
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /widgets/i }))

    await waitFor(() =>
      expect(screen.getByText(/pick it again/i)).toBeInTheDocument()
    )
    expect(screen.queryByText(/remove .* first/i)).toBeNull()
  })
})

describe('a name shared with a non-package skill', () => {
  it('is not treated as a colliding copy', async () => {
    // The qualifier exists for colliding BUNDLES. A user's own copy of a crew skill shares
    // the name without being an ambiguity, so tagging it would be noise.
    mockApi.skills.mockResolvedValue([
      { key: 'babysit', name: 'babysit', description: 'Monitor a PR', source: 'kirocrew' },
      {
        key: 'kiro-user/babysit',
        name: 'babysit',
        description: 'Monitor a PR',
        source: 'kiro-user',
        path: '/home/u/.kiro/crew/skills/babysit/SKILL.md',
      },
    ])
    renderEditor({ skills: [] })
    await openAddMenu()

    const options = await screen.findAllByRole('option')
    expect(options).toHaveLength(2)
    for (const o of options) {
      expect(o.textContent ?? '').not.toContain('.kiro/crew')
    }
  })
})

describe('an unresolved mapping', () => {
  it('states the reason in visible text, not only a hover title', async () => {
    renderEditor({ skills: ['package/deadbeefcafe1234:gone-skill'] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    // A `title`/`aria-label` reaches a mouse and a screen reader; a sighted keyboard or
    // touch user sees only the colour and the icon without this.
    await waitFor(() =>
      expect(screen.getByText(/1 mapped skill no longer matches an installed copy/i)).toBeInTheDocument()
    )
  })

  it('counts the stale chips instead of saying "this copy" over several', async () => {
    // One singular note beside two stale chips points at neither of them.
    renderEditor({
      skills: ['package/deadbeefcafe1234:gone-skill', 'package/feedfacecafe5678:also-gone'],
    })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())

    await waitFor(() =>
      expect(screen.getByText(/2 mapped skills no longer match an installed copy/i)).toBeInTheDocument()
    )
    // Scoped to VISIBLE text: each unresolved chip also carries the note in an `sr-only`
    // span, which is per-chip and correct even when several are unresolved.
    const visibleSingular = screen
      .queryAllByText(/^this skill/i)
      .filter(el => !el.className.includes('sr-only'))
    expect(visibleSingular).toHaveLength(0)
  })
})

describe('a refused REMOVAL', () => {
  it('says what to remove, not to pick again', async () => {
    mockApi.agentPatch.mockRejectedValue(unknownSkillsRejection('kiro-user/prepare-pr'))
    renderEditor({ skills: ['babysit', 'kiro-user/prepare-pr'] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())

    const removes = await screen.findAllByRole('button', { name: /remove skill/i })
    fireEvent.click(removes[0])

    await waitFor(() =>
      expect(screen.getByText(/remove prepare-pr \(marked with a warning\) first/i)).toBeInTheDocument()
    )
    const notice = screen.getByText(/remove prepare-pr \(marked with a warning\) first/i).textContent ?? ''
    // States the OUTCOME, so the user knows the chip is still mapped and why.
    expect(notice).toMatch(/couldn't save/i)
    // The add-branch copy must NOT be what a removal shows.
    expect(screen.queryByText(/no longer listed under that id/i)).toBeNull()
  })

  it('renders no bare leading colon when the refusal names no key', async () => {
    // The detail carries the phrase but no key, so the offender is empty; a composed
    // "<name>: ..." form then renders a dangling colon that trimStart cannot remove.
    mockApi.agentPatch.mockRejectedValue(unknownSkillsRejection())
    renderEditor({ skills: ['babysit', 'kiro-user/prepare-pr'] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())

    const removes = await screen.findAllByRole('button', { name: /remove skill/i })
    fireEvent.click(removes[0])

    await waitFor(() => expect(screen.getByText(/couldn't save/i)).toBeInTheDocument())
    const notice = screen.getByText(/couldn't save/i).textContent ?? ''
    expect(notice.trimStart().startsWith(':')).toBe(false)
    expect(notice).not.toMatch(/:\s*$/)
    // And it must not ask the user to remove a nameless something.
    expect(notice).not.toMatch(/remove\s+first/i)
  })
})

describe('twins whose paths diverge ABOVE the two-segment window', () => {
  it('widens the disambiguator until the rows actually differ', async () => {
    // `skills` is only stripped when TRAILING, so with the skill one level deeper both rows
    // render `skills/sub` under a fixed last-two window and the eventId never appears.
    mockApi.skills.mockResolvedValue([
      {
        key: 'package/aaaa1111:shared-skill',
        name: 'shared-skill',
        description: 'Same text both copies',
        source: 'package',
        path: '/srv/packages/PkgA/eventId-1/skills/sub/shared-skill/SKILL.md',
      },
      {
        key: 'package/bbbb2222:shared-skill',
        name: 'shared-skill',
        description: 'Same text both copies',
        source: 'package',
        path: '/srv/packages/PkgA/eventId-2/skills/sub/shared-skill/SKILL.md',
      },
    ])
    renderEditor({ skills: [] })
    await openAddMenu()

    const options = await screen.findAllByRole('option')
    expect(options).toHaveLength(2)
    const labels = options.map(o => o.textContent ?? '')
    // Each row must name its own eventId, and the two rows must not read alike.
    expect(labels[0]).toContain('eventId-1')
    expect(labels[1]).toContain('eventId-2')
    expect(labels[0]).not.toEqual(labels[1])
    // And not by falling back to a digest, which names nothing the user picked by.
    for (const l of labels) expect(l).not.toMatch(/aaaa1111|bbbb2222/)
  })
})

describe('the deciding line on a twin row', () => {
  it('is not the faintest text on the row', async () => {
    mockApi.skills.mockResolvedValue([
      {
        key: 'package/aaaa1111:shared-skill',
        name: 'shared-skill',
        description: 'Same text both copies',
        source: 'package',
        path: '/srv/packages/PkgA/skills/shared-skill/SKILL.md',
      },
      {
        key: 'package/bbbb2222:shared-skill',
        name: 'shared-skill',
        description: 'Same text both copies',
        source: 'package',
        path: '/srv/packages/PkgB/skills/shared-skill/SKILL.md',
      },
    ])
    renderEditor({ skills: [] })
    await openAddMenu()

    const rows = await screen.findAllByRole('option')
    const tail = rows[0].querySelector('span:last-child')
    expect(tail?.textContent).toContain('PkgA')
    // The name uses text-text; the description is muted. This line decides the click, so it
    // must not be dimmer than the description it sits under.
    expect(tail?.className ?? '').toContain('text-text')
    expect(tail?.className ?? '').not.toContain('text-muted')
  })

  it('renders no line at all rather than a raw digest when no path is available', async () => {
    // No `path` and no `package`, so the only remaining spelling is the qualified key --
    // which the chip refuses, so an omission beats an opaque token the user cannot act on.
    mockApi.skills.mockResolvedValue([
      {
        key: 'package/aaaa1111aaaa1111aaaa1111aaaa1111:shared-skill',
        name: 'shared-skill',
        description: 'Same text both copies',
        source: 'package',
      },
      {
        key: 'package/bbbb2222bbbb2222bbbb2222bbbb2222:shared-skill',
        name: 'shared-skill',
        description: 'Same text both copies',
        source: 'package',
      },
    ])
    renderEditor({ skills: [] })
    await openAddMenu()

    const rows = await screen.findAllByRole('option')
    expect(rows).toHaveLength(2)
    for (const r of rows) {
      expect(r.textContent ?? '').not.toMatch(/aaaa1111|bbbb2222/)
      expect(r.textContent ?? '').not.toMatch(/package\//)
    }
  })
})

describe('a Windows-style row path', () => {
  it('still yields a distinguishing label for colliding copies', async () => {
    // Splitting on '/' alone leaves a backslash path as ONE segment, so every colliding row
    // renders the same label and the disambiguator stops disambiguating.
    mockApi.skills.mockResolvedValue([
      {
        key: 'package/aaaa1111:shared-skill',
        name: 'shared-skill',
        description: 'Same text both copies',
        source: 'package',
        path: 'C:\\srv\\packages\\PkgA\\skills\\shared-skill\\SKILL.md',
      },
      {
        key: 'package/bbbb2222:shared-skill',
        name: 'shared-skill',
        description: 'Same text both copies',
        source: 'package',
        path: 'C:\\srv\\packages\\PkgB\\skills\\shared-skill\\SKILL.md',
      },
    ])
    renderEditor({ skills: [] })
    await openAddMenu()

    const rows = await screen.findAllByRole('option')
    const labels = rows.map(r => r.textContent ?? '')
    expect(labels[0]).toContain('PkgA')
    expect(labels[1]).toContain('PkgB')
    expect(labels[0]).not.toEqual(labels[1])
    // And not by rendering the whole path as one undivided segment.
    for (const l of labels) expect(l).not.toContain('C:\\srv')
  })
})

describe('the mapped-chip disambiguator', () => {
  // The picker row's own class, read from source: the chip mirrors this line, and a
  // literal repeated here would let the two drift while this test still passed.
  const pickerRowClass = (() => {
    const src = readFileSync(
      join(__dirname, '..', 'components', 'AgentSkillsEditor.tsx'),
      'utf-8'
    )
    // Anchored on the span that renders copy_identifier_label, not on the class shape:
    // the row's skill-name line shares that shape at a larger size.
    const m = src.match(
      /className="(block text-\[\d+px\] font-mono text-text truncate)"[\s\S]{0,120}?copy_identifier_label/
    )
    if (!m) throw new Error('picker row location line not found in AgentSkillsEditor.tsx')
    return m[1]
  })()

  it('the capture harness rejects with the body the component actually branches on', () => {
    // The component keys on `code`, so a prose-only rejection renders no notice and the
    // rejection scene captures an unchanged page -- silently, since the harness still exits.
    const src = readFileSync(
      join(__dirname, '..', '..', 'scripts', 'capture-package-skill-key-qualifier.mjs'),
      'utf-8'
    )
    const m = src.match(/rejectPatch\)\s*\{[\s\S]*?body:\s*JSON\.stringify\(([\s\S]*?)\),\s*\}\)/)
    if (!m) throw new Error('rejection fulfil body not found in the capture harness')
    const literal = m[1]
    expect(literal).toContain("code: 'skills_unknown'")
    // And it must name a skill, not a placeholder, so the notice interpolates a real key.
    expect(literal).toMatch(/skills:\s*\[/)
    expect(literal).not.toContain('<stale digest>')
  })

  it('carries the same weight as the picker row line', async () => {
    // Two otherwise-identical chips: this span is the only text saying which copy is bound,
    // so rendering it dimmer than everything else defeats its purpose.
    mockApi.skills.mockResolvedValue([
      {
        key: 'package/aaaa1111:shared-skill',
        name: 'shared-skill',
        source: 'package',
        path: '/srv/packages/PkgA/skills/shared-skill/SKILL.md',
      },
      {
        key: 'package/bbbb2222:shared-skill',
        name: 'shared-skill',
        source: 'package',
        path: '/srv/packages/PkgB/skills/shared-skill/SKILL.md',
      },
    ])
    renderEditor({ skills: ['package/aaaa1111:shared-skill'] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())

    const span = await waitFor(() => {
      const el = Array.from(document.querySelectorAll('span')).find(
        s =>
          (s.textContent ?? '').includes('PkgA') &&
          /text-\[\d+px\]/.test(s.className) &&
          !s.className.includes('font-mono')
      )
      if (!el) throw new Error('chip disambiguator not rendered')
      return el
    })
    // Derived from the picker row's own class rather than repeated as a literal, so the
    // two cannot drift apart while a test asserting parity keeps passing.
    const rowSize = pickerRowClass.match(/text-\[(\d+)px\]/)?.[1]
    expect(rowSize).toBeTruthy()
    expect(span.className).toContain(`text-[${rowSize}px]`)
    expect(span.className).toContain('text-text')
    expect(span.className).not.toContain('text-muted')
  })

  it('labels the fragment in context rather than leaving its meaning to the tooltip', async () => {
    // A bare tail like `PkgA/skills` does not say what it is; the visible prefix is what makes
    // the line legible without hovering, so it is asserted on the rendered text.
    mockApi.skills.mockResolvedValue([
      {
        key: 'package/aaaa1111:shared-skill',
        name: 'shared-skill',
        source: 'package',
        path: '/srv/packages/PkgA/skills/shared-skill/SKILL.md',
      },
      {
        key: 'package/bbbb2222:shared-skill',
        name: 'shared-skill',
        source: 'package',
        path: '/srv/packages/PkgB/skills/shared-skill/SKILL.md',
      },
    ])
    renderEditor({ skills: ['package/aaaa1111:shared-skill'] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())

    const span = await waitFor(() => {
      const el = Array.from(document.querySelectorAll('span')).find(
        s =>
          (s.textContent ?? '').includes('PkgA') &&
          /text-\[\d+px\]/.test(s.className) &&
          !s.className.includes('font-mono')
      )
      if (!el) throw new Error('chip disambiguator not rendered')
      return el
    })
    expect(span.textContent).toMatch(/Located in\s+\S*PkgA/)
  })


  it('does not resubmit a held pick when the blocker is removed', async () => {
    // The pick is an UNQUALIFIED package key: unique only while its bundle is the sole vendor,
    // which is exactly what the refusal's catalog invalidation can change underneath it.
    const stale = 'stale-chip'
    const pick = 'package/foo'
    const fromRootA = {
      key: pick,
      name: 'foo',
      description: 'vendored once',
      source: 'package',
      path: '/opt/ed/packages/RootA/skills/foo/SKILL.md',
    }
    const fromRootB = { ...fromRootA, path: '/opt/ed/packages/RootB/skills/foo/SKILL.md' }

    mockApi.skills.mockResolvedValueOnce([...CATALOG, fromRootA])
    // Every refetch after the refusal answers from the OTHER root: the bundle that minted the
    // held key is gone and this one now vends the name.
    mockApi.skills.mockResolvedValue([...CATALOG, fromRootB])
    mockApi.agentPatch.mockImplementation(async (_agent: string, body: { skills: string[] }) => {
      if (body.skills.includes(stale)) throw unknownSkillsRejection(stale)
      return { ok: true, skills: body.skills }
    })

    renderEditor({ skills: [stale] })
    await waitFor(() => expect(mockApi.skills).toHaveBeenCalled())
    await openAddMenu()
    fireEvent.click(await screen.findByRole('option', { name: /foo/i }))
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByRole('button', { name: new RegExp(`remove.*${stale}`, 'i') }))
    await waitFor(() => expect(mockApi.agentPatch).toHaveBeenCalledTimes(2))

    const sent = mockApi.agentPatch.mock.calls[1][1] as { skills: string[] }
    expect(sent.skills).not.toContain(pick)
    expect(sent.skills).toEqual([])
  })


})

it('does not offer the picker while the catalog failed to load, so no empty state contradicts the notice', async () => {
  mockApi.skills.mockRejectedValue(new Error('catalog unavailable'))
  renderEditor({ skills: [] })

  // The notice is the truthful signal; the picker must not answer with a different story.
  await waitFor(() => expect(screen.getByTestId('agent-skills-catalog-error')).toBeInTheDocument())
  const add = screen.getByRole('button', { name: /add skill/i })
  expect(add).toBeDisabled()
  fireEvent.click(add)
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  expect(screen.queryByText(/No matching skills/i)).not.toBeInTheDocument()
})


it('names the refused copy with its disambiguator, since colliding twins share a name', async () => {
  // The bare catalog name names NEITHER copy of a collision, so a refusal that used it left
  // the reader unable to tell which of two identically-named rows failed.
  mockApi.skills.mockResolvedValue([
    {
      key: 'package/aaa1:shared',
      name: 'shared',
      description: 'from Alpha',
      source: 'package',
      path: '/pkgs/Alpha/skills/shared/SKILL.md',
    },
    {
      key: 'package/bbb2:shared',
      name: 'shared',
      description: 'from Beta',
      source: 'package',
      path: '/pkgs/Beta/skills/shared/SKILL.md',
    },
  ])
  mockApi.agentPatch.mockRejectedValue(unknownSkillsRejection('package/aaa1:shared'))
  renderEditor({ skills: [] })

  await openAddMenu()
  fireEvent.click((await screen.findAllByRole('option', { name: /shared/i }))[0])

  // The disambiguator is rendered as "name (where)", so a bare "shared" fails this.
  await waitFor(() => expect(screen.getByText(/shared \(/)).toBeInTheDocument())
})
