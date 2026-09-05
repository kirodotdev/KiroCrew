// Coverage for SpecDetail — the spec workspace shell. Exercises the parts the
// existing focused tests (chat gating, execute guard) leave untouched: the
// header identity row, the persisted + draggable docs split, keyboard resize,
// the fullscreen review overlay and its Esc handler, the phase-gated
// approve/pause actions, the stacked review-comment tray, and fetch-error
// surfacing.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

let mobile = false

vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobile }))

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
      <input data-testid="doc-comment-draft" aria-label="comment draft" />
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
  default: ({ answerDecision }: { answerDecision: (id: string, option: string, msg: string) => Promise<unknown> }) => (
    <button type="button" data-testid="state-send" onClick={() => { void answerDecision('transport', 'one', 'Decision: one') }}>
      answer
    </button>
  ),
}))

import SpecDetail from '../apps/spec-builder/components/SpecDetail'
import { LS, SPEC_DETAIL_FAST_POLL_MS, SPEC_DETAIL_IDLE_POLL_MS } from '../apps/spec-builder/api'

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
  docs: { 'requirements.md': { hash: 'a'.repeat(64) } },
  duplicate_supported: true,
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

function renderDetail(
  name = 'checkout',
  setErr: (m: string) => void = () => {},
  onDeleted?: () => void,
  onDuplicated?: (name: string) => void,
) {
  return render(
    <QueryClientProvider client={queryClient}>
      <SpecDetail
        name={name}
        setErr={setErr}
        onDeleted={onDeleted}
        onDuplicated={onDuplicated}
      />
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
  mobile = false
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

  it('puts duplicate and archive in one overflow menu on mobile', async () => {
    mobile = true
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    installFetch(BASE)
    renderDetail()

    const menu = await screen.findByRole('button', { name: /more actions/i })
    expect(screen.queryByRole('button', { name: /duplicate this spec/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /archive this spec/i })).not.toBeInTheDocument()
    await user.click(menu)
    expect(await screen.findByRole('menuitem', { name: /duplicate this spec/i })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /archive this spec/i })).toBeInTheDocument()
  })

  it('does not offer duplicate when the backend cannot publish it safely', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    installFetch({ ...BASE, duplicate_supported: false })
    renderDetail()

    await user.click(await screen.findByRole('button', { name: /more actions/i }))

    expect(screen.queryByRole('menuitem', { name: /duplicate this spec/i })).not.toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /archive this spec/i })).toBeInTheDocument()
  })

  it('moves the duplicate form below the fixed header at every pane width', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    installFetch(BASE)
    renderDetail()

    await user.click(await screen.findByRole('button', { name: /more actions/i }))
    await user.click(await screen.findByRole('menuitem', { name: /duplicate this spec/i }))

    const form = await screen.findByTestId('duplicate-form')
    const input = screen.getByRole('textbox', { name: /name for the copy/i })
    expect(form).toHaveClass('flex-col')
    expect(form).toContainElement(input)
    expect(input.closest('header')).toBeNull()
  })

  it('moves the title editor below the fixed header at every pane width', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    installFetch(BASE)
    renderDetail()

    await user.click(await screen.findByRole('button', { name: 'checkout' }))

    const form = await screen.findByTestId('title-form')
    const input = screen.getByRole('textbox', { name: /spec label/i })
    expect(form).toHaveClass('flex-col')
    expect(form).toContainElement(input)
    expect(input.closest('header')).toBeNull()
  })

  it('keeps the mobile title static and puts rename in the overflow menu', async () => {
    mobile = true
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    installFetch(BASE)
    renderDetail()

    await screen.findByText('checkout')
    expect(screen.queryByRole('button', { name: 'checkout' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /more actions/i }))
    expect(await screen.findByRole('menuitem', { name: /rename/i })).toBeInTheDocument()
  })

  it('archives from the overflow menu', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    installFetch(BASE)
    renderDetail()

    await user.click(await screen.findByRole('button', { name: /more actions/i }))
    await user.click(await screen.findByRole('menuitem', { name: /archive this spec/i }))

    await waitFor(() => expect(calls.some((c) => c.url.includes('/archive'))).toBe(true))
    const request = calls.find((c) => c.url.includes('/archive'))
    expect(JSON.parse(request?.body || '{}').archived).toBe(true)
  })

  it('names a duplicate inline and selects the completed copy', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    const onDuplicated = vi.fn()
    installFetch(BASE, (url) => (url.includes('/duplicate')
      ? Promise.resolve(okRes('{"name":"checkout-v2"}'))
      : undefined))
    renderDetail('checkout', () => {}, undefined, onDuplicated)

    await user.click(await screen.findByRole('button', { name: /more actions/i }))
    await user.click(await screen.findByRole('menuitem', { name: /duplicate this spec/i }))
    const input = await screen.findByRole('textbox', { name: /name for the copy/i })
    fireEvent.change(input, { target: { value: 'checkout-v2' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => expect(calls.some((c) => c.url.includes('/duplicate'))).toBe(true))
    const request = calls.find((c) => c.url.includes('/duplicate'))
    expect(JSON.parse(request?.body || '{}').new_name).toBe('checkout-v2')
    await waitFor(() => expect(onDuplicated).toHaveBeenCalledWith('checkout-v2'))
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
    // One document pane: a second copy behind the overlay doubled markdown
    // parse cost and ran two selection listeners on the same window selection.
    expect(screen.getAllByTestId('doc-view')).toHaveLength(1)

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('does not steal focus from the comment composer when the spec poll ticks', async () => {
    installFetch(BASE)
    renderDetail()

    await screen.findByTestId('doc-view')
    fireEvent.click(screen.getByRole('button', { name: 'Expand document for review' }))
    await screen.findByRole('dialog')

    const draft = screen.getByTestId('doc-comment-draft')
    draft.focus()
    expect(draft).toHaveFocus()

    // A poll that returns new object identity re-renders SpecDetail. The overlay
    // used an inline callback ref that focused the dialog on every such tick.
    act(() => {
      queryClient.setQueryData(['spec-builder', 'spec', 'checkout'], { ...BASE, running: true })
    })
    expect(draft).toHaveFocus()
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

  it('keeps phase actions reachable in the overlay so a review can approve', async () => {
    installFetch(BASE)
    renderDetail()

    await screen.findByTestId('doc-view')
    fireEvent.click(screen.getByRole('button', { name: 'Expand document for review' }))
    const dialog = await screen.findByRole('dialog')
    // The column header hides its copy while the overlay is up, so this is
    // the only Approve on the page — expanding used to hide the action entirely.
    expect(dialog).toHaveAccessibleName('Review requirements.md for checkout')
    expect(screen.getByRole('button', { name: /Approve → Design/ })).toBeInTheDocument()
  })
})

describe('SpecDetail phase actions', () => {
  it('refuses to advance when the reviewed document has no trustworthy hash', async () => {
    installFetch({ ...BASE, docs: {} })
    renderDetail()

    const approve = await screen.findByRole('button', { name: /Approve → Design/ })
    expect(approve).toBeDisabled()
    fireEvent.click(approve)
    expect(calls.filter((c) => c.url.includes('/message'))).toHaveLength(0)
  })

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
    detail = {
      ...BASE,
      phase: 'design',
      files: { 'requirements.md': '# r', 'design.md': '# d' },
      docs: { ...BASE.docs, 'design.md': { hash: 'b'.repeat(64) } },
    }
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
    installFetch({
      ...BASE,
      phase: 'design',
      files: { 'requirements.md': '# r', 'design.md': '# d' },
      docs: { ...BASE.docs, 'design.md': { hash: 'b'.repeat(64) } },
    })
    renderDetail()

    const approve = await screen.findByRole('button', { name: /Approve → Tasks/ })
    expect(screen.getByTestId('doc-view')).toHaveAttribute('data-tab', 'requirements')
    expect(approve).toBeDisabled()
    fireEvent.click(approve)
    expect(calls.filter((c) => c.url.includes('/approve'))).toHaveLength(0)
    expect(calls.filter((c) => c.url.includes('/message'))).toHaveLength(0)

    await selectTab('Design')
    expect(screen.getByTestId('doc-view')).toHaveAttribute('data-tab', 'design')
    expect(approve).toBeEnabled()
    fireEvent.click(approve)
    await waitFor(() => expect(calls.filter((c) => c.url.includes('/message'))).toHaveLength(1))
    const approval = calls.find((c) => c.url.includes('/approve'))
    expect(JSON.parse(approval?.body || '{}')).toMatchObject({
      phase: 'design',
      hash: 'b'.repeat(64),
    })
    const message = calls.find((c) => c.url.includes('/message'))
    expect(JSON.parse(message?.body || '{}').text).toContain('Design approved')
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

  it('sends a state-panel answer with its decision id so the backend can lock it', async () => {
    installFetch(BASE)
    renderDetail()

    fireEvent.click(await screen.findByTestId('state-send'))
    await waitFor(() => expect(calls.filter((c) => c.url.includes('/message'))).toHaveLength(1))
    expect(JSON.parse(calls[0].body).text).toBe('Decision: one')
    // Without this the write is an ordinary message and the backend has nothing to
    // record, so the decision stays re-answerable.
    expect(JSON.parse(calls[0].body).decision_id).toBe('transport')
    // The bare option travels separately from the composed prompt: it is what the
    // backend records and what the card renders back as the answer.
    expect(JSON.parse(calls[0].body).decision_option).toBe('one')
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

describe('SpecDetail delete', () => {
  async function openRemoveConfirmation() {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await user.click(await screen.findByRole('button', { name: /more actions/i }))
    await user.click(await screen.findByRole('menuitem', { name: 'Remove spec checkout' }))
  }

  it('asks first, then removes the spec and notifies the workspace', async () => {
    const onDeleted = vi.fn()
    installFetch(BASE)
    renderDetail('checkout', () => {}, onDeleted)

    await openRemoveConfirmation()
    expect(await screen.findByRole('dialog', { name: 'Remove this spec?' })).toBeInTheDocument()
    expect(screen.getByText(/markdown files stay in the project/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Remove this spec' }))
    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1))
    const deleted = calls.filter((c) => c.method === 'DELETE')
    expect(deleted).toHaveLength(1)
    expect(deleted[0].url).toContain('/specs/checkout')
    expect(deleted[0].url).toContain('spec_dir=')
    expect(deleted[0].url).toContain('slot_key=')
  })

  it('removes the confirmed spec, not a replacement that landed while the dialog was open', async () => {
    const onDeleted = vi.fn()
    installFetch(BASE)
    renderDetail('checkout', () => {}, onDeleted)

    await openRemoveConfirmation()
    await screen.findByRole('dialog', { name: 'Remove this spec?' })

    act(() => {
      queryClient.setQueryData(['spec-builder', 'spec', 'checkout'], {
        ...BASE,
        spec_dir: '/proj/other/.kiro/specs/checkout',
        slot_key: 'spec-builder-checkout-replacement',
      })
    })

    fireEvent.click(screen.getByRole('button', { name: 'Remove this spec' }))
    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1))
    const deleted = calls.filter((c) => c.method === 'DELETE')
    expect(deleted).toHaveLength(1)
    expect(deleted[0].url).toContain(encodeURIComponent(BASE.spec_dir))
    expect(deleted[0].url).toContain('spec-builder-checkout-99')
    expect(deleted[0].url).not.toContain('replacement')
  })

  it('dismisses the confirm without sending a delete', async () => {
    installFetch(BASE)
    renderDetail()

    await openRemoveConfirmation()
    await screen.findByRole('dialog', { name: 'Remove this spec?' })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Remove this spec?' })).not.toBeInTheDocument())
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(0)
  })

  it('surfaces a refused delete and leaves the spec open', async () => {
    const setErr = vi.fn()
    const onDeleted = vi.fn()
    installFetch(BASE, (url) => (url.includes('/specs/checkout')
      ? Promise.resolve({ ok: false, status: 409, json: () => Promise.resolve({ error: 'stale client' }) })
      : undefined))
    renderDetail('checkout', setErr, onDeleted)

    await openRemoveConfirmation()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove this spec' }))
    await waitFor(() => expect(setErr).toHaveBeenCalledWith('stale client'), { timeout: 5_000 })
    expect(onDeleted).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog', { name: 'Remove this spec?' })).toBeInTheDocument()
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

describe('SpecDetail tasks-panel poll (#5361)', () => {
  const withTasks = {
    ...BASE,
    files: { 'requirements.md': '# r', 'tasks.md': '- [ ] one' },
    docs: { 'tasks.md': { hash: 'b'.repeat(64) } },
    tasks: [{ index: 0, text: 'one', done: false, hash: 'c'.repeat(64) }],
  }

  it('refetches as soon as the Tasks tab opens, then keeps the fast cadence while idle', async () => {
    let gets = 0
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => {
      gets += 1
      return Promise.resolve(okRes(JSON.stringify(withTasks)))
    }))
    renderDetail()
    await screen.findByTestId('chat-column')
    const afterMount = gets

    await selectTab('Tasks')
    await waitFor(() => expect(gets).toBeGreaterThan(afterMount))
    const afterOpen = gets

    await vi.advanceTimersByTimeAsync(SPEC_DETAIL_FAST_POLL_MS)
    expect(gets).toBeGreaterThan(afterOpen)
  })

  it('stays on the idle cadence on Requirements when nothing is running', async () => {
    let gets = 0
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => {
      gets += 1
      return Promise.resolve(okRes(JSON.stringify(BASE)))
    }))
    renderDetail()
    await screen.findByTestId('chat-column')
    const afterMount = gets

    await vi.advanceTimersByTimeAsync(SPEC_DETAIL_FAST_POLL_MS)
    expect(gets).toBe(afterMount)

    await vi.advanceTimersByTimeAsync(SPEC_DETAIL_IDLE_POLL_MS - SPEC_DETAIL_FAST_POLL_MS)
    expect(gets).toBeGreaterThan(afterMount)
  })
})
