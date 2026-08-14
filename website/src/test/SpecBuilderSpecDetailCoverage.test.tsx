// Coverage for SpecDetail — the spec workspace shell. Exercises the parts the
// existing focused tests (chat gating, execute guard) leave untouched: the
// header identity row, the persisted + draggable docs split, keyboard resize,
// the fullscreen review overlay and its Esc handler, the phase-gated
// approve/pause actions, the stacked review-comment tray, and fetch-error
// surfacing.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// The embedded chat, the document renderer and the state panel are all covered
// by their own tests; stubbing them keeps this file's assertions about
// SpecDetail's own behaviour. The DocView stub exposes the selection-to-comment
// callback as plain buttons so the tray can be driven deterministically.
vi.mock('../apps/spec-builder/components/ChatColumn', () => ({
  default: ({ name, slotKey, onSend }: {
    name: string
    slotKey?: string
    onSend: (msg: string) => Promise<unknown>
  }) => (
    <div data-testid="chat-column" data-name={name} data-slot={slotKey ?? ''}>
      <button type="button" data-testid="chat-send" onClick={() => { void onSend('typed in the chat') }}>
        send
      </button>
    </div>
  ),
}))

vi.mock('../apps/spec-builder/components/DocView', () => ({
  default: ({ tab, running, addComment }: {
    tab: string
    running?: boolean
    addComment: (c: { file: string; quote: string; note: string }) => void
  }) => (
    <div data-testid="doc-view" data-tab={tab} data-running={String(!!running)}>
      <button
        type="button"
        data-testid="add-requirements-comment"
        onClick={() => addComment({ file: 'requirements.md', quote: 'the system shall log', note: 'name the log' })}
      >
        add req
      </button>
      <button
        type="button"
        data-testid="add-design-comment"
        onClick={() => addComment({ file: 'design.md', quote: 'a single module', note: 'split it' })}
      >
        add design
      </button>
    </div>
  ),
}))

vi.mock('../apps/spec-builder/components/SpecStatePanel', () => ({
  default: ({ sendMessage }: { sendMessage: (msg: string) => Promise<unknown> }) => (
    <button type="button" data-testid="state-send" onClick={() => { void sendMessage('Decision: one') }}>
      answer
    </button>
  ),
}))

import SpecDetail from '../apps/spec-builder/components/SpecDetail'
import { LS } from '../apps/spec-builder/api'

interface Call { url: string; method: string; body: string }

let queryClient: QueryClient
let calls: Call[]

const BASE = {
  name: 'checkout',
  phase: 'requirements',
  status: 'planning',
  running: false,
  working_dir: '/proj/checkout',
  spec_dir: '/proj/checkout/.kiro/specs/checkout',
  slot_key: 'spec-builder-checkout-99',
  files: { 'requirements.md': '# r' },
}

const okRes = (text: string) => ({ ok: true, status: 200, text: () => Promise.resolve(text) })

/** Stub fetch: GET answers with `detail`, writes are recorded. `onWrite` may
 *  return a promise the test controls, to hold a mutation in flight. */
function installFetch(
  detail: Record<string, unknown>,
  onWrite?: (url: string) => Promise<unknown> | undefined,
) {
  vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const method = init?.method || 'GET'
    if (method !== 'GET') {
      calls.push({ url, method, body: String(init?.body ?? '') })
      const held = onWrite?.(url)
      if (held) return held
      return Promise.resolve(okRes('{"ok":true}'))
    }
    return Promise.resolve(okRes(JSON.stringify(detail)))
  }))
}

function renderDetail(name = 'checkout', setErr: (m: string) => void = () => {}) {
  return render(
    <QueryClientProvider client={queryClient}>
      <SpecDetail name={name} setErr={setErr} />
    </QueryClientProvider>,
  )
}

/** Pick a document tab regardless of whether SegmentedControl collapsed to its
 *  dropdown (it does under a zero-width test layout). */
