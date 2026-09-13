import { readFileSync } from 'node:fs'
import { useRef } from 'react'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { api, ApiError } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, fileSearch: vi.fn() } }
})

/**
 * Shrink the real deadline by wrapping the MODULE rather than stubbing a
 * timer or `AbortSignal.timeout`. That keeps the production composition under
 * test and changes only the duration, so these tests exercise the same
 * `withDeadline` the component ships with.
 *
 * Stubbing a global instead is how a test in this family passes for the wrong
 * reason: a detached `AbortSignal.timeout` reference throws `TypeError` inside
 * the stub (happy-dom reads an internal window slot off `this`), react-query
 * catches it, and the component lands in the very settled-error state the
 * assertions are looking for — with the fix effectively absent.
 */
const seen = vi.hoisted(() => ({ ms: [] as number[], shrinkTo: 20 }))
vi.mock('../lib/withDeadline', async () => {
  const real = await vi.importActual<typeof import('../lib/withDeadline')>('../lib/withDeadline')
  return {
    withDeadline: (ms: number, outer: AbortSignal | undefined,
                   attempt: (s: AbortSignal) => Promise<unknown>) => {
      seen.ms.push(ms)
      return real.withDeadline(seen.shrinkTo, outer, attempt)
    },
  }
})

import FilePickerMenu from '../components/FilePickerMenu'
import { FILE_SEARCH_TIMEOUT_MS } from '../api/client'
import { withDeadline } from '../lib/withDeadline'
import { retryPolicy, retryDelayPolicy } from '../api/queryClient'

const fileSearch = vi.mocked(api.fileSearch)

/**
 * A wedged gateway behind the SAME deadline the real `api.fileSearch` binds, so
 * the mock stands in for the bounded client rather than for a bare fetch.
 *
 * The inner promise settles ONLY when its signal aborts, and given no signal
 * NEVER settles at all. That second half is the pre-fix behaviour exactly, which
 * is what makes these assertions a real negative control rather than a
 * tautology — drop the deadline binding and the fetch stays pending.
 */
const wedgedGateway = () =>
  (_q: string, _project?: string, signal?: AbortSignal) =>
    withDeadline(FILE_SEARCH_TIMEOUT_MS, signal, s =>
      new Promise((_resolve, reject) => {
        if (s.aborted) return reject(s.reason)
        s.addEventListener('abort', () => reject(s.reason), { once: true })
      }))

/** The picker positions against a live anchor, so give it a real element. */
function Host(props: Omit<React.ComponentProps<typeof FilePickerMenu>, 'anchorRef'>) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <>
      <div ref={ref} data-testid="zzq-anchor" tabIndex={-1} />
      <FilePickerMenu {...props} anchorRef={ref} />
    </>
  )
}

function mount(props: Partial<React.ComponentProps<typeof FilePickerMenu>> = {}) {
  const onSelect = vi.fn()
  const onClose = vi.fn()
  const view = renderWithProviders(
    <Host query="zz" open onSelect={onSelect} onClose={onClose} {...props} />,
  )
  return { onSelect, onClose, ...view }
}

/**
 * Mount with the SHIPPED retry policy instead of the test helper's `retry: false`.
 *
 * Load-bearing: the shared render helper disables retries by default, so a test
 * rendered through it cannot observe the production behaviour at all and would
 * be a vacuous gate. The real `retryPolicy` refuses to retry a deadline, and this
 * query adds no override, so the bound is what settles it.
 */
function mountWithShippedRetry(props: Partial<React.ComponentProps<typeof FilePickerMenu>> = {}) {
  const onSelect = vi.fn()
  const onClose = vi.fn()
  const view = renderWithProviders(
    <Host query="zz" open onSelect={onSelect} onClose={onClose} {...props} />,
    { queryDefaults: { retry: retryPolicy, retryDelay: retryDelayPolicy } },
  )
  return { onSelect, onClose, ...view }
}

