import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  grantSkillTrust: vi.fn(),
  skillTrust: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import ProjectSkillsTrustDialog from '../components/ProjectSkillsTrustDialog'

/** The Trust button, once the dialog has resolved the folder and enabled it.
 *  Consent is gated on knowing the directory, so every click must wait for it. */
async function enabledTrustButton() {
  const btn = await screen.findByRole('button', { name: /Trust this folder/ })
  await waitFor(() => expect(btn).not.toBeDisabled())
  return btn
}

function Harness(props: Partial<React.ComponentProps<typeof ProjectSkillsTrustDialog>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <ProjectSkillsTrustDialog
        open
        skillLeaf="oncall-handover"
        slotKey="dashboard:chat-7"
        onClose={vi.fn()}
        onTrusted={vi.fn()}
        {...props}
      />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  // clearAllMocks (not just restoreAllMocks): these are vi.hoisted vi.fn()s, so
  // restore leaves their CALL HISTORY intact and a `not.toHaveBeenCalled`
  // assertion would read a previous test's call.
  vi.clearAllMocks()
  mockApi.grantSkillTrust.mockResolvedValue({ trusted: true })
  // The dialog shows the directory before it will let anyone consent to it.
  mockApi.skillTrust.mockResolvedValue({ project: '/work/checkout-service' })
})

describe('ProjectSkillsTrustDialog', () => {
  it('renders nothing when closed', () => {
    render(<Harness open={false} />)
    expect(screen.queryByText(/Trust this project's skills\?/)).not.toBeInTheDocument()
  })

  it('names the skill the operator was trying to use', async () => {
    render(<Harness />)
    expect(await screen.findByText(/\$oncall-handover/)).toBeInTheDocument()
  })

  it('states what trusting the folder allows', async () => {
    render(<Harness />)
    // The consent must name the CONSEQUENCE, not the mechanism.
    expect(await screen.findByText(/instruct the agent to run commands/)).toBeInTheDocument()
  })

  it('states what declining does, so the safe choice is not unexplained', async () => {
    render(<Harness />)
    expect(await screen.findByText(/nothing is loaded/)).toBeInTheDocument()
  })

  it('tells the operator the grant can be withdrawn later', async () => {
    render(<Harness />)
    expect(await screen.findByText(/withdraw this later/)).toBeInTheDocument()
  })

  it('grants for the requesting slot and reports the leaf back', async () => {
    const onTrusted = vi.fn()
    render(<Harness onTrusted={onTrusted} />)
    fireEvent.click(await enabledTrustButton())
    await waitFor(() =>
      expect(mockApi.grantSkillTrust).toHaveBeenCalledWith(
        'dashboard:chat-7',
        '/work/checkout-service',
      ),
    )
    await waitFor(() => expect(onTrusted).toHaveBeenCalledWith('oncall-handover'))
  })

  it('declining closes without granting anything', async () => {
    const onClose = vi.fn()
    const onTrusted = vi.fn()
    render(<Harness onClose={onClose} onTrusted={onTrusted} />)
    fireEvent.click(await screen.findByRole('button', { name: /Not now/ }))
    expect(onClose).toHaveBeenCalled()
    expect(mockApi.grantSkillTrust).not.toHaveBeenCalled()
    expect(onTrusted).not.toHaveBeenCalled()
  })

  it('surfaces the server reason when the grant is refused', async () => {
    // Duck-typed unwrap: the real ApiError carries `body`, and an instanceof
    // check would read false under a partial mock or across bundle realms.
    mockApi.grantSkillTrust.mockRejectedValue({
      status: 400,
      body: JSON.stringify({ error: 'no project is set for this chat' }),
    })
    const onTrusted = vi.fn()
    render(<Harness onTrusted={onTrusted} />)
    fireEvent.click(await enabledTrustButton())
    expect(await screen.findByRole('alert')).toHaveTextContent('no project is set for this chat')
    // A refused grant must not be reported as consent.
    expect(onTrusted).not.toHaveBeenCalled()
  })

  it('falls back to a generic message when the failure carries no reason', async () => {
    mockApi.grantSkillTrust.mockRejectedValue({ status: 500 })
    render(<Harness />)
    fireEvent.click(await enabledTrustButton())
    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not record trust/)
  })

  it('accepts an object error body as well as a JSON string', async () => {
    mockApi.grantSkillTrust.mockRejectedValue({ body: { error: 'unusable project' } })
    render(<Harness />)
    fireEvent.click(await enabledTrustButton())
    expect(await screen.findByRole('alert')).toHaveTextContent('unusable project')
  })

  it('will not let the operator consent before the folder is known', async () => {
    // Consent has to name what is being consented to. Until the trust snapshot
    // resolves there is no directory to show -- and nothing for the server to
    // check the grant against -- so Confirm stays disabled and nothing is sent.
    mockApi.skillTrust.mockReturnValue(new Promise(() => {}))
    render(<Harness />)

    const confirm = await screen.findByRole('button', { name: /Trust this folder/ })
    expect(confirm).toBeDisabled()
    expect(mockApi.grantSkillTrust).not.toHaveBeenCalled()
  })

  it('does not report success when the server says the skill still is not usable', async () => {
    // The grant IS recorded, but `skills.project_skills_enabled` is off, so the
    // snapshot comes back trusted:false with no error. Reporting success here
    // inserts a $token that expands to nothing.
    const onTrusted = vi.fn()
    mockApi.grantSkillTrust.mockResolvedValue({ trusted: false })
    render(<Harness onTrusted={onTrusted} />)

    fireEvent.click(await enabledTrustButton())

    await waitFor(() => expect(mockApi.grantSkillTrust).toHaveBeenCalled())
    // The caller is NOT told the skill is ready...
    expect(onTrusted).not.toHaveBeenCalled()
    // ...and the operator is told why nothing happened rather than left guessing.
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })
})
