/**
 * The add-skill popup inside the Crew Member editor's Radix modal dialog
 * (#6358 class). Both symptoms are reachable under happy-dom because neither
 * needs Radix's pointer hit-testing to observe: a dead filter shows up as
 * `document.activeElement` after open, and a dead list as the popup sitting
 * under the dialog's body-level `pointer-events: none` with nothing re-enabling
 * it. Regressing the popup to a `document.body` portal reds most of this file.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({ skills: vi.fn(), agentPatch: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import AgentSkillsEditor from '../components/AgentSkillsEditor'
import { Dialog, DialogBody, DialogContent, DialogTitle } from '../components/ui/dialog'

// Long enough to overflow the popup's capped height, and no filler name contains
// "prep", so the filter narrows to exactly one row.
const CATALOG = [
  { key: 'babysit', name: 'babysit', description: 'Monitor a PR', source: 'kirocrew' },
  { key: 'kiro-user/prepare-pr', name: 'prepare-pr', description: 'Ship a PR', source: 'kiro-user' },
  { key: 'widgets', name: 'widgets', description: 'Render HTML', source: 'kirocrew' },
  ...Array.from({ length: 12 }, (_, i) => ({
    key: `filler-${i}`,
    name: `filler-${String(i).padStart(2, '0')}`,
    description: `Filler skill ${i}`,
    source: 'kirocrew',
  })),
]

/** The editor as the crew editor mounts it — the only arrangement it ships in, and
 *  the only one that can reproduce either symptom. */
function renderInModalDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Dialog open>
        <DialogContent aria-label="Edit agent specialist">
          <DialogTitle>specialist</DialogTitle>
          <DialogBody>
            <AgentSkillsEditor agentName="specialist" skills={[]} onChange={vi.fn()} />
          </DialogBody>
        </DialogContent>
      </Dialog>
    </QueryClientProvider>,
  )
}

async function openAddMenu() {
  const btn = await screen.findByRole('button', { name: /add skill/i })
  await waitFor(() => expect(btn).toBeEnabled())
  fireEvent.click(btn)
  // Radix moves focus a microtask after mount.
  await act(async () => { await new Promise(r => setTimeout(r, 30)) })
}

const listbox = () => screen.getByRole('listbox', { name: /available skills/i })

beforeEach(() => {
  mockApi.skills.mockReset()
  mockApi.agentPatch.mockReset()
  mockApi.skills.mockResolvedValue(CATALOG)
  mockApi.agentPatch.mockResolvedValue({ ok: true })
})

describe('AgentSkillsEditor add-skill popup inside a modal dialog (#6358 class)', () => {
  it('leaves focus on the filter box instead of letting the dialog reclaim it', async () => {
    renderInModalDialog()
    await openAddMenu()

    expect(document.activeElement).toBe(screen.getByPlaceholderText('Type to filter…'))
  })

  it('narrows the list from keys typed at wherever focus actually landed', async () => {
    renderInModalDialog()
    await openAddMenu()

    expect(screen.getAllByRole('option')).toHaveLength(CATALOG.length)
    // Typed at activeElement, not at the input by reference: addressing the input
    // directly passes even when focus is elsewhere, which is the broken state.
    const focused = document.activeElement as HTMLInputElement
    fireEvent.change(focused, { target: { value: 'prep' } })

    expect(focused.value).toBe('prep')
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(1))
    expect(screen.getByRole('option', { name: /prepare-pr/i })).toBeInTheDocument()
  })

  it('re-enables pointer events over the list beneath the dialog\u2019s body-level cut', async () => {
    renderInModalDialog()
    await openAddMenu()

    expect(document.body.style.pointerEvents).toBe('none')

    let reEnabled = false
    for (let el: HTMLElement | null = listbox(); el && el !== document.body; el = el.parentElement) {
      if (el.style.pointerEvents === 'auto') { reEnabled = true; break }
    }
    expect(reEnabled).toBe(true)
    expect(listbox().parentElement).not.toBe(document.body)
    expect(listbox().closest('[data-radix-popper-content-wrapper]')).not.toBeNull()
  })

  it('makes the option list its own capped scroll container', async () => {
    renderInModalDialog()
    await openAddMenu()

    const list = listbox()
    expect(list.className).toContain('overflow-y-auto')
    expect(list.className).toContain('min-h-0')

    const popup = list.closest('[role="dialog"]') as HTMLElement | null
    expect(popup).not.toBeNull()
    expect(popup!.className).toMatch(/max-h-\[/)
  })

  it('keeps the filter input in the same popper layer as the listbox', async () => {
    renderInModalDialog()
    await openAddMenu()

    const inputLayer = screen.getByPlaceholderText('Type to filter…').closest('[data-radix-popper-content-wrapper]')
    const listLayer = listbox().closest('[data-radix-popper-content-wrapper]')
    // Both asserted non-null: the equality alone passes on `null === null`, which
    // is the pre-fix state exactly.
    expect(inputLayer).not.toBeNull()
    expect(listLayer).not.toBeNull()
    expect(inputLayer).toBe(listLayer)
  })

  it('shows the empty state when the filter matches nothing', async () => {
    renderInModalDialog()
    await openAddMenu()

    fireEvent.change(screen.getByPlaceholderText('Type to filter…'), {
      target: { value: 'zzz-no-such-skill' },
    })

    await waitFor(() => expect(screen.getByText(/No matching skills/i)).toBeInTheDocument())
    expect(screen.queryAllByRole('option')).toHaveLength(0)
  })

  it('clears the filter between opens rather than leaking it into the next one', async () => {
    renderInModalDialog()
    await openAddMenu()
    fireEvent.change(screen.getByPlaceholderText('Type to filter…'), { target: { value: 'prep' } })
    await waitFor(() => expect(screen.getAllByRole('option')).toHaveLength(1))

    fireEvent.keyDown(listbox(), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('listbox', { name: /available skills/i })).not.toBeInTheDocument())
    await openAddMenu()

    expect((screen.getByPlaceholderText('Type to filter…') as HTMLInputElement).value).toBe('')
    expect(screen.getAllByRole('option')).toHaveLength(CATALOG.length)
  })
})

describe('AgentSkillsEditor popup source pin', () => {
  it('does not use a bare createPortal for the popup', () => {
    const src = readFileSync(join(__dirname, '..', 'components', 'AgentSkillsEditor.tsx'), 'utf8')
    expect(src).not.toContain('createPortal(')
    expect(src).toContain("from './ui/popover'")
  })
})