async function selectTab(label: string) {
  let options = screen.queryAllByRole('button', { name: label })
  if (!options.length) {
    fireEvent.click(screen.getAllByRole('button', { name: /Requirements|Design|Tasks/ })[0])
    options = await screen.findAllByRole('button', { name: label })
  }
  fireEvent.click(options[options.length - 1])
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  calls = []
  localStorage.clear()
  // The pulsing dots and the poll both schedule work; without fake timers a
  // callback can fire after teardown and throw as an unhandled error.
  vi.useFakeTimers({ shouldAdvanceTime: true })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('SpecDetail header', () => {
  it('renders the spec identity, phase pill and working directory', async () => {
    installFetch(BASE)
    renderDetail()

    expect(await screen.findByTestId('chat-column')).toHaveAttribute('data-slot', 'spec-builder-checkout-99')
    expect(screen.getByText('requirements')).toBeInTheDocument()
    expect(screen.getByTitle('/proj/checkout')).toBeInTheDocument()
    // Not running: no activity indicator, and with no tasks.md no build action.
    expect(screen.queryByText('working…')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Start building/i })).not.toBeInTheDocument()
  })

  it('announces agent activity while the spec is running', async () => {
    installFetch({ ...BASE, running: true })
    renderDetail()

    expect(await screen.findByText('working…')).toBeInTheDocument()
    expect(screen.getByTestId('doc-view')).toHaveAttribute('data-running', 'true')
  })

  it('shows the building label and a Pause action while executing', async () => {
    installFetch({ ...BASE, status: 'executing', files: { 'requirements.md': '# r', 'tasks.md': '- [ ] one' } })
    renderDetail()

    expect(await screen.findByText('building')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Pause/ })).toBeInTheDocument()
    // Executing hides both the approval and the build affordances.
    expect(screen.queryByRole('button', { name: /Approve/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Start building/i })).not.toBeInTheDocument()
  })
})

describe('SpecDetail docs split', () => {
  it('restores a persisted split width', async () => {
    localStorage.setItem(LS.docPct, '60')
    installFetch(BASE)
    renderDetail()

    await screen.findByTestId('doc-view')
    expect(screen.getByRole('separator')).toHaveAttribute('aria-valuenow', '60')
  })

  it('falls back to the default when the persisted width is corrupt', async () => {
    localStorage.setItem(LS.docPct, 'not-a-number')
    installFetch(BASE)
    renderDetail()

    await screen.findByTestId('doc-view')
    expect(screen.getByRole('separator')).toHaveAttribute('aria-valuenow', '44')
  })

  it('resizes with the arrow keys and persists the result', async () => {
    installFetch(BASE)
    renderDetail()

    await screen.findByTestId('doc-view')
    const divider = screen.getByRole('separator')

    fireEvent.keyDown(divider, { key: 'ArrowLeft' })
    expect(divider).toHaveAttribute('aria-valuenow', '48')
    expect(localStorage.getItem(LS.docPct)).toBe('48')

    fireEvent.keyDown(divider, { key: 'ArrowRight' })
    expect(divider).toHaveAttribute('aria-valuenow', '44')

    // Any other key is left to the browser.
    fireEvent.keyDown(divider, { key: 'Enter' })
    expect(divider).toHaveAttribute('aria-valuenow', '44')
  })

  it('drags within its clamp and releases the cursor lock on mouse up', async () => {
    installFetch(BASE)
    renderDetail()

    await screen.findByTestId('doc-view')
    const divider = screen.getByRole('separator')

    fireEvent.mouseDown(divider)
    expect(document.body.style.cursor).toBe('col-resize')

    fireEvent.mouseMove(window, { clientX: 100 })
    expect(divider).toHaveAttribute('aria-valuenow', '25')

    fireEvent.mouseUp(window)
    expect(document.body.style.cursor).toBe('')

    // Listeners are gone: a later move must not move the divider.
    fireEvent.mouseMove(window, { clientX: 400 })
    expect(divider).toHaveAttribute('aria-valuenow', '25')
  })

  it('switches the rendered document when another tab is picked', async () => {
    installFetch(BASE)
    renderDetail()

    expect(await screen.findByTestId('doc-view')).toHaveAttribute('data-tab', 'requirements')
    await selectTab('Tasks')
    await waitFor(() => expect(screen.getByTestId('doc-view')).toHaveAttribute('data-tab', 'tasks'))
  })
})

