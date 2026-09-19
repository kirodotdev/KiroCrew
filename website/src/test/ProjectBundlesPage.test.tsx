import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'

import { api } from '../api/client'
import ProjectBundlesPage from '../pages/ProjectBundlesPage'
import { consumeChatHandoff } from '../utils/errorReport'
import { renderWithProviders } from './helpers'

vi.mock('../api/client', () => ({
  api: {
    projectBundles: vi.fn(),
    createProjectBundle: vi.fn(),
    addProjectBundle: vi.fn(),
    syncProjectBundle: vi.fn(),
    projectBundleReviewPreview: vi.fn(),
    reviewProjectBundle: vi.fn(),
    removeProjectBundle: vi.fn(),
    createChatSlot: vi.fn(),
    chatSlotProject: vi.fn(),
    setSlotColor: vi.fn(),
    setSlotColorHex: vi.fn(),
    deleteChatSlot: vi.fn(),
  },
}))

const localProject = {
  id: '018f4f4a-760f-7a8b-a5d4-5a7e0f130d4e',
  name: 'Payments Platform',
  description: 'Payments services and operational context.',
  workspace_source: 'payments-api',
  sources: [{
    id: 'payments-api',
    type: 'repo',
    url: 'https://github.com/acme/payments-api',
    default_branch: 'main',
  }],
  registrations: [{ origin: 'local' as const, path: '/work/payments', syncable: false }],
  health: { status: 'healthy' as const, code: 'project_healthy' },
  sessions: [{
    key: 'payments-chat',
    title: 'Investigate refunds',
    messages: 4,
    running: false,
    live: true,
  }],
}

const reviewPreview = {
  digest: 'sha256:0f1e2d3c',
  files: [
    {
      path: '.kiro/settings/mcp.json',
      status: 'changed' as const,
      content: '{\n  "mcpServers": {\n    "atlassian": { "command": "npx", "args": ["-y", "mcp-atlassian"] }\n  }\n}\n',
    },
    {
      path: '.kiro/agents/payments.md',
      status: 'added' as const,
      content: '# payments agent\nRuns the refund reconciliation.\n',
    },
    { path: '.kiro/hooks/legacy.json', status: 'removed' as const },
  ],
}

const managedProject = {
  ...localProject,
  id: '018f4f4a-760f-7a8b-a5d4-5a7e0f130d5f',
  name: 'Shared Payments',
  registrations: [{
    origin: 'managed_git' as const,
    path: '/data/projects/shared-payments',
    syncable: true,
  }],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.projectBundles).mockResolvedValue({ projects: [localProject] })
  vi.mocked(api.createProjectBundle).mockResolvedValue(localProject)
  vi.mocked(api.addProjectBundle).mockResolvedValue(localProject)
  vi.mocked(api.syncProjectBundle).mockResolvedValue(managedProject)
  vi.mocked(api.projectBundleReviewPreview).mockResolvedValue(reviewPreview)
  vi.mocked(api.reviewProjectBundle).mockResolvedValue(localProject)
  vi.mocked(api.removeProjectBundle).mockResolvedValue({ ok: true, id: localProject.id })
  vi.mocked(api.createChatSlot).mockResolvedValue({
    key: 'new-project-chat',
    title: 'New Session',
    messages: 0,
    running: false,
    project: '/work/payments',
    project_id: localProject.id,
  })
})

