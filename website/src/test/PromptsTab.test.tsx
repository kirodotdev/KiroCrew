import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  prompts: vi.fn(),
  promptDetail: vi.fn(),
  createPrompt: vi.fn(),
  updatePrompt: vi.fn(),
  deletePrompt: vi.fn(),
}))
// A stub ApiError, declared inside vi.hoisted so the mock factory (which is
// hoisted above the imports) can close over it: the component branches on
// `instanceof ApiError` before reading the coded body, so the mock must export
// something that branch recognizes. Same shape as SecurityPanel.test.tsx.
const StubApiError = vi.hoisted(() => class ApiError extends Error {
  status: number
  body: string
  constructor(status: number, message: string, body = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
})
vi.mock('../api/client', () => ({ api: mockApi, ApiError: StubApiError }))

vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))

vi.mock('../store', () => ({
  useAppDispatch: () => vi.fn(),
  useAppSelector: () => [],
}))

import PromptsTab from '../pages/overview/PromptsTab'
import { parsePromptContent, assemblePromptContent } from '../components/PromptForm'

const USER_PROMPT = {
  name: 'my-prompt', fullName: 'my-prompt', description: 'mine',
  path: '~/.kiro/prompts/my-prompt.md', package: '', source: 'global',
}
const PACKAGE_PROMPT = {
  name: 'sop', fullName: 'agent-sop:sop', description: 'shipped',
  path: '~/pkg/sop.sop.md', package: 'Pkg-1.0', source: 'package',
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><PromptsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

/** Select a prompt in the list pane so the detail pane renders it. The list
 *  row shows the bare stem; the detail header shows the full invocation name. */
async function select(name: string) {
  fireEvent.click(await screen.findByRole('option', { name: new RegExp(name) }))
  await waitFor(() => expect(mockApi.promptDetail).toHaveBeenCalled())
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.prompts.mockResolvedValue([])
  mockApi.promptDetail.mockResolvedValue({
    content: '---\ndescription: mine\n---\n\nbody text', redacted: false,
    hash: 'a'.repeat(64),
  })
  mockApi.createPrompt.mockResolvedValue({ ok: true })
  mockApi.updatePrompt.mockResolvedValue({ ok: true, hash: 'b'.repeat(64) })
  mockApi.deletePrompt.mockResolvedValue({ ok: true })
})

