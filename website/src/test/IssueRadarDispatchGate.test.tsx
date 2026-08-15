import { describe, it, expect, vi, beforeEach } from 'vitest'

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The UI half of the dispatch gate. Both surfaces render a decision the SERVER
// made, so every assertion here is about faithfully reflecting that answer rather
// than about re-deriving it.
//
// The load-bearing assertions, stated up front so a future edit knows what it
// would be removing:
//
//  * "No checkout set" and "the checkout you set broke" render as DIFFERENT
//    sentences. They are separate server reasons precisely because one asks the
//    user to set a value and the other tells them their value broke, and
//    collapsing them in the UI throws that away.
//  * A refused write KEEPS the user's text and shows the server's reason. Snapping
//    the field back to the saved value would read exactly like a successful save
//    of a value the server never took.
//  * A successful write renders the RESOLVED path the server stored, not the
//    string that was typed — a user who pastes a symlink should see where the work
//    will actually happen.
//  * A commit that changes nothing sends no write at all.
const api = {
  getDispatchReadiness: vi.fn(),
  setRepoLocalPath: vi.fn(),
}
vi.mock('../apps/issue-radar/api', () => ({ issueRadarApi: api }))

const DispatchSettings = (
  await import('../apps/issue-radar/views/settings/DispatchSettings')
).default

const REF = { owner: 'acme', repo: 'widget' }

function ready(over: Record<string, unknown> = {}) {
  return {
    owner: 'acme',
    repo: 'widget',
    ready: true,
    reason: 'ok',
    local_path: '/home/me/code/widget',
    ...over,
  }
}

function mount(node: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('DispatchSettings', () => {
  it('shows the saved checkout and a ready status', async () => {
    api.getDispatchReadiness.mockResolvedValue(ready())
    mount(<DispatchSettings repoRef={REF} />)
    expect(await screen.findByDisplayValue('/home/me/code/widget')).toBeTruthy()
    await waitFor(() => expect(screen.getByText(/Ready to work on issues/i)).toBeTruthy())
  })

  it('says a repo has no checkout set rather than showing an empty field with no explanation', async () => {
    api.getDispatchReadiness.mockResolvedValue(
      ready({ ready: false, reason: 'no_local_path', local_path: '' }),
    )
    mount(<DispatchSettings repoRef={REF} />)
    await waitFor(() => expect(screen.getByText(/Not set, so issues cannot be worked on yet/i)).toBeTruthy())
  })

  it('keeps the typed text and says a known refusal in the catalog\'s words', async () => {
    api.getDispatchReadiness.mockResolvedValue(
      ready({ ready: false, reason: 'no_local_path', local_path: '' }),
    )
    // What the server actually sends: a machine-readable code beside the prose.
    // The UI matches the code, so the raw sentence (which names the local_path
    // field) never reaches a user.
    const err = new Error('local_path must be an absolute path to an existing git checkout')
    ;(err as Error & { code?: string }).code = 'invalid_local_path'
    api.setRepoLocalPath.mockRejectedValue(err)
    mount(<DispatchSettings repoRef={REF} />)
    // The field is disabled while readiness is in flight; typing into a disabled
    // input silently does nothing, so wait for the answer to land first.
    await screen.findByText(/Not set, so issues cannot be worked on yet/i)
    const field = screen.getByRole('textbox')
    await userEvent.type(field, '/nope/not-a-repo')
    await userEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(screen.getByText(/not a usable repository/i)).toBeTruthy())
    expect(screen.queryByText(/local_path/)).toBeNull()
    // The refusal is the point: the text survives so the user fixes one character
    // instead of retyping an absolute path from memory.
    expect(screen.getByDisplayValue('/nope/not-a-repo')).toBeTruthy()
  })

  it('falls back to the server\'s text for a refusal code it does not know', async () => {
    api.getDispatchReadiness.mockResolvedValue(
      ready({ ready: false, reason: 'no_local_path', local_path: '' }),
    )
    // A generic apology would hide what went wrong, so an unmapped code shows
    // whatever the server said.
    api.setRepoLocalPath.mockRejectedValue(new Error('the gateway is having a day'))
    mount(<DispatchSettings repoRef={REF} />)
    await screen.findByText(/Not set, so issues cannot be worked on yet/i)
    await userEvent.type(screen.getByRole('textbox'), '/home/me/code/widget')
    await userEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(screen.getByText(/having a day/i)).toBeTruthy())
  })

  it('renders the resolved path the server stored, not what was typed', async () => {
    api.getDispatchReadiness.mockResolvedValue(
      ready({ ready: false, reason: 'no_local_path', local_path: '' }),
    )
    api.setRepoLocalPath.mockResolvedValue(ready({ local_path: '/real/widget' }))
    mount(<DispatchSettings repoRef={REF} />)
    // The field is disabled while readiness is in flight; typing into a disabled
    // input silently does nothing, so wait for the answer to land first.
    await screen.findByText(/Not set, so issues cannot be worked on yet/i)
    const field = screen.getByRole('textbox')
    await userEvent.type(field, '/a/symlink')
    await userEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(screen.getByDisplayValue('/real/widget')).toBeTruthy())
  })

  it('refuses a relative path locally, without asking the server', async () => {
    api.getDispatchReadiness.mockResolvedValue(
      ready({ ready: false, reason: 'no_local_path', local_path: '' }),
    )
    mount(<DispatchSettings repoRef={REF} />)
    // The field is disabled while readiness is in flight; typing into a disabled
    // input silently does nothing, so wait for the answer to land first.
    await screen.findByText(/Not set, so issues cannot be worked on yet/i)
    const field = screen.getByRole('textbox')
    await userEvent.type(field, 'code/widget')
    await userEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(screen.getByText(/Enter an absolute path/i)).toBeTruthy())
    expect(api.setRepoLocalPath).not.toHaveBeenCalled()
  })

  it('sends no write when the value did not change', async () => {
    api.getDispatchReadiness.mockResolvedValue(ready())
    mount(<DispatchSettings repoRef={REF} />)
    await screen.findByText(/Ready to work on issues/i)
    await userEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(api.setRepoLocalPath).not.toHaveBeenCalled())
  })

  it('clears the checkout with an empty value', async () => {
    api.getDispatchReadiness.mockResolvedValue(ready())
    api.setRepoLocalPath.mockResolvedValue(
      ready({ ready: false, reason: 'no_local_path', local_path: '' }),
    )
    mount(<DispatchSettings repoRef={REF} />)
    await screen.findByText(/Ready to work on issues/i)
    const field = screen.getByDisplayValue('/home/me/code/widget')
    await userEvent.clear(field)
    await userEvent.click(screen.getByRole('button'))
    await waitFor(() => expect(api.setRepoLocalPath).toHaveBeenCalledWith(REF, ''))
    await waitFor(() => expect(screen.getByText(/Not set, so issues cannot be worked on yet/i)).toBeTruthy())
  })
})