describe('SpecDetail review overlay', () => {
  it('opens the fullscreen review and closes it with Escape', async () => {
    installFetch(BASE)
    renderDetail()

    await screen.findByTestId('doc-view')
    fireEvent.click(screen.getByRole('button', { name: 'Expand document for review' }))

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    // The overlay mounts its own copy of the document pane.
    expect(screen.getAllByTestId('doc-view')).toHaveLength(2)

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('closes the fullscreen review from its own close button', async () => {
    installFetch(BASE)
    renderDetail()

    await screen.findByTestId('doc-view')
    fireEvent.click(screen.getByRole('button', { name: 'Expand document for review' }))
    await screen.findByRole('dialog')

    fireEvent.click(screen.getByRole('button', { name: 'Close review view' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    // An unrelated key while collapsed is a no-op rather than a crash.
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

describe('SpecDetail phase actions', () => {
  it('sends the phase-approval instruction and locks the button while in flight', async () => {
    let release: (() => void) | undefined
    installFetch(BASE, (url) => (url.includes('/message')
      ? new Promise((res) => { release = () => res(okRes('{"ok":true}')) })
      : undefined))
    renderDetail()

    const approve = await screen.findByRole('button', { name: /Approve → Design/ })
    fireEvent.click(approve)

    await waitFor(() => expect(screen.getByRole('button', { name: /Sending/ })).toBeDisabled())
    const sent = calls.filter((c) => c.url.includes('/message'))
    expect(sent).toHaveLength(1)
    expect(JSON.parse(sent[0].body).text).toContain('Requirements approved')

    release?.()
    // It must NOT spring back to "Approve → Design". The phase is derived from
    // the documents on disk, so it stays 'requirements' until the agent has
    // written design.md — showing the approval button again in that window read
    // as "nothing happened" and invited a second approval into the same turn.
    await waitFor(() => expect(screen.getByRole('button', { name: /Drafting design/ })).toBeDisabled())
    expect(screen.queryByRole('button', { name: /Approve → Design/ })).not.toBeInTheDocument()
  })

  it('switches to the document being drafted and restores the button once the phase moves', async () => {
    let detail: Record<string, unknown> = { ...BASE }
    vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const method = init?.method || 'GET'
      if (method !== 'GET') {
        calls.push({ url, method, body: String(init?.body ?? '') })
        return Promise.resolve(okRes('{"ok":true}'))
      }
      return Promise.resolve(okRes(JSON.stringify(detail)))
    }))
    renderDetail()

    expect((await screen.findByTestId('doc-view')).dataset.tab).toBe('requirements')
    fireEvent.click(await screen.findByRole('button', { name: /Approve → Design/ }))

    // The approved document is done; the one being written is what to watch.
    await waitFor(() => expect(screen.getByTestId('doc-view').dataset.tab).toBe('design'))
    await screen.findByRole('button', { name: /Drafting design/ })

    // Once design.md lands the backend reports the new phase, and the control
    // becomes the next approval rather than staying stuck on "drafting".
    detail = { ...BASE, phase: 'design', files: { 'requirements.md': '# r', 'design.md': '# d' } }
    await waitFor(
      () => expect(screen.getByRole('button', { name: /Approve → Tasks/ })).toBeEnabled(),
      { timeout: 4000 },
    )
  })

  it('keeps the approval state its own when another message is sent mid-flight', async () => {
    // The decision tray, the review tray and the approval all share ONE
    // mutation. mutate()'s per-call callbacks live on the observer, so the
    // second send used to REPLACE the approval's — leaving the control labelled
    // "Sending…" while isPending went false underneath it, i.e. enabled and able
    // to queue a duplicate approval turn.
    let releaseApproval: (() => void) | undefined
    let messages = 0
    vi.stubGlobal('fetch', vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      const method = init?.method || 'GET'
      if (method !== 'GET') {
        calls.push({ url, method, body: String(init?.body ?? '') })
        if (url.includes('/message')) {
          messages += 1
          // Hold only the FIRST message (the approval); the decision answer that
          // follows resolves immediately, which is what displaces the callbacks.
          if (messages === 1) {
            return new Promise((res) => { releaseApproval = () => res(okRes('{"ok":true}')) })
          }
        }
        return Promise.resolve(okRes('{"ok":true}'))
      }
      return Promise.resolve(okRes(JSON.stringify(BASE)))
    }))
    renderDetail()

    fireEvent.click(await screen.findByRole('button', { name: /Approve → Design/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Sending/ })).toBeDisabled())

    // A decision answered while the approval is still in flight.
    fireEvent.click(screen.getByTestId('state-send'))
    await waitFor(() => expect(calls.filter((c) => c.url.includes('/message'))).toHaveLength(2))

    releaseApproval?.()

    await waitFor(() => expect(screen.getByRole('button', { name: /Drafting design/ })).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Drafting design/ })).toBeDisabled()
    expect(screen.queryByRole('button', { name: /Sending/ })).not.toBeInTheDocument()
  })

  it('offers the tasks approval on the design phase', async () => {
    installFetch({ ...BASE, phase: 'design', files: { 'requirements.md': '# r', 'design.md': '# d' } })
    renderDetail()

    fireEvent.click(await screen.findByRole('button', { name: /Approve → Tasks/ }))
    await waitFor(() => expect(calls.filter((c) => c.url.includes('/message'))).toHaveLength(1))
    expect(JSON.parse(calls[0].body).text).toContain('Design approved')
  })

  it('offers no approval for a phase the table does not know', async () => {
    installFetch({ ...BASE, phase: 'archived' })
    renderDetail()

    await screen.findByTestId('doc-view')
    expect(screen.queryByRole('button', { name: /Approve/ })).not.toBeInTheDocument()
    expect(screen.getByText('archived')).toBeInTheDocument()
  })

  it('pauses a running build and disables the control while stopping', async () => {
    let release: (() => void) | undefined
    installFetch(
      { ...BASE, status: 'executing', files: { 'requirements.md': '# r', 'tasks.md': '- [ ] one' } },
      (url) => (url.includes('/stop')
        ? new Promise((res) => { release = () => res(okRes('{"ok":true}')) })
        : undefined),
    )
    renderDetail()

    fireEvent.click(await screen.findByRole('button', { name: /Pause/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Pausing/ })).toBeDisabled())
    expect(calls.filter((c) => c.url.includes('/stop'))).toHaveLength(1)
    expect(JSON.parse(calls[0].body)).toEqual({
      spec_dir: '/proj/checkout/.kiro/specs/checkout',
      slot_key: 'spec-builder-checkout-99',
    })

    release?.()
    await waitFor(() => expect(screen.getByRole('button', { name: /Pause/ })).not.toBeDisabled())
  })

  it('routes a state-panel answer through the shared message mutation', async () => {
    installFetch(BASE)
    renderDetail()

    fireEvent.click(await screen.findByTestId('state-send'))
    await waitFor(() => expect(calls.filter((c) => c.url.includes('/message'))).toHaveLength(1))
    expect(JSON.parse(calls[0].body).text).toBe('Decision: one')
  })

  it('routes a chat message through the shared message mutation', async () => {
    installFetch(BASE)
    renderDetail()

    fireEvent.click(await screen.findByTestId('chat-send'))
    await waitFor(() => expect(calls.filter((c) => c.url.includes('/message'))).toHaveLength(1))
    expect(JSON.parse(calls[0].body).text).toBe('typed in the chat')
  })

  it('hands the task list off to a build agent', async () => {
    installFetch({ ...BASE, phase: 'tasks', files: { 'requirements.md': '# r', 'tasks.md': '- [ ] one' } })
    renderDetail()

    fireEvent.click(await screen.findByRole('button', { name: /Start building/i }))
    await waitFor(() => expect(calls.filter((c) => c.url.includes('/execute'))).toHaveLength(1))
    expect(JSON.parse(calls[0].body).slot_key).toBe('spec-builder-checkout-99')
  })

  it('surfaces a refused handoff', async () => {
    const setErr = vi.fn()
    installFetch(
      { ...BASE, phase: 'tasks', files: { 'requirements.md': '# r', 'tasks.md': '- [ ] one' } },
      (url) => (url.includes('/execute')
        ? Promise.resolve({ ok: false, status: 409, json: () => Promise.resolve({ error: 'spec moved' }) })
        : undefined),
    )
    renderDetail('checkout', setErr)

    fireEvent.click(await screen.findByRole('button', { name: /Start building/i }))
    await waitFor(() => expect(setErr).toHaveBeenCalledWith('spec moved'), { timeout: 5_000 })
    // The control comes back so the user can retry once the identity is fresh.
    await waitFor(() => expect(screen.getByRole('button', { name: /Start building/i })).not.toBeDisabled())
  })

  it('surfaces a refused pause', async () => {
    const setErr = vi.fn()
    installFetch(
      { ...BASE, status: 'executing', files: { 'requirements.md': '# r', 'tasks.md': '- [ ] one' } },
      (url) => (url.includes('/stop')
        ? Promise.resolve({ ok: false, status: 409, json: () => Promise.resolve({ error: 'nothing running' }) })
        : undefined),
    )
    renderDetail('checkout', setErr)

    fireEvent.click(await screen.findByRole('button', { name: /Pause/ }))
    await waitFor(() => expect(setErr).toHaveBeenCalledWith('nothing running'), { timeout: 5_000 })
  })
})

