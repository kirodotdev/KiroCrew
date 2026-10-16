/**
 * The crew editor's presentational pieces in KiroCrewAgentsPagePanes.tsx,
 * mounted from their props: the page owns every value they show and every
 * setter they call, so each case asserts what reaches the page.
 */
import type React from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../test/helpers'
import { i18nT } from '../i18n/t'
import { Dialog, DialogContent } from '../components/ui/dialog'
import type { CrewPaneKey } from '../components/crew/crewEditorSections'
import { CrewEditorHeader, CrewModelPane, CrewPlacePane, CrewRoutingPane } from './KiroCrewAgentsPagePanes'

/* SimpleSelect is stubbed as in WorkspaceModal.test.tsx: these cases are about
   what the panes do with a pick and the select's action row, not about driving
   Radix Select. */
vi.mock('../components/SimpleSelect', () => ({
  default: ({ value, action, 'aria-label': ariaLabel }: {
    value: string
    action?: { label: string; onSelect: () => void }
    'aria-label'?: string
  }) => (
    <div>
      <span role="status" aria-label={ariaLabel}>{value}</span>
      {action && <button type="button" onClick={action.onSelect}>{action.label}</button>}
    </div>
  ),
}))

afterEach(cleanup)

/** Plain render plus the store and query client the avatar reads: the shared
 *  `renderWithProviders` ThemeProvider does an async mount update these
 *  presentational panes do not need, and it trips the act-warning gate.
 *  Mirrors RemoteCrewPanel.turnUrlScrollEdges.test.tsx. */
const renderWithProviders = (ui: React.ReactElement) => render(
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <Provider store={createTestStore()}>{ui}</Provider>
  </QueryClientProvider>,
)

const place = (over: Partial<Parameters<typeof CrewPlacePane>[0]> = {}) => ({
  editing: 'mate',
  workspaceOptions: ['default', 'member-mate'],
  workspace: 'member-mate',
  setWorkspace: vi.fn(),
  setWsModalOpen: vi.fn(),
  memoryStore: 'mate-store',
  kirocrewCfg: { memory_stores: { 'mate-store': { memory_version: 2, owner_member: 'mate' } } },
  sheetBusy: false,
  dirtyPanes: new Set<CrewPaneKey>(),
  schedDraft: false,
  navigate: vi.fn(),
  collidingCrews: [],
  ...over,
})

describe('CrewPlacePane', () => {
  it('opens the nested create-workspace dialog from the workspace action', () => {
    const props = place()
    renderWithProviders(<CrewPlacePane {...props} />)
    fireEvent.click(screen.getByRole('button', { name: i18nT('pages.kiroCrewAgentsPage.new_workspace_action') }))
    expect(props.setWsModalOpen).toHaveBeenCalledWith(true)
  })

  it('manages a private memory store at its own store id', () => {
    const props = place()
    renderWithProviders(<CrewPlacePane {...props} />)
    fireEvent.click(screen.getByRole('button', { name: i18nT('pages.kiroCrewAgentsPage.manage_private_memory') }))
    expect(props.navigate).toHaveBeenCalledWith('/settings/overview?view=memory&store=mate-store')
  })

  it("names the default crew's workspace neighbours, and only the default's", () => {
    const warning = i18nT('pages.kiroCrewAgentsPage.also_used_by_these_crews', { crews: 'ops, docs' })
    renderWithProviders(<CrewPlacePane {...place({ editing: 'default', memoryStore: 'default', collidingCrews: ['ops', 'docs'] })} />)
    expect(screen.getByText(warning)).toBeInTheDocument()
    cleanup()
    renderWithProviders(<CrewPlacePane {...place({ collidingCrews: ['ops', 'docs'] })} />)
    expect(screen.queryByText(warning)).toBeNull()
  })
})

describe('CrewModelPane', () => {
  const model = (over: Partial<Parameters<typeof CrewModelPane>[0]> = {}) => ({
    modelOptions: ['auto', 'model-a'],
    pinModelsQuery: { isError: false, isDegraded: false, isFetching: false, refetch: vi.fn() },
    pinHarness: 'Kiro CLI',
    editModel: 'model-a',
    setEditModel: vi.fn(),
    editEffort: 'high',
    setEditEffort: vi.fn(),
    effortModel: 'model-a',
    effortCapable: false,
    resolved: undefined,
    resolvedError: null,
    ...over,
  })

  it('says why a stored effort pin cannot apply on the picked model', () => {
    renderWithProviders(<CrewModelPane {...model()} />)
    expect(screen.getByText(i18nT('pages.kiroCrewAgentsPage.effort_ignored_on_this_model', { model: 'model-a' }))).toBeInTheDocument()
  })

  it('reads out the resolved model and effort', () => {
    renderWithProviders(<CrewModelPane {...model({
      effortCapable: true,
      resolved: { model: 'model-a', pinned: true, reasoning_effort: '', effort_pinned: false },
    })} />)
    expect(screen.getByText(i18nT('pages.kiroCrewAgentsPage.resolves_to', { model: 'model-a' }))).toBeInTheDocument()
    expect(screen.getByText(i18nT('pages.kiroCrewAgentsPage.effort_resolves_to', { effort: i18nT('lib.effort.default') }))).toBeInTheDocument()
    expect(screen.getByText(i18nT('pages.kiroCrewAgentsPage.no_effort_pin_the_model_decides'), { exact: false })).toBeInTheDocument()
  })
})

describe('CrewRoutingPane', () => {
  it('opens the avatar builder from its button', () => {
    const openAvatarBuilder = vi.fn()
    renderWithProviders(<CrewRoutingPane editing="mate" triggers="" setTriggers={vi.fn()} sessionColor="" setSessionColor={vi.fn()} editAvatar={null} openAvatarBuilder={openAvatarBuilder} />)
    fireEvent.click(screen.getByTestId('open-avatar-builder'))
    expect(openAvatarBuilder).toHaveBeenCalledTimes(1)
  })
})

describe('CrewEditorHeader', () => {
  const header = (over: Partial<Parameters<typeof CrewEditorHeader>[0]> = {}) => ({
    editing: 'mate',
    editingAgent: undefined,
    displayName: 'Mate',
    editAvatar: { kind: 'image' as const, v: 3 },
    sheetBusy: false,
    openAvatarBuilder: vi.fn(),
    requestChat: vi.fn(),
    setError: vi.fn(),
    ...over,
  })
  const mount = (props: ReturnType<typeof header>) => renderWithProviders(
    <Dialog open>
      <DialogContent aria-label="editor">
        <CrewEditorHeader {...props} />
      </DialogContent>
    </Dialog>,
  )

  it("reports a saved picture that does not load as the picture's failure", () => {
    const props = header()
    mount(props)
    const img = screen.getByTestId('header-avatar-button').querySelector('img')!
    fireEvent.error(img)
    expect(props.setError).toHaveBeenCalledWith(i18nT('components.avatarBuilder.image_load_failed'))
  })

  it('routes the chat action through the page', () => {
    const props = header()
    mount(props)
    fireEvent.click(screen.getByRole('button', { name: i18nT('memoryV2.chat_member') }))
    expect(props.requestChat).toHaveBeenCalledTimes(1)
  })
})