describe('PromptsTab authoring', () => {
  it('creates a prompt with the chosen scope and assembled frontmatter', async () => {
    renderTab()
    fireEvent.click(await screen.findByText('Create New Prompt'))

    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: 'My Prompt' } })
    fireEvent.change(screen.getByPlaceholderText(/One line shown/), { target: { value: 'does a thing' } })
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'Do the thing.' } })
    fireEvent.click(screen.getByRole('radio', { name: 'This project' }))
    fireEvent.click(screen.getByText('Create'))

    await waitFor(() => expect(mockApi.createPrompt).toHaveBeenCalledWith(
      'My Prompt',
      '---\ndescription: does a thing\n---\n\nDo the thing.',
      'local',
    ))
  })

  it('writes a bare body when no description is given', async () => {
    renderTab()
    fireEvent.click(await screen.findByText('Create New Prompt'))
    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: 'p' } })
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'Just body.' } })
    fireEvent.click(screen.getByText('Create'))

    await waitFor(() => expect(mockApi.createPrompt).toHaveBeenCalledWith('p', 'Just body.', 'global'))
  })

  it('keeps Create disabled until both name and body are filled', async () => {
    renderTab()
    fireEvent.click(await screen.findByText('Create New Prompt'))
    const create = screen.getByText('Create')
    expect(create).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: 'p' } })
    expect(create).toBeDisabled()

    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'b' } })
    expect(create).not.toBeDisabled()
  })

  it('keeps Create disabled for a name the server would sanitize to nothing', async () => {
    // Katakana, as code-point escapes: the repo forbids CJK literals in source,
    // and the sanitizer only cares that no character is in [a-z0-9-].
    const nonLatin = '\u30d7\u30ed\u30f3\u30d7\u30c8'
    renderTab()
    fireEvent.click(await screen.findByText('Create New Prompt'))
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'b' } })

    // Non-empty to `.trim()`, empty to the server's sanitizer, so gating on the
    // raw name sent a request that could only 400.
    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: nonLatin } })
    expect(screen.getByText('Create')).toBeDisabled()
    expect(screen.getByText(/has none of them/)).toBeInTheDocument()

    // One character in the allowed set is enough to produce a filename.
    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: `${nonLatin}a` } })
    expect(screen.getByText('Create')).not.toBeDisabled()
    expect(mockApi.createPrompt).not.toHaveBeenCalled()
  })

  it('translates a 400 invalid_name instead of echoing the server English', async () => {
    mockApi.createPrompt.mockRejectedValue(new StubApiError(
      400,
      'invalid prompt name',
      JSON.stringify({ error: 'invalid prompt name', code: 'invalid_name' }),
    ))
    renderTab()
    fireEvent.click(await screen.findByText('Create New Prompt'))
    // A name the client mirror accepts, so the request is actually sent: the
    // mapping has to hold for a name the preview and the server disagree on.
    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: 'ok-name' } })
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'b' } })
    fireEvent.click(screen.getByText('Create'))

    await waitFor(() => expect(screen.getByText(/has none of them/)).toBeInTheDocument())
    expect(screen.queryByText('invalid prompt name')).not.toBeInTheDocument()
  })

  it('surfaces a failed create instead of closing the dialog', async () => {
    mockApi.createPrompt.mockRejectedValue(new Error("prompt 'p' already exists"))
    renderTab()
    fireEvent.click(await screen.findByText('Create New Prompt'))
    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: 'p' } })
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'b' } })
    fireEvent.click(screen.getByText('Create'))

    await waitFor(() => expect(screen.getByText(/already exists/)).toBeInTheDocument())
    expect(screen.getByPlaceholderText('my-prompt-name')).toBeInTheDocument()
  })

  it('translates a 400 name_too_long instead of echoing the server English', async () => {
    mockApi.createPrompt.mockRejectedValue(new StubApiError(
      400,
      'prompt name is too long',
      JSON.stringify({ error: 'prompt name is too long', code: 'name_too_long' }),
    ))
    renderTab()
    fireEvent.click(await screen.findByText('Create New Prompt'))
    // Short enough that the client gate lets it through, so the server's own
    // refusal is what has to be rendered.
    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: 'ok-name' } })
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'b' } })
    fireEvent.click(screen.getByText('Create'))

    await waitFor(() => expect(screen.getByText(/at most 200 bytes/)).toBeInTheDocument())
    expect(screen.queryByText('prompt name is too long')).not.toBeInTheDocument()
  })

  it('keeps Create disabled for a name whose filename would exceed the byte cap', async () => {
    renderTab()
    fireEvent.click(await screen.findByText('Create New Prompt'))
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'b' } })

    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: 'a'.repeat(200) } })
    expect(screen.getByText('Create')).toBeDisabled()
    expect(screen.getByText(/at most 200 bytes/)).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: 'a'.repeat(197) } })
    expect(screen.getByText('Create')).not.toBeDisabled()
    expect(mockApi.createPrompt).not.toHaveBeenCalled()
  })

  it('edits a user prompt, sending the scope it came from', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    renderTab()
    await select('my-prompt')

    fireEvent.click(await screen.findByText('Edit'))
    fireEvent.change(screen.getByDisplayValue('body text'), { target: { value: 'edited body' } })
    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(mockApi.updatePrompt).toHaveBeenCalledWith(
      // The 4th argument is the edit base: the hash the detail read handed out,
      // presented back so the server can refuse a save over someone else's edit.
      'my-prompt', 'global', '---\ndescription: mine\n---\n\nedited body', 'a'.repeat(64),
    ))
  })

  it('carries unmodelled frontmatter through an edit verbatim', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    mockApi.promptDetail.mockResolvedValue({
      content: '---\ndescription: mine\ntags: [a, b]\n---\n\nbody text', redacted: false,
    })
    renderTab()
    await select('my-prompt')

    fireEvent.click(await screen.findByText('Edit'))
    fireEvent.change(screen.getByDisplayValue('body text'), { target: { value: 'new' } })
    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(mockApi.updatePrompt).toHaveBeenCalled())
    expect(mockApi.updatePrompt.mock.calls[0][2]).toContain('tags: [a, b]')
  })

  it('carries a multi-line description through verbatim, not reflowed', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    mockApi.promptDetail.mockResolvedValue({
      content: '---\ndescription: |\n  line one\n  line two\n---\n\nbody text', redacted: false,
    })
    renderTab()
    await select('my-prompt')

    fireEvent.click(await screen.findByText('Edit'))
    // The form does not model a block scalar, so it stays out of the field...
    expect(screen.getByPlaceholderText(/One line shown/)).toHaveValue('')
    fireEvent.change(screen.getByDisplayValue('body text'), { target: { value: 'new body' } })
    fireEvent.click(screen.getByText('Save'))

    // ...and re-emits byte-for-byte instead of being flattened onto one line.
    await waitFor(() => expect(mockApi.updatePrompt).toHaveBeenCalled())
    expect(mockApi.updatePrompt.mock.calls[0][2]).toContain('description: |\n  line one\n  line two')
  })

  it('refuses to edit a copy the server filtered, so the marker is never saved', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    mockApi.promptDetail.mockResolvedValue({
      content: 'key: [REDACTED: credential]', redacted: true,
    })
    renderTab()
    await select('my-prompt')

    expect(screen.getByText(/filtered for safety/)).toBeInTheDocument()
    expect(screen.queryByText(/could not be loaded/)).not.toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /Edit unavailable/ }))
      .toBeDisabled()
    expect(mockApi.updatePrompt).not.toHaveBeenCalled()
  })

  it('refuses to edit a prompt whose bytes are not valid UTF-8', async () => {
    // The served content substitutes U+FFFD for what could not be decoded, so
    // it is a transformation of the file. Saving it would write those
    // replacements over bytes that are still intact on disk — the same hazard
    // as editing a redacted copy, and a different cause, so a different caption.
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    mockApi.promptDetail.mockResolvedValue({
      content: 'caf\ufffd legacy \ufffd bytes', redacted: false, lossy: true,
    })
    renderTab()
    await select('my-prompt')

    expect(screen.getByText(/not valid UTF-8/)).toBeInTheDocument()
    expect(screen.queryByText(/filtered for safety/)).not.toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /Edit unavailable/ }))
      .toBeDisabled()
    expect(mockApi.updatePrompt).not.toHaveBeenCalled()
  })

  it('refuses to edit when the detail fetch failed', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    mockApi.promptDetail.mockRejectedValue(new Error('boom'))
    renderTab()
    await select('my-prompt')

    // A fetch failure says so, rather than implying the file holds a secret.
    expect(screen.getByText(/could not be loaded/)).toBeInTheDocument()
    expect(screen.queryByText(/filtered for safety/)).not.toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /Edit unavailable/ }))
      .toBeDisabled()
  })

  it('offers Retry on a failed read, and a successful retry clears the failure', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    // Reject EVERY read, not just the first: the desktop pane auto-selects
    // the only prompt on mount, which already consumes one load, and the
    // row click that follows is the re-click path, which re-loads on a
    // failed read. A `...Once` rejection would be spent by the auto-select
    // and the click's own reload would succeed before Retry is ever seen.
    mockApi.promptDetail.mockRejectedValue(new Error('boom'))
    renderTab()
    await select('my-prompt')

    // The failed read renders a visible recovery, not just a caption: before,
    // the only retry path was re-clicking the selected row, which nothing
    // taught, so the pane dead-ended until the user re-clicked by accident.
    await waitFor(() => expect(screen.getByText(/could not be loaded/)).toBeInTheDocument())
    const callsBefore = mockApi.promptDetail.mock.calls.length

    // The server recovers; Retry reuses the row re-click's load path: the
    // same detail fetch fires once more, and success clears the failed state.
    mockApi.promptDetail.mockResolvedValue({
      content: '---\ndescription: mine\n---\n\nbody text', redacted: false,
      hash: 'a'.repeat(64),
    })
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(mockApi.promptDetail.mock.calls.length).toBe(callsBefore + 1))
    expect(await screen.findByText(/body text/)).toBeInTheDocument()
    expect(screen.queryByText(/could not be loaded/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })

  it('offers no Retry on a redacted copy, whose re-read returns the same answer', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    mockApi.promptDetail.mockResolvedValue({
      content: 'key: [REDACTED: credential]', redacted: true,
    })
    renderTab()
    await select('my-prompt')

    // Redaction is the server's verdict on the file's CONTENT, not a transient
    // fault: retrying cannot change it, so no button implies it could.
    expect(screen.getByText(/filtered for safety/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })

  it('keeps an open editor intact when the create dialog is opened', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    renderTab()
    await select('my-prompt')

    fireEvent.click(await screen.findByText('Edit'))
    fireEvent.change(screen.getByDisplayValue('body text'), { target: { value: 'draft in progress' } })

    // Create owns separate state: opening it must not reach into the editor.
    fireEvent.click(screen.getByText('Create New Prompt'))
    expect(screen.getByDisplayValue('draft in progress')).toBeInTheDocument()
  })

  it('reads a user prompt scope-qualified, so a shared stem cannot cross over', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    renderTab()
    await select('my-prompt')

    // Scope goes as its own argument; without it the server answers first-match.
    expect(mockApi.promptDetail).toHaveBeenCalledWith('my-prompt', 'global')
  })

  it('reads a package prompt package-qualified and unscoped', async () => {
    mockApi.prompts.mockResolvedValue([PACKAGE_PROMPT])
    renderTab()
    await select('sop')

    expect(mockApi.promptDetail).toHaveBeenCalledWith('Pkg-1.0/sop')
  })

  it('preserves body whitespace the shared parser would trim', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    mockApi.promptDetail.mockResolvedValue({
      content: '---\ndescription: mine\n---\n\n  indented body\n\n', redacted: false,
    })
    renderTab()
    await select('my-prompt')

    fireEvent.click(await screen.findByText('Edit'))
    // Leading indent and trailing blank line both survive into the editor.
    // Asserted on the value directly: getByDisplayValue normalizes whitespace,
    // which is exactly what this test is about.
    expect(screen.getByPlaceholderText(/markdown the agent receives/))
      .toHaveValue('  indented body\n\n')
    fireEvent.click(screen.getByText('Save'))

    // ...and back out to the file unchanged.
    await waitFor(() => expect(mockApi.updatePrompt).toHaveBeenCalled())
    expect(mockApi.updatePrompt.mock.calls[0][2]).toBe('---\ndescription: mine\n---\n\n  indented body\n\n')
  })

  it('never writes two description keys when a block scalar is carried through', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    mockApi.promptDetail.mockResolvedValue({
      content: '---\ndescription: |\n  line one\n  line two\n---\n\nbody text', redacted: false,
    })
    renderTab()
    await select('my-prompt')

    fireEvent.click(await screen.findByText('Edit'))
    // The block scalar sits in the passthrough with the field blank; typing a
    // description must replace it, not stack a second key that YAML then wins.
    fireEvent.change(screen.getByPlaceholderText(/One line shown/), { target: { value: 'typed one' } })
    fireEvent.click(screen.getByText('Save'))

    await waitFor(() => expect(mockApi.updatePrompt).toHaveBeenCalled())
    const written = mockApi.updatePrompt.mock.calls[0][2] as string
    expect(written.match(/^description:/gm)).toHaveLength(1)
    expect(written).toContain('description: typed one')
  })

  it('warns the browser about an unsaved edit on reload or close, and stops once it is gone', async () => {
    // The tab-switch guard only covers exits this shell owns. A reload or a tab
    // close destroys the same draft, and beforeunload is the only thing the
    // platform offers there. Asserted in BOTH directions: a listener that is
    // registered unconditionally would nag on every reload of a clean pane, and
    // one that is never removed would nag for the rest of the session.
    const addSpy = vi.spyOn(window, 'addEventListener')
    const removeSpy = vi.spyOn(window, 'removeEventListener')
    const beforeUnloadAdds = () => addSpy.mock.calls.filter(c => c[0] === 'beforeunload').length
    const beforeUnloadRemoves = () => removeSpy.mock.calls.filter(c => c[0] === 'beforeunload').length

    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    renderTab()
    await select('my-prompt')
    fireEvent.click(await screen.findByText('Edit'))
    // Opening the editor is not itself dirty: the baseline is captured on open.
    expect(beforeUnloadAdds()).toBe(0)
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'unsaved edit' } })
    await waitFor(() => expect(beforeUnloadAdds()).toBe(1))

    // Cancel the edit: nothing is at stake any more, so the warning must go.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(beforeUnloadRemoves()).toBe(1))
    expect(beforeUnloadAdds()).toBe(1)
  })

  it('asks before discarding an unsaved draft when another prompt is selected', async () => {
    const OTHER = { ...USER_PROMPT, name: 'other', fullName: 'other', path: '~/.kiro/prompts/other.md' }
    mockApi.prompts.mockResolvedValue([USER_PROMPT, OTHER])
    renderTab()
    await select('my-prompt')
    fireEvent.click(await screen.findByText('Edit'))
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'abandoned draft' } })

    // Selecting another prompt must ASK before destroying typed work: the row
    // targets are large and a misclick has no undo.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    fireEvent.click(screen.getByRole('option', { name: /other/ }))
    expect(confirmSpy).toHaveBeenCalled()
    // Declined: the draft and its editor survive, and no second read fired.
    expect(screen.getByDisplayValue('abandoned draft')).toBeInTheDocument()
    expect(mockApi.promptDetail).toHaveBeenCalledTimes(1)

    // Accepted: the draft goes and the newly selected prompt is read.
    confirmSpy.mockReturnValue(true)
    fireEvent.click(screen.getByRole('option', { name: /other/ }))
    await waitFor(() => expect(mockApi.promptDetail).toHaveBeenCalledTimes(2))
    expect(screen.queryByDisplayValue('abandoned draft')).not.toBeInTheDocument()
    confirmSpy.mockRestore()
  })

  it('asks before Cancel discards a dirty draft, and closes clean ones silently', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    renderTab()
    await select('my-prompt')
    fireEvent.click(await screen.findByText('Edit'))
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'typed work' } })

    // Cancel sits a pixel from Save and destroys the same draft a row-switch
    // would — so it gets the same guard.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    fireEvent.click(screen.getByText('Cancel'))
    expect(confirmSpy).toHaveBeenCalled()
    expect(screen.getByDisplayValue('typed work')).toBeInTheDocument()

    confirmSpy.mockReturnValue(true)
    fireEvent.click(screen.getByText('Cancel'))
    expect(screen.queryByDisplayValue('typed work')).not.toBeInTheDocument()

    // A zero-edit editor closes without asking: there is nothing to discard,
    // and a confirm the user never earned teaches them to click through it.
    confirmSpy.mockClear()
    fireEvent.click(await screen.findByText('Edit'))
    fireEvent.click(screen.getByText('Cancel'))
    expect(confirmSpy).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('maps a save conflict to prose naming the recovery, not the server error', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    mockApi.updatePrompt.mockRejectedValue(new StubApiError(
      409, 'conflict',
      JSON.stringify({ error: 'the prompt changed on disk after the edit was started', code: 'content_conflict' })))
    renderTab()
    await select('my-prompt')
    fireEvent.click(await screen.findByText('Edit'))
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'my draft body' } })
    fireEvent.click(screen.getByText('Save'))

    // The mapped string, not the wire error: the user never saw a hash, so
    // "changed on disk … close the editor and select the prompt again" is the actionable version.
    await waitFor(() => expect(screen.getByText(/changed on disk/)).toBeInTheDocument())
    // The editor survives: the draft is the one copy of the user's work.
    expect(screen.getByDisplayValue('my draft body')).toBeInTheDocument()

    // While the editor is still open, re-clicking the row must NOT reload:
    // swapping a fresh hash beneath the live draft would let the next Save
    // present a base it was not written against — overwriting the very edit
    // that conflicted.
    expect(mockApi.promptDetail).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('option', { name: /my-prompt/ }))
    expect(mockApi.promptDetail).toHaveBeenCalledTimes(1)

    // The copy says "reopen the prompt": after Cancel, clicking the selected
    // row again must actually reload — a no-op here is an instructed recovery
    // that doesn't work, looping the user back into the same 409.
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    fireEvent.click(screen.getByText('Cancel'))
    confirmSpy.mockRestore()
    fireEvent.click(screen.getByRole('option', { name: /my-prompt/ }))
    await waitFor(() => expect(mockApi.promptDetail).toHaveBeenCalledTimes(2))

    // The fresh read disarms the retry: the pane now shows the file as it is,
    // so a further same-row click is the ordinary no-op again.
    fireEvent.click(screen.getByRole('option', { name: /my-prompt/ }))
    expect(mockApi.promptDetail).toHaveBeenCalledTimes(2)
  })

  it('asks before the create modal discards typed work, and closes empty silently', async () => {
    // A non-empty list keeps the empty state (which carries its own
    // Create New Prompt button) out of the DOM, so the header button
    // is the unique match.
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    renderTab()
    fireEvent.click(await screen.findByText('Create New Prompt'))

    // Empty form: Cancel closes without a confirm the user never earned.
    // (Absence is awaited: the modal unmounts after its exit animation.)
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    fireEvent.click(screen.getByText('Cancel'))
    expect(confirmSpy).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByPlaceholderText('my-prompt-name')).not.toBeInTheDocument())

    // Typed work: the footer Cancel sits beside Create, and unlike the
    // backdrop (guardAccidentalDismiss) it previously discarded silently.
    fireEvent.click(screen.getByText('Create New Prompt'))
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'typed body' } })
    fireEvent.click(screen.getByText('Cancel'))
    expect(confirmSpy).toHaveBeenCalled()
    expect(screen.getByDisplayValue('typed body')).toBeInTheDocument()

    confirmSpy.mockReturnValue(true)
    fireEvent.click(screen.getByText('Cancel'))
    await waitFor(() => expect(screen.queryByDisplayValue('typed body')).not.toBeInTheDocument())
    confirmSpy.mockRestore()
  })

  it('presents the hash a save created when saving again without a fresh read', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    renderTab()
    await select('my-prompt')
    fireEvent.click(await screen.findByText('Edit'))
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'first' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updatePrompt).toHaveBeenCalledTimes(1))
    expect(mockApi.updatePrompt.mock.calls[0][3]).toBe('a'.repeat(64))

    // Second save without re-selecting: the edit base is the state the FIRST
    // save created (returned by the server), not the original read.
    fireEvent.click(await screen.findByText('Edit'))
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'second' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(mockApi.updatePrompt).toHaveBeenCalledTimes(2))
    expect(mockApi.updatePrompt.mock.calls[1][3]).toBe('b'.repeat(64))
  })

  it('deletes only after the confirm prompt is accepted', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderTab()
    await select('my-prompt')

    fireEvent.click(await screen.findByText('Delete'))
    expect(mockApi.deletePrompt).not.toHaveBeenCalled()

    confirmSpy.mockReturnValue(true)
    fireEvent.click(await screen.findByText('Delete'))
    await waitFor(() => expect(mockApi.deletePrompt).toHaveBeenCalledWith('my-prompt', 'global'))
    confirmSpy.mockRestore()
  })

  it('offers no write actions for a source it cannot address', async () => {
    // `source` is free-form on the wire; only the two user directories are
    // writable. An unrecognized value must read, not be cast into a scope.
    mockApi.prompts.mockResolvedValue([{ ...USER_PROMPT, source: 'user' }])
    renderTab()
    await select('my-prompt')

    expect(mockApi.promptDetail).toHaveBeenCalledWith('my-prompt')
    expect(screen.queryByLabelText('Actions')).not.toBeInTheDocument()
  })

  it('offers no action menu on a package prompt', async () => {
    mockApi.prompts.mockResolvedValue([PACKAGE_PROMPT])
    renderTab()
    await select('sop')

    expect(screen.queryByLabelText('Actions')).not.toBeInTheDocument()
    // Still fully readable — the row renders its content, just not editable.
    expect(screen.getByText(/body text/)).toBeInTheDocument()
  })
})

