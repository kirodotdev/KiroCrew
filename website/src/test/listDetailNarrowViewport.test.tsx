/**
 * Narrow-viewport list+detail drill-down.
 *
 * These shells put a fixed 240-288px list column beside the detail pane. That
 * is fine on a desktop and unusable on a phone: at 390px the detail is left
 * 40-60px of content width, which renders one or two CJK characters per line
 * and overlaps the Edit/Delete pair in the detail header. While narrow the
 * shell shows exactly ONE pane and drills down instead.
 *
 * The load-bearing case is `auto-selection does not open the detail`. Every one
 * of these shells auto-selects its first row so the desktop detail is never
 * blank, so a rule derived from "is something selected" would open the detail
 * before the user picked anything — the list would be unreachable and Back a
 * no-op, because the auto-select effect re-fires on the next render. A
 * selection-derived implementation passes every other test in this file.
 *
 * happy-dom does not do layout, so the geometry cases pin the classes that
 * select the behavior (the convention NotificationsPage.mobileScroll uses).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

let mobile = false
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

const mockApi = vi.hoisted(() => ({
  steeringFiles: vi.fn(),
  steeringFile: vi.fn(),
  createSteering: vi.fn(),
  updateSteering: vi.fn(),
  deleteSteering: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

import SteeringTab from '../pages/overview/SteeringTab'
import { useListDetailView } from '../hooks/useListDetailView'

const FILES = {
  files: [
    { key: 'user/personal.md', name: 'personal.md', rel: 'personal.md', source: 'user', path: '~/.kiro/steering/personal.md', size: 12, description: 'Personal', inclusion: 'always', inclusion_declared: '', file_match_pattern: '' },
    { key: 'workspace/api.md', name: 'api.md', rel: 'api.md', source: 'workspace', path: '~/proj/.kiro/steering/api.md', size: 20, description: 'API standards', inclusion: 'always', inclusion_declared: '', file_match_pattern: '' },
  ],
  roots: [{ source: 'user', path: '~/.kiro/steering', exists: true }],
  project: '~/proj',
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(<QueryClientProvider client={qc}><SteeringTab /></QueryClientProvider>)
}

/** The list pane, addressed by the role it already carries. */
function listPane() {
  return screen.queryByRole('listbox', { name: 'Steering files' })
}

beforeEach(() => {
  mobile = false
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.steeringFiles.mockResolvedValue(FILES)
  mockApi.steeringFile.mockResolvedValue({ key: 'user/personal.md', content: '# Personal\nbody', path: '~/.kiro/steering/personal.md', source: 'user' })
  mockApi.updateSteering.mockResolvedValue({ ok: true })
  mockApi.deleteSteering.mockResolvedValue({ ok: true })
})

describe('useListDetailView', () => {
  it('shows both panes on a desktop, and keeps showing both after a drill-down', () => {
    mobile = false
    const { result } = renderHook(() => useListDetailView())
    expect(result.current.showList).toBe(true)
    expect(result.current.showDetail).toBe(true)
    act(() => result.current.openDetail())
    expect(result.current.showList).toBe(true)
    expect(result.current.showDetail).toBe(true)
  })

  it('shows the list first while narrow, then swaps to the detail and back', () => {
    mobile = true
    const { result } = renderHook(() => useListDetailView())
    // Precondition: narrow and nothing drilled into.
    expect(result.current.isMobile).toBe(true)
    expect([result.current.showList, result.current.showDetail]).toEqual([true, false])
    act(() => result.current.openDetail())
    expect([result.current.showList, result.current.showDetail]).toEqual([false, true])
    act(() => result.current.closeDetail())
    expect([result.current.showList, result.current.showDetail]).toEqual([true, false])
  })
})

describe('SteeringTab at desktop width', () => {
  it('renders list and detail side by side, list at its fixed width', async () => {
    mobile = false
    renderTab()
    await waitFor(() => expect(screen.getByText('personal.md')).toBeInTheDocument())
    const list = listPane()
    expect(list).not.toBeNull()
    expect(list!.className).toContain('w-[240px]')
    // Detail is present without any tap: the auto-select seeds it.
    expect(await screen.findByTestId('md')).toBeInTheDocument()
    // No Back control on a desktop — both panes are already visible.
    expect(screen.queryByRole('button', { name: 'Steering files' })).toBeNull()
  })
})

