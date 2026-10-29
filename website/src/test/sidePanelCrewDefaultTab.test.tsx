/**
 * A Crew Mode chat's side panel opens on the Crew (Workers) tab until the user
 * picks another one; a normal chat keeps opening on Changes. Rendered through
 * the real SidePanel so the order of its own effects (the pinned-block
 * reconcile writes Changes first) is part of what is tested.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, cleanup, fireEvent, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../pages/chat/command-center/CrewWorkerList', () => ({ default: () => null, CrewWorkersTab: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => false,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'
import type { RootState } from '../store'

const SLOTS = [
  { key: 'crew-root', crew_mode_root: true },
  { key: 'crew-worker', crew_root: 'crew-root' },
  { key: 'plain' },
]

function Harness({ slot }: { slot: string }) {
  const tabsCtl = usePanelTabs(slot)
  return <SidePanel tabsCtl={tabsCtl} slot={slot} onFileSave={async () => {}} onClose={() => {}} />
}

const store = () => {
  const s = createTestStore()
  const dashboard = s.getState().dashboard
  return createTestStore({ dashboard: { ...dashboard, slots: SLOTS } } as unknown as Partial<RootState>)
}

function show(slot: string, st = store()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={st}><Harness slot={slot} /></Provider>
    </QueryClientProvider>,
  )
}

const selected = () => screen.getAllByRole('tab').find(t => t.getAttribute('aria-selected') === 'true')
const chip = (name: string) => screen.getAllByRole('tab').find(t => t.textContent?.includes(name) || t.getAttribute('aria-label') === name)!

describe('side panel default tab in Crew Mode', () => {
  beforeEach(() => { cleanup(); localStorage.clear(); __resetPanelTabs() })

  it('a Crew Mode root opens on the Crew tab', () => {
    show('crew-root')
    expect(selected()?.textContent).toContain('Crew')
  })

  it("a Crew Mode worker's view opens on the Crew tab too", () => {
    show('crew-worker')
    expect(selected()?.textContent).toContain('Crew')
  })

  it('a normal chat still opens on Changes', () => {
    show('plain')
    expect(selected()?.getAttribute('aria-label') ?? selected()?.textContent).toContain('Changes')
    expect(screen.queryAllByRole('tab').some(t => t.textContent?.includes('Crew'))).toBe(false)
  })

  it('a tab the user picks is kept when the panel opens again', () => {
    const view = show('crew-root')
    fireEvent.click(chip('Changes'))
    expect(selected()?.getAttribute('aria-label')).toBe('Changes')
    view.unmount()
    show('crew-root')
    expect(selected()?.getAttribute('aria-label')).toBe('Changes')
  })

  it('switching between a Crew Mode chat and a normal chat picks each default', () => {
    const st = store()
    const a = show('crew-root', st); expect(selected()?.textContent).toContain('Crew'); a.unmount()
    const b = show('plain', st); expect(selected()?.getAttribute('aria-label')).toBe('Changes'); b.unmount()
    const c = show('crew-root', st); expect(selected()?.textContent).toContain('Crew'); c.unmount()
    show('plain', st); expect(selected()?.getAttribute('aria-label')).toBe('Changes')
  })
})