describe('PromptsTab feedback', () => {
  it('translates the no-project refusal into the vocabulary the form uses', async () => {
    // The backend says "no active project for local scope". The control the user
    // chose is labelled "This project" and nothing in this UI says "local", so
    // the raw string names a concept the user never saw.
    mockApi.prompts.mockResolvedValue([])
    mockApi.createPrompt.mockRejectedValue(new StubApiError(
      400,
      'no active project for local scope',
      JSON.stringify({ error: 'no active project for local scope', code: 'no_active_project' }),
    ))
    renderTab()
    fireEvent.click(await screen.findByText('Create New Prompt'))
    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: 'p' } })
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'body' } })
    fireEvent.click(screen.getByRole('radio', { name: 'This project' }))
    fireEvent.click(screen.getByText('Create'))

    expect(await screen.findByText(/nowhere to save/)).toBeInTheDocument()
    expect(screen.queryByText(/local scope/)).not.toBeInTheDocument()
  })

  it('reports a failed delete, which has no form of its own to report in', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    mockApi.deletePrompt.mockRejectedValue(new Error('permission denied'))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderTab()
    await select('my-prompt')

    fireEvent.click(await screen.findByText('Delete'))
    // Before: mutationError rendered only inside the edit form and the create
    // modal, so authorizing a destructive act that failed produced silence.
    await waitFor(() => expect(screen.getByText(/permission denied/)).toBeInTheDocument())
    confirmSpy.mockRestore()
  })

  it('disables Create while the write is in flight, so a double-click cannot 409 itself', async () => {
    let release: (v: unknown) => void = () => {}
    mockApi.createPrompt.mockReturnValue(new Promise(r => { release = r }))
    renderTab()
    fireEvent.click(await screen.findByText('Create New Prompt'))
    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: 'p' } })
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'b' } })

    const create = screen.getByText('Create')
    fireEvent.click(create)
    await waitFor(() => expect(create).toBeDisabled())
    expect(mockApi.createPrompt).toHaveBeenCalledTimes(1)
    release({ ok: true })
  })

  it('shows the list and detail panes side by side, with a selection', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT, PACKAGE_PROMPT])
    renderTab()
    // The first prompt is selected without a click: on a desktop both panes are
    // always on screen, and a blank detail pane reads as a broken tab.
    await waitFor(() => expect(mockApi.promptDetail).toHaveBeenCalled())
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /my-prompt/ })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('option', { name: /sop/ })).toHaveAttribute('aria-selected', 'false')
  })

  it('tells the user when a filter matches nothing', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    renderTab()
    await waitFor(() => expect(mockApi.promptDetail).toHaveBeenCalled())
    fireEvent.change(screen.getByPlaceholderText(/Filter prompts/), { target: { value: 'zzz' } })
    expect(await screen.findByText(/No prompts match/)).toBeInTheDocument()
  })

  it('points the empty state at the Create button, not the filesystem', async () => {
    mockApi.prompts.mockResolvedValue([])
    renderTab()
    expect(await screen.findByText('No prompts yet')).toBeInTheDocument()
    expect(screen.queryByText(/\.kiro\/prompts/)).not.toBeInTheDocument()
  })
})