describe('SteeringTab at phone width', () => {
  it('auto-selection does not open the detail: the list is what the user lands on', async () => {
    mobile = true
    renderTab()
    await waitFor(() => expect(screen.getByText('personal.md')).toBeInTheDocument())
    // Precondition — auto-select HAS run, so a selection exists.
    expect(screen.getByLabelText('Select personal.md')).toHaveAttribute('aria-current', 'true')
    // ...and yet the detail is not on screen, and the list is full width.
    expect(screen.queryByTestId('md')).toBeNull()
    expect(listPane()!.className).toContain('w-full')
    expect(listPane()!.className).not.toContain('w-[240px]')
  })

  it('tapping a row swaps the list out for the detail', async () => {
    mobile = true
    renderTab()
    await waitFor(() => expect(screen.getByText('api.md')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Select api.md'))
    expect(await screen.findByTestId('md')).toBeInTheDocument()
    // One pane at a time: the list is gone, not merely narrower.
    expect(listPane()).toBeNull()
  })

  it('Back returns to the list and the auto-select effect does not bounce it shut', async () => {
    mobile = true
    renderTab()
    await waitFor(() => expect(screen.getByText('personal.md')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Select personal.md'))
    await screen.findByTestId('md')

    const back = screen.getByRole('button', { name: 'Steering files' })
    fireEvent.click(back)

    await waitFor(() => expect(listPane()).not.toBeNull())
    expect(screen.queryByTestId('md')).toBeNull()
    // The selection survives the trip back (returning is not deselecting), which
    // is exactly why the open/closed flag cannot be derived from it.
    expect(screen.getByLabelText('Select personal.md')).toHaveAttribute('aria-current', 'true')
  })

  it('drops the absolute path from the detail header so the actions keep the row', async () => {
    mobile = true
    renderTab()
    await waitFor(() => expect(screen.getByText('personal.md')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Select personal.md'))
    await screen.findByTestId('md')
    expect(screen.queryByText('~/.kiro/steering/personal.md')).toBeNull()
    // Both actions are still reachable — the row wraps instead of overlapping.
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
  })

  it('sizes the shell in svh so mobile browser chrome cannot cover the pane bottom', async () => {
    mobile = true
    renderTab()
    await waitFor(() => expect(screen.getByText('personal.md')).toBeInTheDocument())
    // While narrow the list IS the only pane, so the shell's own height decides
    // whether its bottom edge is reachable. `vh` resolves against the large
    // viewport (chrome retracted) and would run under the address bar.
    const shell = listPane()!.parentElement
    expect(shell!.className).toContain('supports-[height:100svh]:h-[calc(100svh-260px)]')
    // The vh declaration stays as the no-svh fallback rather than being replaced.
    expect(shell!.className).toContain('h-[calc(100vh-260px)]')
  })
})

describe('SteeringTab across a viewport change', () => {
  it('narrowing keeps the detail the user opened on a desktop, rather than resetting to the list', async () => {
    // Material's resize rule: expanded -> compact keeps the detail visible and
    // hides the list, because that is the pane the user was reading. This falls
    // out of routing the drill-down through the shared row-select handler, which
    // a desktop click also goes through.
    mobile = false
    const { rerender } = renderTab()
    await waitFor(() => expect(screen.getByText('api.md')).toBeInTheDocument())
    fireEvent.click(screen.getByLabelText('Select api.md'))
    await screen.findByTestId('md')

    mobile = true
    rerender(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })}><SteeringTab /></QueryClientProvider>)

    // The detail survived the narrowing; the list stepped aside.
    expect(await screen.findByTestId('md')).toBeInTheDocument()
    expect(listPane()).toBeNull()
  })

  it('a phone user who never opened a detail still lands on the list after a widen and re-narrow', async () => {
    mobile = true
    const { rerender } = renderTab()
    await waitFor(() => expect(screen.getByText('personal.md')).toBeInTheDocument())
    expect(screen.queryByTestId('md')).toBeNull()

    const widen = (m: boolean) => {
      mobile = m
      rerender(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })}><SteeringTab /></QueryClientProvider>)
    }
    widen(false)
    widen(true)

    // Auto-selection still has not been mistaken for intent.
    expect(listPane()).not.toBeNull()
    expect(screen.queryByTestId('md')).toBeNull()
  })
})

