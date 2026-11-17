import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skill: vi.fn(),
  skillTree: vi.fn(),
  skillFile: vi.fn(),
  createSkill: vi.fn(),
  updateSkill: vi.fn(),
  deleteSkill: vi.fn(),
  skillsPending: vi.fn(),
  skillPendingDetail: vi.fn(),
  approvePendingSkill: vi.fn(),
  dismissPendingSkill: vi.fn(),
  dismissAllPendingSkills: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

vi.mock('../components/SkillDirectoryBrowser', () => ({
  default: () => <div data-testid="dir-browser">browser</div>,
}))

vi.mock('../components/DiffBlock', () => ({
  default: ({ code }: { code: string }) => <pre data-testid="diff">{code}</pre>,
}))

import SkillsTab from '../pages/overview/SkillsTab'

function renderWithQuery() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><SkillsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

const update = (slug: string, createdAt: string | undefined, target = 'auto/deploy-helper') => ({
  slug,
  name: `auto/${slug}`,
  description: 'deploy the demo service',
  has_scripts: false,
  kind: 'update',
  target,
  base_version: 1,
  created_at: createdAt,
})

// The server lists the queue by slug, which is not staging order: the newest
// update sorts first here, so the tests cannot pass by reading list position.
const NEWEST = update('deploy-helper-a', '2026-11-13T03:00:03+00:00')
const OLDER = update('deploy-helper-b', '2026-11-13T03:00:02+00:00')
const OLDEST = update('deploy-helper-c', '2026-11-13T03:00:01+00:00')

const DETAIL = {
  name: 'auto/deploy-helper-a',
  content: '',
  scripts: [],
  content_sha256: 'a'.repeat(64),
  diff: '--- live\n+++ proposed\n@@ -1 +1 @@\n-old\n+new\n',
  from_version: 1,
  to_version: 2,
  stale_base: false,
}

const rowOf = async (name: string) => {
  const title = await screen.findByText(name, undefined, { timeout: 5000 })
  return title.closest('div.rounded-md') as HTMLElement
}

async function openAndCheck(rowName: string) {
  const row = await rowOf(rowName)
  fireEvent.click(within(row).getByText('Review'))
  await waitFor(() => expect(within(row).getByTestId('diff')).toBeTruthy())
  return row
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => 'mockReset' in m && m.mockReset())
  mockApi.skills.mockResolvedValue([])
  mockApi.skill.mockResolvedValue({ name: 'x', content: '---\nname: x\n---\nbody' })
  mockApi.skillsPending.mockResolvedValue({ pending: [NEWEST, OLDER, OLDEST] })
  mockApi.skillPendingDetail.mockResolvedValue(DETAIL)
  mockApi.approvePendingSkill.mockResolvedValue({ approved: 'auto/deploy-helper' })
  mockApi.dismissAllPendingSkills.mockResolvedValue({ dismissed_count: 2 })
})

describe('SkillsTab stacked updates to one skill', () => {
  it('labels every update that has a newer one queued, and not the newest', async () => {
    renderWithQuery()
    expect(within(await rowOf('auto/deploy-helper-b')).getByText('Newer update exists')).toBeTruthy()
    expect(within(await rowOf('auto/deploy-helper-c')).getByText('Newer update exists')).toBeTruthy()
    expect(within(await rowOf('auto/deploy-helper-a')).queryByText('Newer update exists')).toBeNull()
    // The reason the older cards stop being approvable is visible text, collapsed.
    const hint = /A newer update to auto\/deploy-helper is queued\. Approving it means this one can no longer be approved\./
    expect(within(await rowOf('auto/deploy-helper-b')).getByText(hint)).toBeTruthy()
    expect(within(await rowOf('auto/deploy-helper-a')).queryByText(hint)).toBeNull()
  })

  it('names the older updates on the newest card and dismisses them after the approval', async () => {
    renderWithQuery()
    const row = await openAndCheck('auto/deploy-helper-a')
    const box = within(row).getByLabelText('Also dismiss 2 older updates to auto/deploy-helper') as HTMLInputElement
    expect(box.checked).toBe(false)
    expect(within(row).getByText(
      /Checking this removes them from the review queue when this update is approved: auto\/deploy-helper-c and auto\/deploy-helper-b/,
    )).toBeTruthy()
    fireEvent.click(box)
    fireEvent.click(within(row).getByText('Approve'))
    await waitFor(() => expect(mockApi.dismissAllPendingSkills).toHaveBeenCalledTimes(1))
    expect(mockApi.approvePendingSkill).toHaveBeenCalledWith('deploy-helper-a', 'a'.repeat(64))
    expect(mockApi.dismissAllPendingSkills).toHaveBeenCalledWith(['deploy-helper-c', 'deploy-helper-b'])
    // Dismissing waits for the approval: the order of the two calls is the contract.
    expect(mockApi.approvePendingSkill.mock.invocationCallOrder[0])
      .toBeLessThan(mockApi.dismissAllPendingSkills.mock.invocationCallOrder[0])
  })

  it('approves alone when the offer is left unchecked', async () => {
    renderWithQuery()
    const row = await openAndCheck('auto/deploy-helper-a')
    fireEvent.click(within(row).getByText('Approve'))
    await waitFor(() => expect(mockApi.approvePendingSkill).toHaveBeenCalledTimes(1))
    // Let the success handler run before asserting nothing else was sent.
    await waitFor(() => expect(mockApi.skillsPending.mock.calls.length).toBeGreaterThan(1))
    expect(mockApi.dismissAllPendingSkills).not.toHaveBeenCalled()
  })

  it('dismisses nothing when the approval is refused', async () => {
    mockApi.approvePendingSkill.mockRejectedValue(
      Object.assign(new Error('changed'), { status: 409, body: JSON.stringify({ code: 'pending_skill_changed' }) }),
    )
    renderWithQuery()
    const row = await openAndCheck('auto/deploy-helper-a')
    fireEvent.click(within(row).getByLabelText('Also dismiss 2 older updates to auto/deploy-helper'))
    fireEvent.click(within(row).getByText('Approve'))
    await waitFor(() => expect(mockApi.approvePendingSkill).toHaveBeenCalledTimes(1))
    expect(await within(row).findByText(/changed after you read it/)).toBeTruthy()
    expect(mockApi.dismissAllPendingSkills).not.toHaveBeenCalled()
  })

  it('leaves out updates to another skill and updates with no staging time', async () => {
    mockApi.skillsPending.mockResolvedValue({
      pending: [NEWEST, update('other-old', '2026-11-13T03:00:00+00:00', 'auto/other'), update('untimed', undefined)],
    })
    renderWithQuery()
    const row = await openAndCheck('auto/deploy-helper-a')
    expect(within(row).queryByLabelText(/Also dismiss/)).toBeNull()
    expect(within(await rowOf('auto/other-old')).queryByText('Newer update exists')).toBeNull()
    expect(within(await rowOf('auto/untimed')).queryByText('Newer update exists')).toBeNull()
  })

  it('offers a middle card only the updates older than it', async () => {
    renderWithQuery()
    const row = await rowOf('auto/deploy-helper-b')
    fireEvent.click(within(row).getByText('Review'))
    await waitFor(() => expect(within(row).getByTestId('diff')).toBeTruthy())
    expect(within(row).getByLabelText('Also dismiss 1 older update to auto/deploy-helper')).toBeTruthy()
    expect(within(row).queryByText(/auto\/deploy-helper-a/)).toBeNull()
  })
})