describe('PromptsTab in-flight writes', () => {
  it('re-reads a prompt on re-selection rather than trusting the cache', async () => {
    // The shared QueryClient sets `staleTime: Infinity`, tuned for data the
    // server pushes invalidations for. Nothing pushes when a prompt file is
    // edited outside the dashboard, so a cache entry that never goes stale
    // would seed the editor from a copy older than the file and Save would
    // write it back. The detail read therefore opts out.
    const OTHER = { ...USER_PROMPT, name: 'other', fullName: 'other', path: '~/.kiro/prompts/other.md' }
    mockApi.prompts.mockResolvedValue([USER_PROMPT, OTHER])
    mockApi.promptDetail.mockResolvedValue({
      content: '---\ndescription: mine\n---\n\nbody text', redacted: false,
    })
    renderTab()
    await select('my-prompt')
    const first = mockApi.promptDetail.mock.calls.length

    fireEvent.click(screen.getByRole('option', { name: /other/ }))
    await waitFor(() => expect(mockApi.promptDetail.mock.calls.length).toBeGreaterThan(first))
    const second = mockApi.promptDetail.mock.calls.length

    // Back to the first prompt: a cached-and-never-stale entry would answer
    // without a request, which is the stale-edit-base hazard.
    fireEvent.click(screen.getByRole('option', { name: /my-prompt/ }))
    await waitFor(() => expect(mockApi.promptDetail.mock.calls.length).toBeGreaterThan(second))
  })

  it('does not let a slow save land its body on a different prompt', async () => {
    const OTHER = { ...USER_PROMPT, name: 'other', fullName: 'other', path: '~/.kiro/prompts/other.md' }
    mockApi.prompts.mockResolvedValue([USER_PROMPT, OTHER])
    let release: (v: unknown) => void = () => {}
    mockApi.updatePrompt.mockReturnValue(new Promise(r => { release = r }))
    renderTab()
    await select('my-prompt')
    fireEvent.click(await screen.findByText('Edit'))
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'A body' } })
    fireEvent.click(screen.getByText('Save'))

    // While that save is in flight the row click is refused, so its completion
    // cannot arrive with a different prompt on screen.
    await waitFor(() => expect(screen.getByText('Cancel')).toBeDisabled())
    fireEvent.click(screen.getByRole('option', { name: /other/ }))
    expect(mockApi.promptDetail).toHaveBeenCalledTimes(1)

    release({ ok: true })
    await waitFor(() => expect(screen.queryByPlaceholderText(/markdown the agent receives/)).not.toBeInTheDocument())
  })

  it('does not call a freshly opened editor dirty when the file is not canonical', async () => {
    // assemble canonicalizes spacing and the post-fence blank line. Comparing
    // against the file's own text made any non-canonical prompt read as dirty
    // with zero edits, so switching rows popped a confirm the user never earned.
    mockApi.prompts.mockResolvedValue([USER_PROMPT, { ...USER_PROMPT, name: 'other', fullName: 'other' }])
    mockApi.promptDetail.mockResolvedValue({
      content: '---\ndescription:   padded out\n---\nno blank line after the fence\n', redacted: false,
    })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderTab()
    await select('my-prompt')
    fireEvent.click(await screen.findByText('Edit'))

    fireEvent.click(screen.getByRole('option', { name: /other/ }))
    expect(confirmSpy).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('retries a failed read when its own row is clicked again', async () => {
    // A failed read leaves the pane saying so with nothing else to click, so
    // re-clicking the row is the only retry a user would try. A same-key no-op
    // there is a dead end reachable on first paint, since the first prompt is
    // auto-selected.
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    mockApi.promptDetail.mockRejectedValueOnce(new Error('boom'))
    renderTab()
    await waitFor(() => expect(screen.getByText(/could not be loaded/)).toBeInTheDocument())
    expect(mockApi.promptDetail).toHaveBeenCalledTimes(1)

    mockApi.promptDetail.mockResolvedValue({ content: 'recovered body', redacted: false })
    fireEvent.click(screen.getByRole('option', { name: /my-prompt/ }))
    await waitFor(() => expect(screen.getByText('recovered body')).toBeInTheDocument())
    expect(screen.queryByText(/could not be loaded/)).not.toBeInTheDocument()
  })

  it('leaves a dirty editor alone when its own row is clicked again', async () => {
    mockApi.prompts.mockResolvedValue([USER_PROMPT])
    renderTab()
    await select('my-prompt')
    fireEvent.click(await screen.findByText('Edit'))
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'in progress' } })

    // Re-selecting the SAME prompt is not a selection change. The dirty guard
    // only fires on a change, so without a same-key short-circuit this click
    // would tear the editor down with no confirm at all.
    fireEvent.click(screen.getByRole('option', { name: /my-prompt/ }))
    expect(screen.getByDisplayValue('in progress')).toBeInTheDocument()
  })

  it('never leaves one prompt editable while another is being loaded', async () => {
    const OTHER = { ...USER_PROMPT, name: 'other', fullName: 'other', path: '~/.kiro/prompts/other.md' }
    mockApi.prompts.mockResolvedValue([USER_PROMPT, OTHER])
    renderTab()
    await select('my-prompt')
    expect(screen.getByText(/body text/)).toBeInTheDocument()

    // Hold the second read open. The header already names the new prompt, so
    // the old content must not still be sitting there editable — Edit would
    // seed from it and Save would write it under the new prompt's name.
    let release: (v: unknown) => void = () => {}
    mockApi.promptDetail.mockReturnValue(new Promise(r => { release = r }))
    fireEvent.click(screen.getByRole('option', { name: /other/ }))

    await waitFor(() => expect(screen.queryByText(/body text/)).not.toBeInTheDocument())
    // While the read is in flight there is no Edit control at all — not a
    // disabled "Edit unavailable", which asserts a fact about the FILE the
    // system doesn't hold yet. The loading placeholder stands in its place.
    expect(screen.queryByRole('button', { name: /Edit/ })).not.toBeInTheDocument()
    expect(screen.getByText('Loading prompt…')).toBeInTheDocument()

    release({ content: 'other body', redacted: false, hash: 'c'.repeat(64) })
    await waitFor(() => expect(screen.getByText('other body')).toBeInTheDocument())
    expect(screen.queryByText('Loading prompt…')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit' })).toBeEnabled()
  })

  it('keeps the create dialog open while its POST is in flight', async () => {
    let release: (v: unknown) => void = () => {}
    mockApi.createPrompt.mockReturnValue(new Promise(r => { release = r }))
    renderTab()
    fireEvent.click(await screen.findByText('Create New Prompt'))
    fireEvent.change(screen.getByPlaceholderText('my-prompt-name'), { target: { value: 'p' } })
    fireEvent.change(screen.getByPlaceholderText(/markdown the agent receives/), { target: { value: 'b' } })
    fireEvent.click(screen.getByText('Create'))

    await waitFor(() => expect(screen.getByText('Cancel')).toBeDisabled())
    expect(screen.getByPlaceholderText('my-prompt-name')).toBeInTheDocument()
    release({ ok: true })
  })
})