/**
 * Clearing the selection must also leave the detail pane.
 *
 * With exclusive panes, `selectedAgent = null` while `detailOpen` stays true
 * renders the "select an agent" placeholder — a branch that carries no Back
 * control, since Back lives in the `selectedAgent` truthy branch — with the
 * roster and its filter inside the hidden list pane. That combination leaves a
 * phone user with no in-page way back, which is what deleting the agent you were
 * viewing used to do.
 *
 * Asserted over the source because the invariant is about EVERY path that nulls
 * the selection, including ones added later: a rendered test would only cover
 * the delete path that exists today.
 */
describe('AgentsPage selection clearing', () => {
  it('pairs every setSelectedAgent(null) with closeDetail()', async () => {
    const src = (await import('../pages/AgentsPage.tsx?raw')).default as string
    const clears = [...src.matchAll(/setSelectedAgent\(null\)/g)]
    expect(clears.length, 'expected at least one path that clears the selection')
      .toBeGreaterThan(0)
    for (const match of clears) {
      // The pane exit has to be in the same statement/handler, not merely
      // somewhere in the file.
      const window = src.slice(match.index!, match.index! + 200)
      expect(window, `setSelectedAgent(null) at ${match.index} without closeDetail()`)
        .toMatch(/closeDetail\(\)/)
    }
  })

  it('keeps Back out of the placeholder branch, so the exit must be the handler', async () => {
    const src = (await import('../pages/AgentsPage.tsx?raw')).default as string
    // Documents WHY the fix belongs in the mutation handler. If a future change
    // renders Back in the placeholder too, this can be revisited deliberately
    // rather than by accident.
    const placeholder = src.indexOf('select_an_agent_to_view_details')
    expect(placeholder, 'placeholder branch not found').toBeGreaterThan(-1)
    const branch = src.slice(placeholder - 400, placeholder + 200)
    expect(branch).not.toMatch(/ListDetailBack/)
  })
})

/**
 * Back sits on its own row, not in the header's action row.
 *
 * `website/AUTOSDE.yaml`'s `max-two-buttons-per-row` is a blocking rule: a row
 * already carrying three controls is tolerated, but a compliant row may not
 * GROW into one. Both detail headers here already spend their two slots —
 * Cancel+Save while editing, Edit+Delete while reading — so dropping Back into
 * that flex row made three controls render together on a phone. Moving it to a
 * separate row is the remedy the rule allows.
 *
 * Pinned on the source because the violation is structural (which row the
 * control is a child of), which is exactly what a source assertion can see and
 * what a jsdom render cannot measure.
 */
describe('detail header action budget', () => {
  const SHELLS = [
    '../pages/overview/SkillsTab.tsx',
    '../pages/overview/SteeringTab.tsx',
  ]

  it('never puts Back inside the justify-between action row', async () => {
    for (const mod of SHELLS) {
      const src = (await import(`${mod}?raw`)).default as string
      const lines = src.split('\n')
      lines.forEach((line, i) => {
        if (!line.includes('<ListDetailBack')) return
        // The row it belongs to is the nearest enclosing element opened above.
        const opener = lines.slice(Math.max(0, i - 3), i).join('\n')
        expect(
          opener,
          `${mod}:${i + 1} — Back is a child of the action row; give it its own row`,
        ).not.toMatch(/justify-between/)
        expect(
          opener,
          `${mod}:${i + 1} — Back should sit in its own px-4 pt-2.5 row`,
        ).toMatch(/px-4 pt-2\.5/)
      })
    }
  })

  // Deliberately NOT asserted here: "each action group renders at most two
  // controls". SteeringTab's group holds four `<Btn>` in SOURCE because
  // Cancel+Save and Edit+Delete are two conditional branches inside one
  // wrapper, and only one branch ever renders. A source count cannot tell those
  // apart, so such an assertion fails on compliant code. Counting what actually
  // renders needs the header mounted in both editing states, which belongs with
  // the rendered SteeringTab cases above rather than here.
})