describe('Projects portal (thin Project)', () => {
  it('opens a Project from a single-column list into a focused detail view', async () => {
    renderWithProviders(<ProjectBundlesPage />)

    const project = await screen.findByRole('button', { name: /Open project Payments Platform/ })
    expect(screen.queryByRole('button', { name: 'New chat' })).not.toBeInTheDocument()

    fireEvent.click(project)

    expect(await screen.findByRole('heading', { name: 'Payments Platform' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Back to projects' })).toBeInTheDocument()
    // Creating a slot has one name across the dashboard: the sidebar's
    // "New chat", here too -- never a second term for the same action.
    expect(screen.getByRole('button', { name: 'New chat' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New session/ })).not.toBeInTheDocument()
    expect(screen.getByText('Payments services and operational context.')).toBeInTheDocument()
    expect(screen.getAllByText('payments-api')).toHaveLength(2)
    expect(screen.getByText('https://github.com/acme/payments-api')).toBeInTheDocument()
    expect(screen.getByText('/work/payments')).toBeInTheDocument()
    expect(screen.getByText('Healthy')).toBeInTheDocument()
    // The thin Project installs nothing: no activation / capabilities control.
    expect(screen.queryByRole('button', { name: /Trust and activate/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Deactivate/ })).not.toBeInTheDocument()
    expect(screen.getByText('Investigate refunds')).toBeInTheDocument()
    // A session row says what it counts, not a bare number.
    expect(screen.getByText('4 messages')).toBeInTheDocument()
  })

  it('pluralizes a single message in a session row', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({
      projects: [{ ...localProject, sessions: [{ ...localProject.sessions[0], messages: 1 }] }],
    })
    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Payments Platform/ }))

    expect(screen.getByText('1 message')).toBeInTheDocument()
  })

  it('shows no card for context the manifest does not carry', async () => {
    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Payments Platform/ }))

    // The manifest schema has no `mcp` and no `memory` key, so the detail lists
    // nothing under a "declared" heading: a card that only says "not active
    // yet" would read as a second status.
    expect(screen.getByText('Repositories')).toBeInTheDocument()
    expect(screen.getByText('Local copy')).toBeInTheDocument()
    expect(screen.queryByText('Declared context')).not.toBeInTheDocument()
    expect(screen.queryByText(/MCP servers/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Project memory/)).not.toBeInTheDocument()
    expect(screen.queryByText('Not active yet; nothing to do here.')).not.toBeInTheDocument()
  })

  it('renders only repository sources in detail when provider data contains objects', async () => {
    const projectWithExtensionSource = {
      ...localProject,
      sources: [
        ...localProject.sources,
        { id: 'pay-board', type: 'jira', url: { board: 'PAY' } },
      ],
    }
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [projectWithExtensionSource] })
    renderWithProviders(<ProjectBundlesPage />)

    fireEvent.click(await screen.findByRole('button', { name: /Open project Payments Platform/ }))

    expect(screen.getByText('https://github.com/acme/payments-api')).toBeInTheDocument()
    expect(screen.queryByText('pay-board')).not.toBeInTheDocument()
  })

  it('starts a session with the Project identity in the create request', async () => {
    renderWithProviders(<ProjectBundlesPage />)

    fireEvent.click(await screen.findByRole('button', { name: /Open project Payments Platform/ }))
    fireEvent.click(screen.getByRole('button', { name: 'New chat' }))

    await waitFor(() => {
      expect(api.createChatSlot).toHaveBeenCalledWith(
        undefined,
        undefined,
        undefined,
        undefined,
        // createSlot resolves the configured default memory mode before the
        // request; this test pins the Project identity, not that default.
        expect.any(String),
        undefined,
        undefined,
        undefined,
        undefined,
        // adopt_remote_slot — unset here.
        undefined,
        localProject.id,
      )
    })
  })

  it('explains how to populate an empty registry', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [] })

    renderWithProviders(<ProjectBundlesPage />)

    expect(await screen.findByText('No projects yet')).toBeInTheDocument()
    expect(screen.getByText('Create a local Project or add one from a folder or Git URL.')).toBeInTheDocument()
  })

  it('creates a local bundle and refreshes the portal list', async () => {
    vi.mocked(api.projectBundles)
      .mockResolvedValueOnce({ projects: [] })
      .mockResolvedValue({ projects: [localProject] })

    renderWithProviders(<ProjectBundlesPage />)
    await screen.findByText('No projects yet')
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }))
    fireEvent.change(screen.getByLabelText('Project name'), {
      target: { value: 'Payments Platform' },
    })
    const projectFolder = screen.getByLabelText('Project folder')
    fireEvent.change(projectFolder, {
      target: { value: '/work/payments' },
    })
    fireEvent.click(within(projectFolder.closest('form')!).getByRole('button', { name: 'Create project' }))

    expect(await screen.findByRole('button', { name: /Open project Payments Platform/ })).toBeInTheDocument()
    // The form carries no id field: the registry assigns the Project id.
    expect(api.createProjectBundle).toHaveBeenCalledWith('Payments Platform', '/work/payments')
    expect(screen.queryByLabelText(/Project ID/i)).not.toBeInTheDocument()
  })

  it('adds an existing folder or Git URL and refreshes the portal list', async () => {
    vi.mocked(api.projectBundles)
      .mockResolvedValueOnce({ projects: [] })
      .mockResolvedValue({ projects: [localProject] })

    renderWithProviders(<ProjectBundlesPage />)
    await screen.findByText('No projects yet')
    // "Add existing project" is distinguishable from "Create project" at a
    // glance; the helper text keeps the folder-or-URL detail.
    expect(screen.queryByRole('button', { name: 'Add project' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Add existing project' }))
    expect(screen.getByText('Register an existing Project folder, or clone one from a Git URL.')).toBeInTheDocument()
    const projectSource = screen.getByLabelText('Folder or Git URL')
    fireEvent.change(projectSource, {
      target: { value: '/work/payments' },
    })
    fireEvent.click(within(projectSource.closest('form')!).getByRole('button', { name: 'Add existing project' }))

    expect(await screen.findByRole('button', { name: /Open project Payments Platform/ })).toBeInTheDocument()
    // Only the source goes over the wire; the server mints the id.
    expect(api.addProjectBundle).toHaveBeenCalledWith('/work/payments')
  })

  it('names the sandbox requirement when a host that denies user namespaces refuses an add', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [] })
    vi.mocked(api.addProjectBundle).mockRejectedValue(Object.assign(new Error('sandbox unavailable'), {
      status: 503,
      body: JSON.stringify({ error: 'sandbox unavailable', code: 'project_sandbox_unavailable' }),
    }))

    renderWithProviders(<ProjectBundlesPage />)
    await screen.findByText('No projects yet')
    fireEvent.click(screen.getByRole('button', { name: 'Add existing project' }))
    const projectSource = screen.getByLabelText('Folder or Git URL')
    fireEvent.change(projectSource, { target: { value: 'https://github.com/acme/payments' } })
    fireEvent.click(within(projectSource.closest('form')!).getByRole('button', { name: 'Add existing project' }))

    // The notice repeats the requirement rather than echoing the server's
    // short reason, and offers the agent hand-off for a host-level fix.
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Project Git operations run in the Kiro Crew sandbox.')
    expect(alert).toHaveTextContent('user namespaces are disabled')
    expect(within(alert).getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
  })

  it('pulls updates for managed Git projects and confirms completion', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [managedProject] })

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))
    // One label for the one action, wherever it stands: no "Sync project",
    // no "Retry sync".
    expect(screen.queryByRole('button', { name: /Sync project|Retry sync/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Pull updates' }))

    // The success line uses the button's own verb; nothing says "synced".
    const pulled = await screen.findByText('Updates pulled.')
    expect(pulled).toHaveAttribute('role', 'status')
    expect(screen.queryByText(/synced/i)).not.toBeInTheDocument()
    expect(screen.queryByTestId('project-sync-partial-local')).not.toBeInTheDocument()
  })

  it('names the sources a pull could not fetch instead of reading as a plain success', async () => {
    // A partial pull is still a 200: the bundle and every source that could
    // follow fast-forwarded, and the response lists the declared ids that did
    // not, with the Project's health already sources_unavailable in it.
    const partial = {
      ...managedProject,
      sources: [
        { ...managedProject.sources[0], status: 'healthy' },
        { id: 'payments-infra-1a2b3c4d', type: 'repo', url: 'https://github.com/acme/payments-infra', default_branch: 'main', status: 'unavailable' },
      ],
      health: { status: 'sources_unavailable' as const, code: 'project_sources_unavailable', unavailable_sources: ['payments-infra-1a2b3c4d'] },
    }
    vi.mocked(api.projectBundles)
      .mockResolvedValueOnce({ projects: [managedProject] })
      .mockResolvedValue({ projects: [partial] })
    vi.mocked(api.syncProjectBundle).mockResolvedValue({ ...partial, unavailable_sources: ['payments-infra-1a2b3c4d'] })

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Pull updates' }))

    // Anchored under the control that was clicked (the Local copy card),
    // through the shared error surface with the hand-off: the id is what to
    // fix in project.yaml, so it is an error the owner acts on.
    const outcome = await screen.findByTestId('project-sync-outcome-local')
    const notice = within(outcome).getByTestId('project-sync-partial-local')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('Updates pulled; 1 source could not be fetched: payments-infra-1a2b3c4d')
    expect(within(notice).getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
    // Never the plain success line beside it.
    expect(within(outcome).queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByText('Updates pulled.')).not.toBeInTheDocument()
    // The refetched payload flips the page into the sources-unavailable state.
    expect(await screen.findByText(/Edit \/data\/projects\/shared-payments\/project\.yaml/)).toBeInTheDocument()
  })

  it('pluralizes the sources a pull could not fetch and lists every id', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [managedProject] })
    vi.mocked(api.syncProjectBundle).mockResolvedValue({ ...managedProject, unavailable_sources: ['payments-infra-1a2b3c4d', 'web'] })

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Pull updates' }))

    const notice = await screen.findByTestId('project-sync-partial-local')
    expect(notice).toHaveTextContent('Updates pulled; 2 sources could not be fetched: payments-infra-1a2b3c4d, web')
  })

  it('explains a project.yaml that does not parse with the parser text and where the file is', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [managedProject] })
    vi.mocked(api.syncProjectBundle).mockRejectedValue(Object.assign(new Error('HTTP 409'), {
      status: 409,
      body: JSON.stringify({ error: 'project_manifest_invalid', code: 'project_manifest_invalid', project_id: managedProject.id, detail: 'sources[0].url: expected a string' }),
    }))

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Pull updates' }))

    const alert = within(await screen.findByTestId('project-sync-outcome-local')).getByRole('alert')
    // The lead says what could not be read; the parser's message is the
    // reason, verbatim; the remedy names the file by its full path and the
    // page's one verb.
    expect(alert).toHaveTextContent('project.yaml could not be read.')
    expect(alert).toHaveTextContent('sources[0].url: expected a string')
    expect(alert).toHaveTextContent(`Fix ${managedProject.registrations[0].path}/project.yaml, then pull updates again.`)
    expect(alert).not.toHaveTextContent('HTTP 409')
    expect(alert).not.toHaveTextContent('project_manifest_invalid')
    expect(within(alert).getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
    expect(screen.queryByText('Updates pulled.')).not.toBeInTheDocument()
  })

  it('offers recovery for an unavailable Git Project and explains why sessions are blocked', async () => {
    const unavailable = {
      ...managedProject,
      health: { status: 'unavailable' as const, code: 'project_manifest_unavailable' },
    }
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [unavailable] })

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))

    expect(screen.getByRole('alert')).toHaveTextContent('Project files are unavailable')
    expect(screen.getByRole('alert')).toHaveTextContent('Restore the folder or pull updates.')
    // The recovery control carries the same label as every other pull (the
    // banner's, and the Local copy card's further down).
    expect(screen.getAllByRole('button', { name: 'Pull updates' })).toHaveLength(2)
    expect(screen.queryByRole('button', { name: 'Retry sync' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
  })

  const reviewStale = {
    ...managedProject,
    health: {
      status: 'review_stale' as const,
      code: 'project_review_stale',
      stale_files: ['.kiro/settings/mcp.json', '.kiro/agents/payments.md', '.kiro/hooks/legacy.json'],
    },
  }

  async function openReviewDialog() {
    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Review files' }))
    return within(await screen.findByRole('dialog'))
  }

  it('flags a review-stale Project and lists the files awaiting review as relative paths', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [reviewStale] })

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))

    // Warning badge, copy that reads for a first review as well as a changed
    // one, and every stale path rendered as received — any `.kiro/` depth.
    expect(screen.getByText('Review needed')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('that can run code are waiting for your review')
    expect(screen.getByText('.kiro/settings/mcp.json')).toBeInTheDocument()
    expect(screen.getByText('.kiro/agents/payments.md')).toBeInTheDocument()
    expect(screen.getByText('.kiro/hooks/legacy.json')).toBeInTheDocument()
    // Sessions stay blocked until the owner accepts the changes; nothing was
    // accepted by merely opening the page.
    expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
    expect(api.reviewProjectBundle).not.toHaveBeenCalled()
    // Every unreadable remedy ends in "pull updates", so the pull stands
    // beside Review files in this state and runs the page's one pull action.
    const actions = screen.getByRole('button', { name: 'Review files' }).parentElement!
    expect(within(actions).getByRole('button', { name: 'Pull updates' })).toBeInTheDocument()
    fireEvent.click(within(actions).getByRole('button', { name: 'Pull updates' }))
    await waitFor(() => expect(api.syncProjectBundle).toHaveBeenCalledWith(reviewStale.id))
    // The outcome answers where the click was: in the banner's region, not
    // only in the Local copy card further down the page.
    const outcome = await screen.findByTestId('project-sync-outcome-review')
    expect(within(outcome).getByRole('status')).toHaveTextContent('Updates pulled.')
    expect(screen.getByRole('alert').parentElement).toContainElement(outcome)
    expect(screen.queryByTestId('project-sync-outcome-local')).not.toBeInTheDocument()
  })

  it('renders the sync outcome beside the sources-unavailable banner when the sync started there', async () => {
    const unavailableSources = {
      ...managedProject,
      health: { status: 'sources_unavailable' as const, code: 'project_sources_unavailable', unavailable_sources: ['payments-api'] },
    }
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [unavailableSources] })
    vi.mocked(api.syncProjectBundle).mockRejectedValue(new Error('HTTP 500'))

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))
    // Two Pull controls are on the page (banner and Local copy card); the
    // banner's is the first in document order.
    const [bannerSync] = screen.getAllByRole('button', { name: 'Pull updates' })
    fireEvent.click(bannerSync)

    const outcome = await screen.findByTestId('project-sync-outcome-sources')
    expect(within(outcome).getByRole('alert')).toHaveTextContent('HTTP 500')
    expect(screen.queryByTestId('project-sync-outcome-local')).not.toBeInTheDocument()
  })

  /** The structured refusal: the bundle and every source are pulled on their
   *  own, so the body lists what moved beside what did not. */
  function divergedRefusal(advanced: string[], diverged: { checkout: string; detail: string }[]) {
    return Object.assign(new Error('HTTP 409'), {
      status: 409,
      body: JSON.stringify({ error: 'project_checkout_diverged', code: 'project_checkout_diverged', project_id: managedProject.id, advanced, diverged }),
    })
  }

  it.each([
    ['local-commits', /has commits the shared repository does not, so the pull could not fast-forward it and changed nothing/],
    ['dirty-tree', /has uncommitted changes the pull would touch, so it changed nothing/],
    ['unrelated-history', /history is unrelated to the shared repository's, so the pull could not fast-forward it and changed nothing/],
  ])('explains a %s checkout the pull refused to fast-forward, naming the checkout and what did move', async (detail, copy) => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [managedProject] })
    vi.mocked(api.syncProjectBundle).mockRejectedValue(divergedRefusal(['bundle', 'web'], [{ checkout: 'payments-api', detail }]))

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Pull updates' }))

    const outcome = await screen.findByTestId('project-sync-outcome-local')
    const alert = within(outcome).getByRole('alert')
    expect(alert).toHaveTextContent(copy)
    expect(alert).toHaveTextContent('then pull updates again.')
    // One verb on the page: nothing calls the action a sync.
    expect(alert).not.toHaveTextContent(/\bsync\b/i)
    expect(alert).toHaveTextContent('Checkout: payments-api')
    // What DID fast-forward is named too, and last: the bundle is never
    // unwound because a source could not follow.
    expect(alert.textContent).toMatch(/Checkout: payments-api\n\nUpdated: bundle, web/)
    expect(alert).not.toHaveTextContent('HTTP 409')
    expect(within(alert).getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
    expect(screen.queryByText('Updates pulled.')).not.toBeInTheDocument()
  })

  it('renders every diverged checkout with its own detail, and no Updated line when nothing moved', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [managedProject] })
    vi.mocked(api.syncProjectBundle).mockRejectedValue(divergedRefusal([], [
      { checkout: 'bundle', detail: 'dirty-tree' },
      { checkout: 'payments-api', detail: 'local-commits' },
    ]))

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Pull updates' }))

    const alert = within(await screen.findByTestId('project-sync-outcome-local')).getByRole('alert')
    // The bundle entry is shown at the Local copy card's path; the source at its id.
    expect(alert).toHaveTextContent('has uncommitted changes the pull would touch')
    expect(alert).toHaveTextContent(`Checkout: ${managedProject.registrations[0].path}`)
    expect(alert).toHaveTextContent('has commits the shared repository does not')
    expect(alert).toHaveTextContent('Checkout: payments-api')
    expect(alert).not.toHaveTextContent('Updated:')
  })

  it('shows a generic line for a diverged detail it does not know, and one for a refusal naming no checkout', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [managedProject] })
    vi.mocked(api.syncProjectBundle).mockRejectedValueOnce(divergedRefusal(['bundle'], [{ checkout: 'payments-api', detail: 'something-new' }]))

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Pull updates' }))

    let alert = within(await screen.findByTestId('project-sync-outcome-local')).getByRole('alert')
    expect(alert).toHaveTextContent('has diverged from the shared repository, so the pull could not fast-forward it and changed nothing')
    expect(alert).toHaveTextContent('Checkout: payments-api')
    expect(alert).toHaveTextContent('Updated: bundle')

    // A body with neither list still reads as a refusal on the Project's own copy.
    vi.mocked(api.syncProjectBundle).mockRejectedValueOnce(Object.assign(new Error('HTTP 409'), {
      status: 409,
      body: JSON.stringify({ error: 'project_checkout_diverged', code: 'project_checkout_diverged', project_id: managedProject.id }),
    }))
    fireEvent.click(screen.getByRole('button', { name: 'Pull updates' }))
    await waitFor(() => expect(api.syncProjectBundle).toHaveBeenCalledTimes(2))
    await waitFor(() => {
      alert = within(screen.getByTestId('project-sync-outcome-local')).getByRole('alert')
      expect(alert).toHaveTextContent(`Checkout: ${managedProject.registrations[0].path}`)
    })
    expect(alert).not.toHaveTextContent('Updated:')
  })

  it('reads a diverged refusal from its structured lists only, never from a top-level checkout or detail', async () => {
    // The body carries `advanced` and `diverged` and nothing else that names a
    // checkout. A stray top-level pair by those names is not a checkout and
    // must not become a line: only the list entry renders.
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [managedProject] })
    vi.mocked(api.syncProjectBundle).mockRejectedValue(Object.assign(new Error('HTTP 409'), {
      status: 409,
      body: JSON.stringify({
        error: 'project_checkout_diverged', code: 'project_checkout_diverged', project_id: managedProject.id,
        advanced: ['bundle'], diverged: [{ checkout: 'web', detail: 'dirty-tree' }],
        checkout: 'payments-api', detail: 'local-commits',
      }),
    }))

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Pull updates' }))

    const alert = within(await screen.findByTestId('project-sync-outcome-local')).getByRole('alert')
    expect(alert).toHaveTextContent('has uncommitted changes the pull would touch')
    expect(alert).toHaveTextContent('Checkout: web')
    expect(alert).toHaveTextContent('Updated: bundle')
    expect(alert).not.toHaveTextContent('has commits the shared repository does not')
    expect(alert).not.toHaveTextContent('Checkout: payments-api')
  })

  it('withholds accept for a file that is not text, and hands over the path to fix', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [reviewStale] })
    vi.mocked(api.projectBundleReviewPreview).mockResolvedValue({
      digest: 'sha256:binary',
      files: [
        reviewPreview.files[0],
        { path: '.kiro/settings/mcp.json', status: 'unreadable' as const, reason: 'binary' },
      ],
    })
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })

    const dialog = await openReviewDialog()
    const rows = await dialog.findAllByTestId('project-review-file')

    expect(within(rows[1]).getByText('Unreadable')).toBeInTheDocument()
    expect(within(rows[1]).getByText(/This file is not text, so its content cannot be shown or accepted/)).toBeInTheDocument()
    expect(within(rows[1]).getByText(/Replace it with a text file inside the Project, pull updates, and review again/)).toBeInTheDocument()
    expect(within(rows[1]).queryByLabelText(/Content of/)).not.toBeInTheDocument()
    // The path to fix stands on its own line with a copy affordance, so the
    // remedy is actionable without retyping.
    const fixPath = within(rows[1]).getByTestId('project-review-fix-path')
    expect(within(fixPath).getByText('.kiro/settings/mcp.json')).toBeInTheDocument()
    const copy = within(fixPath).getByRole('button', { name: 'Copy path' })
    fireEvent.click(copy)
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('.kiro/settings/mcp.json'))
    expect(await within(fixPath).findByRole('button', { name: 'Path copied' })).toBeInTheDocument()

    const blocked = dialog.getByTestId('project-review-blocked')
    expect(blocked).toHaveTextContent('1 entry cannot be accepted until it is fixed')
    expect(within(blocked).getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
    expect(dialog.getByRole('button', { name: 'Accept these changes' })).toBeDisabled()
  })

  it('reports a copy that did not reach the clipboard through the shared error notice, with the entry still readable', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [reviewStale] })
    vi.mocked(api.projectBundleReviewPreview).mockResolvedValue({
      digest: 'sha256:link',
      files: [{ path: '.kiro/skills/deploy/run.sh', status: 'unreadable' as const, reason: 'link-outside-root' }],
    })
    // Both clipboard layers refuse: the async API rejects (a denied
    // permission) and the execCommand fallback reports nothing copied.
    const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    const originalExecCommand = Object.getOwnPropertyDescriptor(document, 'execCommand')
    const writeText = vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError'))
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    Object.defineProperty(document, 'execCommand', { value: vi.fn().mockReturnValue(false), configurable: true })
    try {
      const dialog = await openReviewDialog()
      const [row] = await dialog.findAllByTestId('project-review-file')
      expect(within(row).queryByTestId('project-review-copy-failed')).not.toBeInTheDocument()

      fireEvent.click(within(row).getByRole('button', { name: 'Copy path' }))
      await waitFor(() => expect(writeText).toHaveBeenCalledWith('.kiro/skills/deploy/run.sh'))

      // The failure is an error, rendered as one: the shared notice (role=alert)
      // under the path, saying what to do instead. No tick over an unchanged
      // clipboard, and no agent hand-off -- the remedy is the path on screen.
      const notice = await within(row).findByTestId('project-review-copy-failed')
      expect(notice).toHaveAttribute('role', 'alert')
      expect(notice).toHaveTextContent('The path could not be copied. Select it above and copy it by hand.')
      expect(within(notice).queryByRole('button', { name: /Ask the agent/ })).not.toBeInTheDocument()
      expect(within(row).queryByRole('button', { name: 'Path copied' })).not.toBeInTheDocument()
      // The entry it concerns is still readable above the notice: reason,
      // path line and the copy affordance all stand.
      expect(within(row).getByText(/link that points outside the Project/)).toBeInTheDocument()
      const fixPath = within(row).getByTestId('project-review-fix-path')
      expect(within(fixPath).getByText('.kiro/skills/deploy/run.sh')).toBeInTheDocument()
      expect(within(fixPath).getByRole('button', { name: /Copy (path|failed)/ })).toBeInTheDocument()

      // Dismissable, so a fixed permission does not leave a stale alert.
      fireEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }))
      expect(within(row).queryByTestId('project-review-copy-failed')).not.toBeInTheDocument()
    } finally {
      if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard)
      else delete (navigator as { clipboard?: unknown }).clipboard
      if (originalExecCommand) Object.defineProperty(document, 'execCommand', originalExecCommand)
      else delete (document as { execCommand?: unknown }).execCommand
    }
  })

  it('shows each file with its status and content in the review dialog, and accepts the previewed digest', async () => {
    vi.mocked(api.projectBundles)
      .mockResolvedValueOnce({ projects: [reviewStale] })
      .mockResolvedValue({ projects: [managedProject] })
    vi.mocked(api.reviewProjectBundle).mockResolvedValue(managedProject)

    const dialog = await openReviewDialog()

    // Clicking "Review files" fetched the preview and posted nothing.
    await waitFor(() => expect(api.projectBundleReviewPreview).toHaveBeenCalledWith(reviewStale.id))
    expect(api.reviewProjectBundle).not.toHaveBeenCalled()
    expect(dialog.getByText('Review changed files in Shared Payments')).toBeInTheDocument()

    // Path, status badge, and the bytes being accepted, per file.
    const rows = await dialog.findAllByTestId('project-review-file')
    expect(rows).toHaveLength(3)
    expect(within(rows[0]).getByText('.kiro/settings/mcp.json')).toBeInTheDocument()
    expect(within(rows[0]).getByText('Changed')).toBeInTheDocument()
    expect(within(rows[0]).getByLabelText('Content of .kiro/settings/mcp.json')).toHaveTextContent('"command": "npx"')
    expect(within(rows[1]).getByText('Added')).toBeInTheDocument()
    expect(within(rows[1]).getByLabelText('Content of .kiro/agents/payments.md')).toHaveTextContent('Runs the refund reconciliation.')
    // Whole content, every time: no entry says a part of the file is missing.
    expect(dialog.queryByText(/Only the first part of this file/)).not.toBeInTheDocument()
    // A removed file has no content; the row says what accepting records.
    expect(within(rows[2]).getByText('Removed')).toBeInTheDocument()
    expect(within(rows[2]).queryByLabelText(/Content of/)).not.toBeInTheDocument()
    expect(within(rows[2]).getByText('This file was removed. Accepting records that it is gone.')).toBeInTheDocument()

    fireEvent.click(dialog.getByRole('button', { name: 'Accept these changes' }))

    // The accept carries the digest the owner was shown, nothing else.
    await waitFor(() => expect(api.reviewProjectBundle).toHaveBeenCalledWith(reviewStale.id, reviewPreview.digest))
    // The refetch returns a healthy Project: badge flips, notice clears, dialog closes.
    expect(await screen.findByText('Healthy')).toBeInTheDocument()
    expect(screen.queryByText('Review needed')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New chat' })).not.toBeDisabled()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('reads as a first review when every file arrived with the Project', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [reviewStale] })
    vi.mocked(api.projectBundleReviewPreview).mockResolvedValue({
      digest: 'sha256:first',
      files: reviewPreview.files.slice(0, 2).map(file => ({ ...file, status: 'added' as const })),
    })

    const dialog = await openReviewDialog()

    expect(await dialog.findByText('Review the files that can run code in Shared Payments')).toBeInTheDocument()
    expect(dialog.getByText(/These files arrived with the Project/)).toBeInTheDocument()
    expect(dialog.queryByText(/changed since you last accepted/)).not.toBeInTheDocument()
    expect(dialog.getAllByText('Added')).toHaveLength(2)
  })

  it('re-fetches the preview and says so when the files moved under the review', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [reviewStale] })
    const movedPreview = {
      digest: 'sha256:moved',
      files: [{ ...reviewPreview.files[0], content: '{ "mcpServers": { "evil": { "command": "curl" } } }' }],
    }
    vi.mocked(api.projectBundleReviewPreview)
      .mockResolvedValueOnce(reviewPreview)
      .mockResolvedValue(movedPreview)
    vi.mocked(api.reviewProjectBundle).mockRejectedValueOnce(Object.assign(new Error('The files changed since this preview.'), {
      status: 409,
      body: JSON.stringify({ error: 'The files changed since this preview.', code: 'project_review_moved' }),
    }))

    const dialog = await openReviewDialog()
    await dialog.findAllByTestId('project-review-file')
    fireEvent.click(dialog.getByRole('button', { name: 'Accept these changes' }))

    await waitFor(() => expect(api.reviewProjectBundle).toHaveBeenCalledWith(reviewStale.id, reviewPreview.digest))
    // The rejection is explained in-dialog, the preview is re-read, and the
    // NEW bytes are what is now on screen.
    expect(await dialog.findByText(/These files changed again while you were reading/)).toBeInTheDocument()
    await waitFor(() => expect(api.projectBundleReviewPreview).toHaveBeenCalledTimes(2))
    expect(await dialog.findByText(/"evil"/)).toBeInTheDocument()
    expect(dialog.queryByText(/"atlassian"/)).not.toBeInTheDocument()
    // Nothing was recorded: the Project is still review-stale.
    expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()

    // A second accept carries the re-read digest.
    vi.mocked(api.reviewProjectBundle).mockResolvedValue(managedProject)
    fireEvent.click(dialog.getByRole('button', { name: 'Accept these changes' }))
    await waitFor(() => expect(api.reviewProjectBundle).toHaveBeenLastCalledWith(reviewStale.id, 'sha256:moved'))
  })

  it('withholds accept while an entry can never be accepted, and says why', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [reviewStale] })
    vi.mocked(api.projectBundleReviewPreview).mockResolvedValue({
      digest: 'sha256:blocked',
      files: [
        reviewPreview.files[0],
        { path: '.kiro/skills/deploy/run.sh', status: 'unreadable' as const, reason: 'link-outside-root' },
      ],
    })

    const dialog = await openReviewDialog()
    const rows = await dialog.findAllByTestId('project-review-file')

    expect(within(rows[1]).getByText('Unreadable')).toBeInTheDocument()
    expect(within(rows[1]).getByText(/link that points outside the Project/)).toBeInTheDocument()
    // Every unreadable entry carries its path with a copy button.
    const fixPath = within(rows[1]).getByTestId('project-review-fix-path')
    expect(within(fixPath).getByText('.kiro/skills/deploy/run.sh')).toBeInTheDocument()
    expect(within(fixPath).getByRole('button', { name: 'Copy path' })).toBeInTheDocument()
    const blocked = dialog.getByTestId('project-review-blocked')
    expect(blocked).toHaveTextContent('1 entry cannot be accepted until it is fixed')
    const accept = dialog.getByRole('button', { name: 'Accept these changes' })
    expect(accept).toBeDisabled()
    fireEvent.click(accept)
    expect(api.reviewProjectBundle).not.toHaveBeenCalled()
    // A withheld accept is not the end: the hand-off beside the blocker
    // stages a chat that names the Project and the exact entries to fix, and
    // closes the dialog so it does not sit over the chat it navigates to.
    fireEvent.click(within(blocked).getByRole('button', { name: /Ask the agent/ }))
    const staged = consumeChatHandoff()
    expect(staged).toContain('Project: Shared Payments')
    expect(staged).toContain('- .kiro/skills/deploy/run.sh (link-outside-root)')
    expect(staged).toContain('Code: project_review_unreviewable')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('withholds accept for an entry the redactor changed, and points the review outside the dashboard', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [reviewStale] })
    vi.mocked(api.projectBundleReviewPreview).mockResolvedValue({
      digest: 'sha256:redacted',
      files: [
        reviewPreview.files[0],
        { path: '.kiro/settings/secrets.env', status: 'unreadable' as const, reason: 'redacted' },
      ],
    })

    const dialog = await openReviewDialog()
    const rows = await dialog.findAllByTestId('project-review-file')

    // No content: the bytes on screen would not be the bytes accepted. The
    // remedy is not "replace the file" — it is to review it elsewhere.
    expect(within(rows[1]).getByText('Unreadable')).toBeInTheDocument()
    expect(within(rows[1]).queryByRole('region')).not.toBeInTheDocument()
    expect(within(rows[1]).getByText(
      'Contains a value the dashboard must redact, so it cannot be shown whole. Review this file outside the dashboard.',
    )).toBeInTheDocument()
    expect(within(within(rows[1]).getByTestId('project-review-fix-path')).getByText('.kiro/settings/secrets.env')).toBeInTheDocument()
    expect(dialog.getByTestId('project-review-blocked')).toHaveTextContent('1 entry cannot be accepted until it is fixed')
    expect(dialog.getByRole('button', { name: 'Accept these changes' })).toBeDisabled()
    // The hand-off names the entry with the server's reason, and says a
    // redacted entry is reviewed outside the dashboard rather than replaced.
    fireEvent.click(within(dialog.getByTestId('project-review-blocked')).getByRole('button', { name: /Ask the agent/ }))
    const staged = consumeChatHandoff()
    expect(staged).toContain('- .kiro/settings/secrets.env (redacted)')
    expect(staged).toContain('reviewed outside it')
  })

  it('renders a preview that could not be loaded as a failure with the hand-off', async () => {
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [reviewStale] })
    vi.mocked(api.projectBundleReviewPreview).mockRejectedValue(new Error('HTTP 500'))

    const dialog = await openReviewDialog()

    expect(await dialog.findByRole('alert')).toHaveTextContent('HTTP 500')
    expect(dialog.getByRole('button', { name: 'Accept these changes' })).toBeDisabled()
  })

  it('flags a sources-unavailable Project, lists the missing source ids in monospace, and blocks new sessions', async () => {
    const sourcesUnavailable = {
      ...managedProject,
      sources: [
        ...managedProject.sources,
        { id: 'payments-infra-1a2b3c4d', type: 'repo', url: 'https://github.com/acme/payments-infra', default_branch: 'main' },
      ],
      health: {
        status: 'sources_unavailable' as const,
        code: 'project_sources_unavailable',
        unavailable_sources: ['payments-infra-1a2b3c4d'],
      },
    }
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [sourcesUnavailable] })

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))

    // Error badge with its own label, distinct from the review-stale copy —
    // once in the header, once on the failing row (checked below).
    expect(screen.getAllByText('Source unavailable')).toHaveLength(2)
    const alert = screen.getByRole('alert')
    // The edit leads, naming project.yaml by its full path (the local copy
    // path the page already shows), then what the pause costs — no
    // "Manifest:" label, no "cloned".
    expect(alert.textContent).toMatch(/^Edit \/data\/projects\/shared-payments\/project\.yaml to fix the sources listed below, then pull updates\./)
    expect(alert).toHaveTextContent('New sessions are paused until every source is available.')
    expect(alert).not.toHaveTextContent('Manifest:')
    expect(alert).not.toHaveTextContent('cloned')
    // The failing source's synthesized id, rendered in monospace in the notice…
    const ids = screen.getAllByText('payments-infra-1a2b3c4d')
    expect(ids.some(el => el.className.includes('font-mono'))).toBe(true)
    // …and marked INSIDE the Repositories card with the SAME term the banner's
    // badge uses, so one state has one name on the page.
    const infraRow = screen.getByTestId('project-source-payments-infra-1a2b3c4d')
    expect(within(infraRow).getByText('Source unavailable')).toBeInTheDocument()
    expect(within(infraRow).getByText('https://github.com/acme/payments-infra')).toBeInTheDocument()
    expect(screen.queryByText('Clone failed')).not.toBeInTheDocument()
    const apiRow = screen.getByTestId('project-source-payments-api')
    expect(within(apiRow).queryByText('Source unavailable')).not.toBeInTheDocument()
    // Session start is blocked by the existing status !== 'healthy' guard.
    expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
  })

  it('shows both the review-stale notice and the unavailable-source list when they co-occur', async () => {
    const combined = {
      ...managedProject,
      health: {
        status: 'review_stale' as const,
        code: 'project_review_stale',
        stale_files: ['.kiro/settings/mcp.json'],
        unavailable_sources: ['payments-infra-1a2b3c4d'],
      },
    }
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [combined] })

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))

    // A stale digest outranks a missing secondary source: the badge is the
    // review-stale warning, not the error badge.
    expect(screen.getByText('Review needed')).toBeInTheDocument()
    expect(screen.queryByText('Source unavailable')).not.toBeInTheDocument()
    // Both notices render side by side.
    const alerts = screen.getAllByRole('alert')
    expect(alerts.length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('.kiro/settings/mcp.json')).toBeInTheDocument()
    expect(screen.getByText(/Edit \/data\/projects\/shared-payments\/project\.yaml/)).toBeInTheDocument()
    expect(screen.getByText('payments-infra-1a2b3c4d')).toBeInTheDocument()
    // Sessions stay blocked while the digest is unreviewed.
    expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
  })

  it('explains which files removal preserves', async () => {
    vi.mocked(api.projectBundles)
      .mockResolvedValueOnce({ projects: [localProject] })
      .mockResolvedValue({ projects: [] })
    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Payments Platform/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove from Kiro Crew' }))

    const dialog = within(await screen.findByRole('dialog'))
    expect(dialog.getByText('Remove Payments Platform?')).toBeInTheDocument()
    // What removal keeps (the owner's folders) and what happens to the
    // Project's sessions: history stays, work stops, the way out is a new
    // chat outside the project -- the same term the create controls use.
    expect(dialog.getByText(
      'Folders you added stay on disk. Kiro Crew removes only storage it created for this project. Its sessions keep their history but stop working; start new chats outside this project.',
    )).toBeInTheDocument()
    // The confirm repeats the trigger's own label, so the owner confirms the
    // thing they clicked; the title keeps the Project's name.
    expect(dialog.queryByRole('button', { name: 'Remove project' })).not.toBeInTheDocument()
    fireEvent.click(dialog.getByRole('button', { name: 'Remove from Kiro Crew' }))

    await waitFor(() => expect(api.removeProjectBundle).toHaveBeenCalledWith(localProject.id))
    expect(await screen.findByText('No projects yet')).toBeInTheDocument()
    // A clean removal leaves no notice behind.
    expect(screen.queryByTestId('project-remove-cleanup-pending')).not.toBeInTheDocument()
  })

  it('reports a removal that left files on disk above the list, naming every leftover path', async () => {
    vi.mocked(api.projectBundles)
      .mockResolvedValueOnce({ projects: [managedProject, localProject] })
      .mockResolvedValue({ projects: [localProject] })
    // The registration is gone (200), but the on-disk roots could not all be
    // deleted: the server lists what remains, relative to projects/.
    vi.mocked(api.removeProjectBundle).mockResolvedValue({
      ok: true,
      id: managedProject.id,
      cleanup_pending: [`managed/${managedProject.id}`, `state/${managedProject.id}`],
    })
    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove from Kiro Crew' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Remove from Kiro Crew' }))

    await waitFor(() => expect(api.removeProjectBundle).toHaveBeenCalledWith(managedProject.id))
    // Back on the list, the Project is gone and the notice stands above the
    // remaining rows: the shared error surface, naming the Project, saying it
    // was removed, and listing the paths with the agent hand-off.
    const notice = await screen.findByTestId('project-remove-cleanup-pending')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent('Shared Payments')
    expect(notice).toHaveTextContent('Removed from Kiro Crew. Some files could not be deleted.')
    expect(notice).toHaveTextContent(`managed/${managedProject.id}`)
    expect(notice).toHaveTextContent(`state/${managedProject.id}`)
    expect(within(notice).getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /Open project Payments Platform/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Open project Shared Payments/ })).not.toBeInTheDocument()
    // Dismissable once the owner has cleaned up by hand.
    fireEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('project-remove-cleanup-pending')).not.toBeInTheDocument()
  })

  it('marks a declared source that waits on the review as pending, with what lifts it', async () => {
    // A freshly added Project declares sources it has not cloned: they enter
    // the checkout only once the owner accepts the review that names them, so
    // the payload carries them as `pending` under a review-stale health.
    const pendingSources = {
      ...reviewStale,
      health: { ...reviewStale.health, stale_files: ['project.yaml'] },
      sources: [
        { ...managedProject.sources[0], status: 'pending' },
        { id: 'payments-infra-1a2b3c4d', type: 'repo', url: 'https://github.com/acme/payments-infra', default_branch: 'main', status: 'pending' },
      ],
    }
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [pendingSources] })

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))

    // The banner is the review-stale one, whose copy already reads for a
    // first review; nothing calls the sources unavailable.
    expect(screen.getByText('Review needed')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('waiting for your review')
    expect(screen.queryByText('Source unavailable')).not.toBeInTheDocument()
    // Each pending row wears the muted badge and says when the clone happens.
    expect(screen.getAllByText('Pending review')).toHaveLength(2)
    expect(screen.getAllByText('Cloned after you accept the review.')).toHaveLength(2)
    const infraRow = screen.getByTestId('project-source-payments-infra-1a2b3c4d')
    expect(within(infraRow).getByText('Pending review')).toBeInTheDocument()
    expect(within(infraRow).getByText('https://github.com/acme/payments-infra')).toBeInTheDocument()
    // Sessions wait on the acceptance, like every review-stale Project.
    expect(screen.getByRole('button', { name: 'New chat' })).toBeDisabled()
  })

  it('reads an unavailable source status off the row itself, and shows a healthy row bare', async () => {
    const mixed = {
      ...managedProject,
      sources: [
        { ...managedProject.sources[0], status: 'healthy' },
        { id: 'payments-infra-1a2b3c4d', type: 'repo', url: 'https://github.com/acme/payments-infra', status: 'unavailable' },
      ],
      health: { status: 'sources_unavailable' as const, code: 'project_sources_unavailable', unavailable_sources: ['payments-infra-1a2b3c4d'] },
    }
    vi.mocked(api.projectBundles).mockResolvedValue({ projects: [mixed] })

    renderWithProviders(<ProjectBundlesPage />)
    fireEvent.click(await screen.findByRole('button', { name: /Open project Shared Payments/ }))

    const infraRow = screen.getByTestId('project-source-payments-infra-1a2b3c4d')
    expect(within(infraRow).getByText('Source unavailable')).toBeInTheDocument()
    expect(within(infraRow).queryByText('Pending review')).not.toBeInTheDocument()
    const apiRow = screen.getByTestId('project-source-payments-api')
    expect(within(apiRow).queryByText('Source unavailable')).not.toBeInTheDocument()
    expect(within(apiRow).queryByText('Pending review')).not.toBeInTheDocument()
  })
})