describe('parsePromptContent — a block-scalar indicator is not a description', () => {
  // parseFrontmatter maps a bare `|`/`>` to '', but a CHOMPED indicator arrives
  // as its literal two characters. Modelling either one would write
  // `description: >-` back with every continuation line dropped.
  for (const ind of ['>-', '|+', '|-', '>+', '|', '>']) {
    it(`preserves a ${ind} description block verbatim`, () => {
      const raw = `---\ndescription: ${ind}\n  first line\n  second line\n---\n\n# Body\n`
      const data = parsePromptContent(raw, 'p', 'global')
      // Not modelled: the single-line input cannot hold it, so it stays raw.
      expect(data.description).toBe('')
      expect(assemblePromptContent(data)).toContain(`description: ${ind}`)
      expect(assemblePromptContent(data)).toContain('second line')
    })
  }
})

describe('parsePromptContent — leading "---" is not always frontmatter', () => {
  it('keeps a thematic rule as body when no field was parsed', () => {
    // A prompt opening with a horizontal rule has the same shape as
    // frontmatter but declares no fields. Treating it as frontmatter would
    // drop everything above the second "---" on the next Save.
    const raw = '---\nDo this first\n---\nContinue\n'
    const data = parsePromptContent(raw, 'p', 'global')
    expect(data.body).toBe(raw)
    expect(data.description).toBe('')
    // No frontmatter was recognized at all, so there is no block to edit.
    expect(data.frontmatter).toBeUndefined()
    // Round-trip is byte-exact: nothing is lost by opening and saving.
    expect(assemblePromptContent(data)).toBe(raw)
  })

  it('still strips a real frontmatter block', () => {
    const raw = '---\ndescription: One line\n---\n\n# Body\n'
    const data = parsePromptContent(raw, 'p', 'global')
    expect(data.description).toBe('One line')
    expect(data.body).toBe('# Body\n')
  })
})

