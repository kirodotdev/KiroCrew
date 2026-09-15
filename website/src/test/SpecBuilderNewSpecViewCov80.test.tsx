// NewSpecView — the conversational spec creator. The name is derived, the
// worktree opt-in only appears for a git folder, and a name collision is retried
// once with a numeric suffix.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const browse = vi.fn()
const create = vi.fn()

vi.mock('../apps/spec-builder/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../apps/spec-builder/api')>()
  return {
    ...actual,
    specApi: { ...actual.specApi, browse: (...a: unknown[]) => browse(...a), create: (...a: unknown[]) => create(...a) },
  }
})

// The real picker browses the filesystem over the app API; the view only needs a
// value + onChange from it.
vi.mock('../apps/spec-builder/components/ProjectPicker', () => ({
  default: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <input aria-label="zz-project-picker" value={value} onChange={e => onChange(e.target.value)} />
  ),
}))

import NewSpecView from '../apps/spec-builder/components/NewSpecView'

function renderView(handlers: Partial<{
  onCancel: () => void
  onCreated: (n: string) => void
  setErr: (m: string) => void
  onSettings: () => void
}> = {}) {
  const props = {
    onCancel: vi.fn(),
    onCreated: vi.fn(),
    setErr: vi.fn(),
    onSettings: vi.fn(),
    ...handlers,
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <NewSpecView {...props} />
    </QueryClientProvider>,
  )
  return props
}

/** Fill the two user-owned fields so the submit button becomes enabled. */
function fillForm(desc = 'Add zzlogin with a passkey', dir = '/zz/project') {
  fireEvent.change(screen.getByLabelText(/describe what you want to do/i), { target: { value: desc } })
  fireEvent.change(screen.getByLabelText('zz-project-picker'), { target: { value: dir } })
}

const submit = () => screen.getByRole('button', { name: /Start the conversation/i })

beforeEach(() => {
  browse.mockReset().mockResolvedValue({ path: '/zz/project', parent: '/zz', dirs: [], is_git: true })
  create.mockReset().mockResolvedValue({})
})

describe('NewSpecView readiness', () => {
  it('keeps submission disabled until both fields are filled', () => {
    renderView()
    expect(submit()).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/describe what you want to do/i), { target: { value: 'zz idea' } })
    expect(submit()).toBeDisabled()
    fireEvent.change(screen.getByLabelText('zz-project-picker'), { target: { value: '/zz/project' } })
    expect(submit()).toBeEnabled()
  })

  it('nudges toward picking a folder before the worktree option can apply', () => {
    renderView()
    expect(screen.getByText(/pick a project folder/i)).toBeInTheDocument()
  })

  it('cancels without creating anything', () => {
    const { onCancel } = renderView()
    fireEvent.click(screen.getByRole('button', { name: /Never mind/i }))
    expect(onCancel).toHaveBeenCalled()
    expect(create).not.toHaveBeenCalled()
  })

  it('opens settings from the storage-location footnote', () => {
    const { onSettings } = renderView()
    fireEvent.click(screen.getByText('Settings'))
    expect(onSettings).toHaveBeenCalled()
  })
})

describe('NewSpecView worktree opt-in', () => {
  it('offers the opt-in for a git folder, unchecked, and previews the branch', async () => {
    renderView()
    fillForm()
    const optIn = await screen.findByLabelText('Work in a fresh worktree')
    expect(optIn).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByText(/spec\/add-zzlogin-with-a-passkey/)).toBeInTheDocument()
    fireEvent.click(optIn)
    expect(screen.getByLabelText('Work in a fresh worktree')).toHaveAttribute('aria-pressed', 'true')
  })

  it('explains that a non-git folder is worked in directly', async () => {
    browse.mockResolvedValue({ path: '/zz/project', parent: '/zz', dirs: [], is_git: false })
    renderView()
    fillForm()
    expect(await screen.findByText(/isn.t a git repository/i)).toBeInTheDocument()
    expect(screen.queryByLabelText('Work in a fresh worktree')).not.toBeInTheDocument()
  })

  it('withholds the opt-in while the git probe is in flight', () => {
    browse.mockReturnValue(new Promise(() => {}))
    renderView()
    fillForm()
    expect(screen.queryByLabelText('Work in a fresh worktree')).not.toBeInTheDocument()
    expect(screen.queryByText(/isn.t a git repository/i)).not.toBeInTheDocument()
  })
})