describe('SpecDetail review comment tray', () => {
  it('stacks comments, removes one, and clears the rest', async () => {
    installFetch(BASE)
    renderDetail()

    await screen.findByTestId('doc-view')
    expect(screen.queryByRole('button', { name: 'Clear' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('add-requirements-comment'))
    expect(await screen.findByText('1 pending comment')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('add-design-comment'))
    expect(await screen.findByText('2 pending comments')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Remove comment on design.md' }))
    expect(await screen.findByText('1 pending comment')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove comment on design.md' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(screen.queryByText('1 pending comment')).not.toBeInTheDocument())
    // Nothing stacked: sending is a no-op with no request behind it.
    expect(calls.filter((c) => c.url.includes('/message'))).toHaveLength(0)
  })

  it('sends every stacked comment as one grouped message and empties the tray', async () => {
    installFetch(BASE)
    renderDetail()

    await screen.findByTestId('doc-view')
    fireEvent.click(screen.getByTestId('add-requirements-comment'))
    fireEvent.click(screen.getByTestId('add-design-comment'))
    await screen.findByText('2 pending comments')

    fireEvent.click(screen.getByRole('button', { name: /Send all to agent/ }))

    await waitFor(() => expect(calls.filter((c) => c.url.includes('/message'))).toHaveLength(1))
    const text = JSON.parse(calls[0].body).text as string
    expect(text).toContain('Review feedback on the spec documents')
    expect(text).toContain('## requirements.md')
    expect(text).toContain('## design.md')
    expect(text).toContain('1. Regarding this passage')
    expect(text).toContain('the system shall log')
    expect(text).toContain('split it')

    await waitFor(() => expect(screen.queryByText('2 pending comments')).not.toBeInTheDocument())
  })

  it('keeps the stack when the send fails', async () => {
    const setErr = vi.fn()
    installFetch(BASE, (url) => (url.includes('/message')
      ? Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({ error: 'agent busy' }) })
      : undefined))
    renderDetail('checkout', setErr)

    await screen.findByTestId('doc-view')
    fireEvent.click(screen.getByTestId('add-requirements-comment'))
    await screen.findByText('1 pending comment')

    fireEvent.click(screen.getByRole('button', { name: /Send all to agent/ }))

    await waitFor(() => expect(setErr).toHaveBeenCalledWith('agent busy'), { timeout: 5_000 })
    // The comment survives so the review can be retried.
    expect(screen.getByText('1 pending comment')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: /Send all to agent/ })).not.toBeDisabled())
  })
})

describe('SpecDetail error surfacing', () => {
  it('reports a failed detail fetch without clearing the pane', async () => {
    const setErr = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: () => Promise.resolve({ error: 'spec index unavailable' }),
    }))

    renderDetail('checkout', setErr)

    await waitFor(() => expect(setErr).toHaveBeenCalledWith('spec index unavailable'), { timeout: 5_000 })
    // No detail: the chat stays withheld and the phase pill falls back.
    expect(screen.queryByTestId('chat-column')).not.toBeInTheDocument()
    expect(screen.getByText('…')).toBeInTheDocument()
  })
})