describe('assemblePromptContent — the frontmatter block is the author\'s text', () => {
  // Rebuilding frontmatter from parsed fields normalizes away everything the
  // model does not carry. Editing the block in place instead makes the whole
  // class of losses impossible rather than fixing them one shape at a time.
  const round = (raw: string, description?: string) => {
    const d = parsePromptContent(raw, 'p', 'global')
    return assemblePromptContent(description === undefined ? d : { ...d, description })
  }

  it('keeps comments, blank lines, key order and quoting untouched', () => {
    const raw = [
      '---',
      '# who maintains this',
      'owner: platform',
      '',
      "tags: ['a', 'b']",
      'description: one line',
      '---',
      '',
      '# Body',
      '',
    ].join('\n')
    expect(round(raw)).toBe(raw)
  })

  it('round-trips a body that itself begins with a --- rule', () => {
    // The fence regex is lazy, so it closes at the FIRST following `---`. With
    // the separator now carried rather than synthesized, a body starting with
    // `---` and no blank line after the fence puts three fence-shaped lines in
    // a row — this asserts the second parse still splits them the same way.
    const raw = '---\ndescription: x\n---\n---\nBody after a rule\n'
    expect(round(raw)).toBe(raw)
    const once = round(raw, 'edited')
    expect(once).toBe('---\ndescription: edited\n---\n---\nBody after a rule\n')
    // Stable under a second pass: the body is still the body.
    expect(parsePromptContent(once, 'p', 'global').body).toBe('---\nBody after a rule\n')
  })

  it('keeps CRLF fences on a single-field block whose body has no newline', () => {
    // The two cases content-inference cannot see: a one-field block has no
    // internal newline, and a body with no newline has none either. Taking the
    // ending from the file's own fence covers both.
    const raw = '---\r\ndescription: x\r\n---\r\n\r\nBody'
    expect(round(raw)).toBe(raw)
    const out = round(raw, 'edited')
    expect(out).toBe('---\r\ndescription: edited\r\n---\r\n\r\nBody')
    expect(out).not.toMatch(/[^\r]\n/)
  })

  it('does not invent a blank line after the fence', () => {
    // The blank line after the closing fence is a convention, not a
    // requirement. A file written without it must not acquire one just because
    // it was opened and saved — with or without an actual description edit.
    const raw = '---\ndescription: x\n---\nBody starts immediately\n'
    expect(round(raw)).toBe(raw)
    expect(round(raw, 'edited'))
      .toBe('---\ndescription: edited\n---\nBody starts immediately\n')
  })

  it('consumes the whole closing-fence line and preserves its tail', () => {
    // The reader closes frontmatter at any line STARTING with `---`, so a
    // `---junk` closer is a closer. Matching only the three dashes would leak
    // `junk` into the body on Save; carrying the closer keeps the no-edit
    // round-trip byte-exact instead of normalizing the author's line.
    const raw = '---\ndescription: x\n---junk\nBody\n'
    const d = parsePromptContent(raw, 'p', 'global')
    expect(d.frontmatter).toBe('description: x')
    expect(d.body).toBe('Body\n')
    expect(d.body).not.toContain('junk')
    expect(round(raw)).toBe(raw)
    expect(round(raw, 'edited')).toBe('---\ndescription: edited\n---junk\nBody\n')
  })

  it('keeps the blank line when the file had one', () => {
    const raw = '---\ndescription: x\n---\n\nBody after a blank line\n'
    expect(round(raw)).toBe(raw)
    expect(round(raw, 'edited'))
      .toBe('---\ndescription: edited\n---\n\nBody after a blank line\n')
  })

  it('is identity when the description was not edited', () => {
    // A save with no description edit must return the block byte-for-byte.
    // Otherwise a duplicate field gets collapsed, and spacing normalized, on a
    // save the user made without touching the field.
    const raw = '---\ndescription: first\nowner: me\ndescription:   second\n---\n\nBody\n'
    expect(round(raw)).toBe(raw)
    // Both occurrences survive; only an actual edit may collapse them.
    expect(round(raw).match(/description/g)).toHaveLength(2)
  })

  it('is identity for an unedited block with odd spacing and comments', () => {
    const raw = '---\n# note\ndescription :   padded\n\nowner: me\n---\n\nBody\n'
    expect(round(raw)).toBe(raw)
  })

  it('rewrites only the description line when it changes', () => {
    const raw = '---\n# note\nowner: me\ndescription: old\n---\n\nBody\n'
    expect(round(raw, 'new')).toBe('---\n# note\nowner: me\ndescription: new\n---\n\nBody\n')
  })

  it('drops the field when the description is cleared, keeping the rest', () => {
    const raw = '---\nowner: me\ndescription: old\n---\n\nBody\n'
    expect(round(raw, '')).toBe('---\nowner: me\n---\n\nBody\n')
  })

  it('falls back to a bare body when clearing empties the block', () => {
    expect(round('---\ndescription: only\n---\n\nBody\n', '')).toBe('Body\n')
  })

  it('replaces a block scalar and its continuation lines when one value is typed', () => {
    // Emitting both would leave two `description:` keys, and YAML last-key-wins
    // would silently pick one of them.
    const raw = '---\ndescription: >-\n  folded\n  lines\nowner: me\n---\n\nBody\n'
    const out = round(raw, 'typed')
    expect(out).toBe('---\ndescription: typed\nowner: me\n---\n\nBody\n')
    expect(out.match(/^description:/gm)).toHaveLength(1)
  })

  it('consumes a block scalar containing blank lines, orphaning nothing', () => {
    // A blank line INSIDE a block scalar belongs to it. A removal loop that
    // stops at the first blank leaves the rest of the scalar behind as
    // orphaned frontmatter lines.
    const raw = '---\ndescription: |\n  first\n\n  second\nowner: me\n---\n\nBody\n'
    const out = round(raw, 'typed')
    expect(out).toBe('---\ndescription: typed\nowner: me\n---\n\nBody\n')
    expect(out).not.toContain('second')
  })

  it('consumes the trailing blank the reader folds into the scalar', () => {
    // The backend's parse_frontmatter takes every blank-or-indented line after
    // an indicator as part of the scalar, so the editor takes the same span.
    // Diverging would leave a line the reader thinks it already consumed.
    const raw = '---\ndescription: |\n  only\n\nowner: me\n---\n\nBody\n'
    expect(round(raw, 'typed')).toBe('---\ndescription: typed\nowner: me\n---\n\nBody\n')
  })

  it('recognizes the field the way the reader does: space before the colon', () => {
    // parse_frontmatter partitions at the first colon and trims the key, so
    // `description : x` IS the description field. Matching only `description:`
    // would edit nothing and leave the old value in effect.
    const raw = '---\ndescription : old\n---\n\nBody\n'
    expect(parsePromptContent(raw, 'p', 'global').description).toBe('old')
    expect(round(raw, 'new')).toBe('---\ndescription: new\n---\n\nBody\n')
  })

  it('collapses duplicate description fields to the one the reader uses', () => {
    // The reader's last-key-wins means the second is the effective value.
    // Leaving the first behind would keep stale metadata a later read could take.
    const raw = '---\ndescription: first\nowner: me\ndescription: second\n---\n\nBody\n'
    expect(parsePromptContent(raw, 'p', 'global').description).toBe('second')
    const out = round(raw, 'only one')
    expect(out).toBe('---\nowner: me\ndescription: only one\n---\n\nBody\n')
    expect(out.match(/description/g)).toHaveLength(1)
  })

  it('reads and rewrites a CRLF-authored prompt without mixing line endings', () => {
    const raw = '---\r\ndescription: old\r\nowner: me\r\n---\r\n\r\nBody\r\n'
    expect(parsePromptContent(raw, 'p', 'global').description).toBe('old')
    const out = round(raw, 'new')
    expect(out).toBe('---\r\ndescription: new\r\nowner: me\r\n---\r\n\r\nBody\r\n')
    expect(out).not.toMatch(/[^\r]\n/)
  })

  it('does not swallow an indented line following a PLAIN description', () => {
    // A block scalar owns the indented lines after it; a plain field does not.
    // Applying the scalar span to a plain field deletes the author's next line.
    const raw = '---\ndescription: plain\n  # a note the reader ignores\nowner: me\n---\n\nBody\n'
    expect(round(raw, 'edited'))
      .toBe('---\ndescription: edited\n  # a note the reader ignores\nowner: me\n---\n\nBody\n')
    // Clearing it removes only its own line, too.
    expect(round(raw, ''))
      .toBe('---\n  # a note the reader ignores\nowner: me\n---\n\nBody\n')
  })

  it('takes line endings from the body, so a CRLF body never gets LF fences', () => {
    // The fences are joined onto the body. Deriving them from the frontmatter
    // block instead can mix endings when only one side is CRLF.
    const data = parsePromptContent('Body line\r\n', 'p', 'global')
    const out = assemblePromptContent({ ...data, description: 'added' })
    expect(out).toBe('---\r\ndescription: added\r\n---\r\n\r\nBody line\r\n')
    expect(out).not.toMatch(/[^\r]\n/)
  })

  it('adds frontmatter to a bare prompt only when a description is given', () => {
    expect(round('Just body.\n')).toBe('Just body.\n')
    expect(round('Just body.\n', 'now described'))
      .toBe('---\ndescription: now described\n---\n\nJust body.\n')
  })
})
