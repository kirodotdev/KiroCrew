/**
 * AgentSkillsEditor popup layering — the #11281 regression, unit-testable half.
 *
 * This editor is rendered inside the Crew Member editor's Radix MODAL dialog
 * (its one call site: KiroCrewAgentsPage.tsx). The old implementation
 * portaled its add-skill popup to `document.body` with a bare `createPortal`,
 * OUTSIDE the dialog's layer stack: react-remove-scroll's `pointer-events:
 * none` on the body swallowed clicks on the options and the filter box, and
 * the dialog's FocusScope kept the filter input from ever taking focus (dead
 * keyboard). Rebuilt on Radix Popover, the popup joins the dialog's own
 * focus/dismiss layer stack instead.
 *
 * The interaction itself (open + filter + select INSIDE a real modal dialog,
 * with react-remove-scroll's wheel lock actually engaged) cannot be exercised
 * faithfully under happy-dom — see the header of AgentSelector.dialog.test.tsx
 * for why. What is pinned HERE, mirroring that file, is the structure that
 * makes the fix work, so a regression back to a bare body portal fails fast
 * in unit tests too:
 *
 *  1. the popup renders through Radix's popper layer (not a bare portal), and
 *  2. the component no longer reaches for `createPortal` at all.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  agentPatch: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import AgentSkillsEditor from '../components/AgentSkillsEditor'

const CATALOG = [
  { key: 'babysit', name: 'babysit', description: 'Monitor a PR', source: 'kirocrew' },
  { key: 'widgets', name: 'widgets', description: 'Render HTML', source: 'kirocrew' },
]

function renderEditor() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AgentSkillsEditor agentName="specialist" skills={[]} onChange={() => {}} />
    </QueryClientProvider>,
  )
}

async function openAddMenu() {
  const btn = await screen.findByRole('button', { name: /add skill/i })
  await waitFor(() => expect(btn).toBeEnabled())
  fireEvent.click(btn)
}

beforeEach(() => {
  mockApi.skills.mockReset()
  mockApi.agentPatch.mockReset()
  mockApi.skills.mockResolvedValue(CATALOG)
  mockApi.agentPatch.mockResolvedValue({ ok: true })
})

describe('AgentSkillsEditor popup layering (#11281)', () => {
  it('renders the option list inside a Radix popper layer, not a bare body portal', async () => {
    renderEditor()
    await openAddMenu()

    const listbox = await screen.findByRole('listbox', { name: /available skills/i })
    // Radix Popover mounts its content inside a popper wrapper it owns. That
    // wrapper is what enrols the popup in the surrounding dialog's
    // focus/dismiss layer stack — the property the old `createPortal(...,
    // document.body)` implementation lacked, which is what made options
    // unclickable (body pointer-events: none) and the keyboard dead
    // (FocusScope reclaim) inside the crew editor's modal dialog.
    expect(listbox.closest('[data-radix-popper-content-wrapper]')).not.toBeNull()
  })

  it('keeps the filter input inside the same popper layer as the option list', async () => {
    renderEditor()
    await openAddMenu()

    const input = await screen.findByPlaceholderText('Type to filter…')
    const listbox = await screen.findByRole('listbox', { name: /available skills/i })
    expect(input.closest('[data-radix-popper-content-wrapper]'))
      .toBe(listbox.closest('[data-radix-popper-content-wrapper]'))
  })

  it('does not use a bare createPortal for the popup', () => {
    // Source-level pin, same style as AgentSelector.dialog.test.tsx: a future
    // refactor that swaps the Radix Popover back for `createPortal(...,
    // document.body)` reintroduces the modal-dialog click-through silently —
    // no unit test can catch the interaction itself under happy-dom, so the
    // call is the cheapest reliable tripwire.
    const src = readFileSync(join(__dirname, '..', 'components', 'AgentSkillsEditor.tsx'), 'utf8')
    expect(src).not.toContain('createPortal(')
    expect(src).toContain("from './ui/popover'")
  })
})