describe('NewSpecView type picker', () => {
  it('starts on the feature type and switches on click', () => {
    renderView()
    const options = screen.getAllByRole('button').filter(b => b.getAttribute('aria-pressed') !== null)
    expect(options[0]).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(options[1])
    expect(options[1]).toHaveAttribute('aria-pressed', 'true')
    expect(options[0]).toHaveAttribute('aria-pressed', 'false')
  })

  it('sends the chosen type with the create request', async () => {
    renderView()
    fillForm()
    const options = screen.getAllByRole('button').filter(b => b.getAttribute('aria-pressed') !== null)
    fireEvent.click(options.at(-1)!)
    fireEvent.click(submit())
    await waitFor(() => expect(create).toHaveBeenCalled())
    expect(create.mock.calls[0][0].spec_type).toBe('quick')
  })
})

describe('NewSpecView creation', () => {
  it('posts the derived name and the trimmed folder', async () => {
    const { onCreated } = renderView()
    fillForm('Add zzlogin with a passkey', '  /zz/project  ')
    fireEvent.click(submit())
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('add-zzlogin-with-a-passkey'))
    expect(create.mock.calls[0][0]).toMatchObject({
      name: 'add-zzlogin-with-a-passkey',
      working_dir: '/zz/project',
      use_worktree: false,
    })
  })

  it('only requests a worktree when the git opt-in is checked', async () => {
    renderView()
    fillForm()
    fireEvent.click(await screen.findByLabelText('Work in a fresh worktree'))
    fireEvent.click(submit())
    await waitFor(() => expect(create).toHaveBeenCalled())
    expect(create.mock.calls[0][0].use_worktree).toBe(true)
  })

  it('retries once with a numeric suffix on a name collision', async () => {
    create.mockRejectedValueOnce(new Error('A spec named that already exists'))
    const { onCreated } = renderView()
    fillForm()
    fireEvent.click(submit())
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2))
    const retried = create.mock.calls[1][0].name as string
    expect(retried).not.toBe('add-zzlogin-with-a-passkey')
    expect(retried).toMatch(/^add-zzlogin-with-a-passkey-\d{1,3}$/)
    expect(onCreated).toHaveBeenCalledWith(retried)
  })

  it('keeps the suffix within the name cap for a long description', async () => {
    create.mockRejectedValueOnce(new Error('already exists'))
    renderView()
    fillForm('zzz '.repeat(30).trim())
    fireEvent.click(submit())
    await waitFor(() => expect(create).toHaveBeenCalledTimes(2))
    expect((create.mock.calls[1][0].name as string).length).toBeLessThanOrEqual(48)
  })

  it('surfaces any other failure and re-enables the button', async () => {
    create.mockRejectedValue(new Error('zz-backend-refused'))
    const { setErr, onCreated } = renderView()
    fillForm()
    fireEvent.click(submit())
    await waitFor(() => expect(setErr).toHaveBeenCalledWith('zz-backend-refused'))
    expect(onCreated).not.toHaveBeenCalled()
    expect(create).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(submit()).toBeEnabled())
  })

  it('shows a busy label while the request is in flight', async () => {
    create.mockReturnValue(new Promise(() => {}))
    renderView()
    fillForm()
    fireEvent.click(submit())
    expect(await screen.findByRole('button', { name: /Starting/i })).toBeDisabled()
  })
})