beforeEach(() => {
  vi.clearAllMocks()
  seen.ms = []
  seen.shrinkTo = 20
})
afterEach(() => { vi.restoreAllMocks() })

describe('FilePickerMenu — bounded /api/file-search fetch', () => {
  it('still hands api.fileSearch an AbortSignal (the deadline must not drop it)', async () => {
    // The wrapper substitutes its OWN signal for react-query's; the call must
    // keep receiving one in slot 3 so unmount/cancel still aborts the request.
    fileSearch.mockImplementation(wedgedGateway() as never)
    mount()
    await waitFor(() => expect(fileSearch).toHaveBeenCalled())
    expect(fileSearch.mock.calls[0][2]).toBeInstanceOf(AbortSignal)
  })

  it('asks for a deadline of FILE_SEARCH_TIMEOUT_MS, bounded either side', async () => {
    // Pinned on the REQUESTED value, not elapsed wall-clock, so the constant
    // cannot drift silently without this failing.
    fileSearch.mockImplementation(wedgedGateway() as never)
    mount()
    await waitFor(() => expect(seen.ms.length).toBeGreaterThan(0))
    expect(seen.ms).toContain(FILE_SEARCH_TIMEOUT_MS)
    // A bound under the walk time fails every attempt alike, so the recovery beside the
    // timeout could never win. Driving the shipped handler measured 5.4s worst case.
    expect(FILE_SEARCH_TIMEOUT_MS).toBeGreaterThan(5_400)
    // Pinned on its own literal, NOT relative to the skills menu: an alias would let a
    // skills retune drag file search along silently.
    expect(FILE_SEARCH_TIMEOUT_MS).toBe(15_000)
  })

  it('clears "Searching…" when the deadline fires on a response that never arrives', async () => {
    // THE DEFECT: unbounded, the query stayed pending and the menu showed
    // "Searching…" forever — indistinguishable from a hang.
    fileSearch.mockImplementation(wedgedGateway() as never)
    // 250ms (not the 20ms the others use) so the pending state is observable
    // before the deadline fires, making both halves deterministic.
    seen.shrinkTo = 250
    const onSelect = vi.fn()
    const onClose = vi.fn()
    const { rerender } = mount({ onSelect, onClose })
    // The rerender is load-bearing: anchorRef.current is null on first render,
    // so the menu returns null and paints nothing until something re-renders it.
    rerender(<Host query="zz" open onSelect={onSelect} onClose={onClose} />)
    // Matched by substring: the held/sends suffix is the composer's copy, not this
    // test's subject, which is that the pending state CLEARS when the deadline fires.
    expect(await screen.findByText(/Searching…/)).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText(/Searching…/)).not.toBeInTheDocument())
  })

  it('settles into the released-Enter empty state, so the composer is not deadlocked', async () => {
    // releaseKeysWhenEmpty admits `isError`, so a TimeoutError reaches the same
    // settled state and hands Enter back instead of swallowing it forever.
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    const { onSelect, onClose } = mount()
    expect(await screen.findByText(/File search timed out — Enter sends the message/)).toBeInTheDocument()
    await waitFor(() => expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true))
    expect(onClose).toHaveBeenCalled()
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('does not retry under the SHIPPED retry policy, so the bound is the one advertised', async () => {
    // Neither policy retries a deadline, so the advertised bound is the real one:
    // no second attempt extends the window Enter stays swallowed.
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    const { onClose } = mountWithShippedRetry()
    await waitFor(() => expect(fileSearch).toHaveBeenCalledTimes(1))

    // Past the 1s retry backoff: a second attempt would have been made by now.
    await new Promise(r => setTimeout(r, 1_400))
    expect(fileSearch).toHaveBeenCalledTimes(1)

    // And the query is settled, so the release gate is armed rather than
    // swallowing Enter across a retry window.
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true)
    expect(onClose).toHaveBeenCalled()
  })

  it('retries a transient 429 under the SHIPPED policy, so a throttle is not a failed search', async () => {
    // Inverted deliberately: it previously pinned the `retry: false` the GPT lane
    // blocked. Non-vacuous -- with the override still present, attempts stays at 1.
    let attempts = 0
    fileSearch.mockImplementation((() => {
      attempts += 1
      return Promise.reject(new ApiError(429, 'Rate exceeded'))
    }) as never)
    mountWithShippedRetry()

    // Past the 1s + jitter the shared ladder waits before attempt two.
    await waitFor(() => expect(attempts).toBeGreaterThan(1), { timeout: 4_000 })
  })

  it('says the search FAILED rather than reporting no matches (a timeout is not an absence)', async () => {
    // Routing a wedged gateway into the settled-empty copy tells a user the file
    // they are looking for does not exist. That is a false negative, not a hint.
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    mount()
    expect(await screen.findByText(/File search timed out — Enter sends the message/))
      .toBeInTheDocument()
    expect(screen.queryByText(/No matching files/)).not.toBeInTheDocument()
  })

  it('names Ctrl+Enter in the failure copy when that is the send binding', async () => {
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    mount({ sendOnEnter: 'ctrl-enter' })
    expect(await screen.findByText(/File search timed out — Ctrl\+Enter sends the message/))
      .toBeInTheDocument()
  })

  it('announces the empty and failed copy to a screen reader (role="alert")', async () => {
    // The copy exists to prevent a silent-send surprise when Enter's meaning
    // flips, so a visually-only announcement leaves that user with the surprise.
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    mount()
    const status = await screen.findByRole('alert')
    expect(status).toHaveTextContent(/File search timed out — Enter sends the message/)
  })

  it('colours the failure branch differently from the empty branch', async () => {
    // The failed/empty distinction this change exists to draw must not be
    // legible only by reading the words.
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    mount()
    const failed = await screen.findByRole('alert')
    expect(failed.className).toContain('text-danger')
    expect(failed.className).not.toContain('text-muted')
    // The empty branch keeps role="status"; only a failure is an alert.
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('keeps the settled-empty branch muted, and still announces its copy', async () => {
    fileSearch.mockResolvedValue({ results: [], root: '/root' } as never)
    mount()
    const empty = await screen.findByRole('status')
    expect(empty).toHaveTextContent(/No matching files/)
    expect(empty.className).toContain('text-muted')
    expect(empty.className).not.toContain('text-danger')
  })

  it('names the timeout apart from a gateway failure, not one shared copy', async () => {
    // A plain rejection must still read "failed": the distinction is the point, not a rename.
    seen.shrinkTo = 20
    fileSearch.mockImplementation(wedgedGateway() as never)
    mountWithShippedRetry()
    expect(await screen.findByText(/File search timed out/)).toBeInTheDocument()
    expect(screen.queryByText(/File search failed/)).not.toBeInTheDocument()
  })

  it('names a refusal by its cause, not as a transient failure the user should retry', async () => {
    // Same `code` -> cause map the folder panel uses. Collapsing a 403 refusal into
    // "File search failed" reads as transient and invites a pointless retry.
    fileSearch.mockRejectedValue(new ApiError(
      403, 'denied', JSON.stringify({ error: 'denied', code: 'access_denied' })))
    mount()
    expect(await screen.findByText(/No access to the project folder/)).toBeInTheDocument()
    expect(screen.queryByText(/File search failed/)).not.toBeInTheDocument()
  })

  it('names a missing project root apart from a refusal', async () => {
    fileSearch.mockRejectedValue(new ApiError(
      404, 'gone', JSON.stringify({ error: 'gone', code: 'project_not_found' })))
    mount()
    expect(await screen.findByText(/Project folder not found/)).toBeInTheDocument()
  })

  it('falls back to the generic copy for a session expiry, which refuses no path', async () => {
    // The body carries a mappable `code` ON PURPOSE: without it the generic key is
    // reached anyway and this test would pass with the session-expiry guard deleted.
    fileSearch.mockRejectedValue(new ApiError(
      403, 'auth', JSON.stringify({ error: 'auth', code: 'access_denied' }), true))
    mount()
    expect(await screen.findByText(/File search failed/)).toBeInTheDocument()
    expect(screen.queryByText(/No access to the project folder/)).not.toBeInTheDocument()
  })

  it('drops the send-key claim from the notice while rows stay selectable', async () => {
    // With rows present useListKeyboardNav has count > 0, so Enter PICKS. A notice promising
    // "Enter sends the message" there states the opposite of what the key does.
    fileSearch.mockResolvedValueOnce({
      results: [{ path: '/p/kept.ts', name: 'kept.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const { queryClient } = mount({ query: 'zz' })
    expect(await screen.findByText('kept.ts')).toBeInTheDocument()

    fileSearch.mockRejectedValue(new ApiError(500, 'boom'))
    await queryClient.refetchQueries({ queryKey: ['file-search'] })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/File search failed/)
    expect(alert).not.toHaveTextContent(/sends the message/)
    // The recovery cue is a real control now, not a word inside the notice.
    expect(alert).not.toHaveTextContent(/Retry/i)
    expect(screen.getByText('kept.ts')).toBeInTheDocument()
  })

  it('still names the send key when NO rows survive, where Enter really does send', async () => {
    // The mirror case: at count === 0 the release gate fires and Enter sends the draft, so the
    // suffix is the true statement there and must not be collateral damage of the fix above.
    fileSearch.mockRejectedValue(new ApiError(500, 'boom'))
    mount({ query: 'zz' })
    expect(await screen.findByRole('alert')).toHaveTextContent(/Enter sends the message/)
  })

  it.each([
    ['timed_out', 504, 'deadline', undefined, true],
    ['failed', 500, 'boom', undefined, true],
    ['denied', 403, 'denied', 'access_denied', false],
    ['root_missing', 404, 'gone', 'project_not_found', false],
  ] as const)('offers Retry for %s only when re-asking could answer differently',
    async (_cause, status, msg, code, expected) => {
      fileSearch.mockRejectedValue(code
        ? new ApiError(status, msg, JSON.stringify({ error: msg, code }))
        : new ApiError(status, msg))
      mount({ query: 'zz' })

      // The notice itself lands either way -- only the recovery control is conditional.
      await screen.findByRole('alert')
      const button = screen.queryByRole('button', { name: /^Retry: / })
      expect(button === null).toBe(!expected)
    })

  it('offers a Retry on a first failed search, when there are no rows to fall back on', async () => {
    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    mount({ query: 'zz' })

    const alert = await screen.findByRole('alert')
    // The send-key hint is true here -- with no rows Enter is released to send.
    expect(alert).toHaveTextContent(/Enter sends the message/i)
    expect(screen.queryByText('kept.ts')).toBeNull()

    const button = screen.getByRole('button', { name: /^Retry: / })
    expect(button).toBeEnabled()

    fileSearch.mockResolvedValue({
      results: [{ path: '/p/found.ts', name: 'found.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const before = fileSearch.mock.calls.length
    fireEvent.click(button)
    await waitFor(() => expect(fileSearch.mock.calls.length).toBeGreaterThan(before))
    expect(await screen.findByText('found.ts')).toBeInTheDocument()
  })

  it('gives Tab to the Retry button instead of closing, so recovery is keyboard-reachable', async () => {
    // Focus stays in the composer and the release hands Tab back, so before this only a
    // pointer could reach Retry -- unusable on a surface driven entirely by the keyboard.
    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    const { onClose } = mount({ query: 'zz' })

    const button = await screen.findByRole('button', { name: /^Retry: / })
    expect(fireEvent.keyDown(document, { key: 'Tab' })).toBe(false)
    expect(button).toHaveFocus()
    expect(onClose).not.toHaveBeenCalled()

    // Enter is the focused button's own activation key, so it must re-run the search rather
    // than dismiss the menu out from under the control the user just reached.
    fileSearch.mockResolvedValue({
      results: [{ path: '/p/found.ts', name: 'found.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const before = fileSearch.mock.calls.length
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(false)
    await waitFor(() => expect(fileSearch.mock.calls.length).toBeGreaterThan(before))
    expect(onClose).not.toHaveBeenCalled()
    expect(await screen.findByText('found.ts')).toBeInTheDocument()
  })

  it('still releases Enter to the composer when focus never reached Retry', async () => {
    // The claim back is scoped to the focused control; with focus still in the composer the
    // released Enter must send, which is the prompt-mention trap this change exists to fix.
    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    const { onClose } = mount({ query: 'zz' })

    await screen.findByRole('button', { name: /^Retry: / })
    expect(fireEvent.keyDown(document, { key: 'Enter' })).toBe(true)
    expect(onClose).toHaveBeenCalled()
  })

  it('keeps the failure notice mounted while the clicked Retry is in flight', async () => {
    // Dropping the notice on `isFetching` unmounted the button mid-press, so its disabled /
    // aria-busy state could never render and focus fell to the body.
    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    mount({ query: 'zz' })

    const button = await screen.findByRole('button', { name: /^Retry: / })
    let release: (v: unknown) => void = () => {}
    fileSearch.mockImplementation(() => new Promise(r => { release = r }) as never)
    fireEvent.click(button)

    await waitFor(() => expect(screen.getByRole('button', { name: /^Retry: / })).toBeDisabled())
    expect(screen.getByRole('button', { name: /^Retry: / })).toHaveAttribute('aria-busy', 'true')
    release({ results: [], root: '/p' })
  })

  it('hands focus back to Retry when the retry FAILS, not to the body', async () => {
    // A browser blurs a control the moment it becomes `disabled` and jsdom does not, so the
    // recovery is pinned on the hand-back itself rather than on where focus lands.
    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    mount({ query: 'zz' })

    const button = await screen.findByRole('button', { name: /^Retry: / })
    const refocus = vi.spyOn(button, 'focus')

    let reject: (v: unknown) => void = () => {}
    fileSearch.mockImplementation(() => new Promise((_r, rej) => { reject = rej }) as never)
    fireEvent.click(button)
    await waitFor(() => expect(screen.getByRole('button', { name: /^Retry: / })).toBeDisabled())
    expect(refocus).not.toHaveBeenCalled()

    reject(new ApiError(504, 'deadline again'))
    await waitFor(() => expect(screen.getByRole('button', { name: /^Retry: / })).toBeEnabled())
    await waitFor(() => expect(refocus).toHaveBeenCalled())
  })

  it('lets Tab leave the Retry instead of re-focusing it, which had no stated exit', async () => {
    // Claiming Tab calls preventDefault; declining falls through to the release/close path.
    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    mount({ query: 'zz' })

    const button = await screen.findByRole('button', { name: /^Retry: / })
    const claimed = () => {
      const e = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
      document.dispatchEvent(e)
      return e.defaultPrevented
    }
    // First Tab, focus still outside the button: the hand-off takes it.
    expect(claimed()).toBe(true)
    await waitFor(() => expect(screen.getByRole('button', { name: /^Retry: / })).toHaveFocus())

    // Second Tab, now ON the button: it must DECLINE so the key reaches the host.
    expect(claimed()).toBe(false)
    expect(button).toBe(screen.getByRole('button', { name: /^Retry: / }))
  })

  it('stops promising Enter sends while the retry has re-swallowed it', async () => {
    // The #5029 trap, transiently: a retry re-enters `pending`, which closes the key gate
    // again, so the settled copy's "Enter sends" half is false for the whole retry window.
    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    mount({ query: 'zz' })

    const settled = await screen.findByRole('alert')
    expect(settled).toHaveTextContent(/Enter sends the message/i)

    const button = screen.getByRole('button', { name: /^Retry: / })
    let release: (v: unknown) => void = () => {}
    fileSearch.mockImplementation(() => new Promise(r => { release = r }) as never)
    fireEvent.click(button)

    await waitFor(() =>
      expect(screen.getByRole('alert')).not.toHaveTextContent(/Enter sends the message/i))
    // The cause is still named -- only the key promise is withheld.
    expect(screen.getByRole('alert')).toHaveTextContent(/File search failed/i)
    release({ results: [], root: '/p' })
  })

  it('hands focus back to the composer when a retry succeeds', async () => {
    // A successful retry unmounts the button being pressed, so without the hand-back focus
    // lands on <body> and the next keystroke reaches nothing.
    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    mount({ query: 'zz' })

    const button = await screen.findByRole('button', { name: /^Retry: / })
    button.focus()
    fileSearch.mockResolvedValue({
      results: [{ path: '/p/found.ts', name: 'found.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    fireEvent.click(button)

    expect(await screen.findByText('found.ts')).toBeInTheDocument()
    await waitFor(() => expect(document.activeElement).toBe(screen.getByTestId('zzq-anchor')))
  })

  it('activates the focused Retry on Enter instead of inserting a surviving row', async () => {
    // A pointer or a browser Tab can put focus on Retry while rows survive, and the n>0 Enter
    // dispatch would choose the highlighted row -- inserting a stale mention instead of retrying.
    fileSearch.mockResolvedValueOnce({
      results: [{ path: '/p/kept.ts', name: 'kept.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const { queryClient, onSelect } = mount({ query: 'zz' })
    expect(await screen.findByText('kept.ts')).toBeInTheDocument()

    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    await queryClient.refetchQueries()

    const button = await screen.findByRole('button', { name: /^Retry: / })
    button.focus()
    expect(button).toHaveFocus()

    const before = fileSearch.mock.calls.length
    fireEvent.keyDown(document, { key: 'Enter' })
    await waitFor(() => expect(fileSearch.mock.calls.length).toBeGreaterThan(before))
    expect(onSelect).not.toHaveBeenCalled()
  })


  it('marks surviving rows as belonging to the failed query, not the current one', async () => {
    fileSearch.mockResolvedValueOnce({
      results: [{ path: '/p/kept.ts', name: 'kept.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const { queryClient } = mount({ query: 'zz' })
    expect(await screen.findByText('kept.ts')).toBeInTheDocument()

    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    await queryClient.refetchQueries()
    await screen.findByRole('button', { name: /^Retry: / })

    const row = screen.getByText('kept.ts').closest('[role=\'option\']') as HTMLElement
    // The dim has to stay clear of this codebase's DISABLED band (opacity-30/40), or a mouse
    // user reads a still-clickable stale result as dead; the notice carries the meaning.
    expect(row.className).toContain('opacity-80')
    expect(row.className).not.toContain('opacity-60')
    expect(row.className).toContain('hover:opacity-100')
    expect(row.className).toContain('focus-visible:opacity-100')
    const noteId = row.getAttribute('aria-describedby')
    expect(noteId).toBeTruthy()
    expect(document.getElementById(noteId!)).toContainElement(
      screen.getByTestId('file-picker-search-error'))
  })

  it('does not spend an IME-owned Tab on the Retry hand-off', async () => {
    // The hand-off consumes the key, so claiming it mid-composition would abandon text the
    // user has not committed -- the guard the nonempty path already had one branch over.
    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    const { onClose } = mount({ query: 'zz' })
    const button = await screen.findByRole('button', { name: /^Retry: / })

    const composer = document.querySelector('textarea') ?? document.body
    fireEvent.compositionStart(composer)
    fireEvent.keyDown(document, { key: 'Tab', isComposing: true })

    expect(button).not.toHaveFocus()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('does not insert a stale mention when Tab is pressed a SECOND time', async () => {
    // The second Tab used to decline the hand-off, and with rows on screen the hook's nonempty
    // branch then dispatched `onChoose` -- inserting a mention the notice calls out of date.
    fileSearch.mockResolvedValueOnce({
      results: [{ path: '/p/kept.ts', name: 'kept.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const { queryClient, onSelect } = mount({ query: 'zz' })
    expect(await screen.findByText('kept.ts')).toBeInTheDocument()

    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    await queryClient.refetchQueries()

    const button = await screen.findByRole('button', { name: /^Retry: / })
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(button).toHaveFocus()

    // The stale rows are still on screen, so a second Tab must not choose one of them.
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('tells a sighted user the surviving rows are the PREVIOUS result', async () => {
    // opacity-80 plus aria-describedby left the screen-reader path covered and the sighted one
    // guessing, so a dimmed row could be read as an answer to the query that just failed.
    fileSearch.mockResolvedValueOnce({
      results: [{ path: '/p/kept.ts', name: 'kept.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const { queryClient } = mount({ query: 'zz' })
    expect(await screen.findByText('kept.ts')).toBeInTheDocument()

    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    await queryClient.refetchQueries()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('File search failed')
    expect(alert).toHaveTextContent('showing the last values we read')
    // The row the clause describes is still on screen, so it is talking about something visible.
    expect(screen.getByText('kept.ts')).toBeInTheDocument()
  })

  it('leaves the EMPTY failure arm without the previous-result clause', async () => {
    // Control for the pin above: with no surviving rows there is nothing earlier to describe.
    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    mount({ query: 'zz' })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('File search failed')
    expect(alert).not.toHaveTextContent('showing the last values we read')
  })

  it('gives Tab to the Retry when stale rows survived the failure', async () => {
    // Cached rows keep the list nonempty, so the empty-list hand-off never runs and Tab would
    // select a stale row, leaving the rendered Retry unreachable by keyboard.
    fileSearch.mockResolvedValueOnce({
      results: [{ path: '/p/kept.ts', name: 'kept.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const { queryClient, onClose } = mount({ query: 'zz' })
    expect(await screen.findByText('kept.ts')).toBeInTheDocument()

    fileSearch.mockRejectedValue(new ApiError(504, 'deadline'))
    await queryClient.refetchQueries()

    const button = await screen.findByRole('button', { name: /^Retry: / })
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(button).toHaveFocus()
    expect(onClose).not.toHaveBeenCalled()
    // The hand-off must not have cost the user the rows underneath.
    expect(screen.getByText('kept.ts')).toBeInTheDocument()
  })

  it('offers a Retry the user can press when a refetch fails over stale rows', async () => {
    // The word alone promised an affordance that was not there: with rows on screen Enter picks a
    // row, so neither the send-key hint nor a bare "Retry" label gives the user a way to re-run.
    fileSearch.mockResolvedValueOnce({
      results: [{ path: '/p/kept.ts', name: 'kept.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const { queryClient } = mount({ query: 'zz' })
    expect(await screen.findByText('kept.ts')).toBeInTheDocument()

    fileSearch.mockRejectedValue(new ApiError(500, 'boom'))
    await queryClient.refetchQueries({ queryKey: ['file-search'] })
    await screen.findByRole('alert')

    const button = screen.getByRole('button', { name: /^Retry: / })
    expect(button).toBeEnabled()

    fileSearch.mockResolvedValue({
      results: [{ path: '/p/back.ts', name: 'back.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const before = fileSearch.mock.calls.length
    fireEvent.click(button)
    // Pressing it re-runs the search rather than merely looking like it might.
    await waitFor(() => expect(fileSearch.mock.calls.length).toBeGreaterThan(before))
    expect(await screen.findByText('back.ts')).toBeInTheDocument()
  })

  it('keeps the @-menu and folder-panel refusal nouns naming their own scope', async () => {
    // The two surfaces search different roots -- the menu the chat's project, the panel the
    // folder tab's cwd -- so aligning the nouns would mis-name one of them.
    const en = JSON.parse(
      readFileSync('src/i18n/locales/en.manual.json', 'utf8')) as Record<string, never>
    const menu = (en as unknown as {
      components: { filePickerMenu: Record<string, string> }
    }).components.filePickerMenu
    const panel = (en as unknown as {
      pages: { chat: { folderPanel: Record<string, string> } }
    }).pages.chat.folderPanel

    expect(menu.search_denied).toMatch(/project folder/i)
    expect(menu.search_root_missing).toMatch(/project folder/i)
    // The panel must NOT claim the project, which is a different directory.
    expect(panel.search_denied).not.toMatch(/project/i)
    expect(panel.search_root_missing).not.toMatch(/project/i)
    expect(panel.search_denied).toMatch(/this folder/i)
  })

  it('states a hand-off decision on every ErrorNotice it renders', () => {
    // `errors-use-error-notice` (AUTOSDE.yaml, blocking) wants each notice to carry `askAgent` or a
    // `No hand-off:` naming the draft; this menu sits over the composer's unsent message.
    const src = readFileSync('src/components/FilePickerMenu.tsx', 'utf8')
    const notices = src.split('<ErrorNotice').length - 1
    expect(notices).toBeGreaterThan(0)
    // One decision per notice: the comment sits immediately above each one.
    const decisions = src.split('No hand-off:').length - 1
    expect(decisions).toBe(notices)
    // EVERY decision must name the concrete draft: the rule blocks on a comment that names none,
    // so counting only one occurrence would pass while a sibling notice carried a generic excuse.
    const concrete = src.split("No hand-off: the composer's unsent message").length - 1
    expect(concrete).toBe(notices)
    // askAgent must stay OFF here -- the hand-off navigates and would discard that draft.
    expect(src).not.toMatch(/askAgent/)
  })

  it('labels the recovery cue with the same word the pickers use', () => {
    // Synonym drift: the pickers' control is "Retry"; the @-menu appended "Try again".
    const src = readFileSync('src/components/FilePickerMenu.tsx', 'utf8')
    expect(src).toMatch(/components\.filePickerMenu\.retry/)
    expect(src).not.toMatch(/errorBoundary\.try_again/)
  })

  it('keeps the CACHED rows and annotates them when a later fetch fails', async () => {
    // A refetch failing on the same key leaves the last good page in `data`, so isError is
    // true while results.length is still > 0 -- and those rows are still valid options.
    fileSearch.mockResolvedValueOnce({
      results: [{ path: '/p/kept.ts', name: 'kept.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const { queryClient } = mount({ query: 'zz' })
    expect(await screen.findByText('kept.ts')).toBeInTheDocument()

    fileSearch.mockRejectedValue(new ApiError(500, 'boom'))
    await queryClient.refetchQueries({ queryKey: ['file-search'] })

    expect(await screen.findByRole('alert')).toHaveTextContent(/File search failed/)
    expect(screen.getByText('kept.ts')).toBeInTheDocument()
  })

  it('leaves the annotated rows selectable, so Enter still picks instead of sending', async () => {
    // The rows are on screen, so they must stay in the collection the keyboard hook walks --
    // and because that count is non-zero, the Enter-release gate does not fire.
    fileSearch.mockResolvedValueOnce({
      results: [{ path: '/p/kept.ts', name: 'kept.ts', size: 1, mtime: 1 }],
      root: '/p',
    } as never)
    const { queryClient, onSelect } = mount({ query: 'zz' })
    expect(await screen.findByText('kept.ts')).toBeInTheDocument()

    fileSearch.mockRejectedValue(new ApiError(500, 'boom'))
    await queryClient.refetchQueries({ queryKey: ['file-search'] })
    await screen.findByRole('alert')

    fireEvent.keyDown(document, { key: 'ArrowDown' })
    fireEvent.keyDown(document, { key: 'Enter' })
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onSelect.mock.calls[0][0].path).toContain('kept.ts')
  })
})
