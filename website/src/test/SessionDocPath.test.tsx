/**
 * The session-document path in the artifacts list ("From your chats", table and
 * gallery): a button that opens the file in the user's editor through the shell
 * bridge when that can work, and static text when it cannot.
 */
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SessionDocRow } from '../components/library/LibraryTable'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { stubPathKinds, kindsRequestCount, type StubKind } from './pathKindStub'
import type { SessionDoc } from '../types'

// `directLocal`: the browser is on the gateway's own machine, the only place the
// shell's launch can work.
const brandingEnv = vi.hoisted(() => ({ directLocal: true }))
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: brandingEnv.directLocal }),
}))

// The row's star reads the gateway connection; these cases are about the path,
// so they run connected (LibraryTable.folderMenuOffline covers the offline star).
vi.mock('../hooks/useConnected', () => ({ useConnected: () => true }))

const PATH = '/ws/research/FINDINGS.md'
const realFetch = globalThis.fetch

const mkDoc = (path = PATH): SessionDoc => ({
  path,
  name: path.split('/').pop() || path,
  updated_at: '2026-05-21T22:00:00',
  session_key: 'dashboard_s1',
  session_title: 'Research session',
  message_ts: 'm1',
  saved: false,
  slug: '',
})

function renderRow(onPreview = vi.fn()) {
  render(
    <table>
      <tbody>
        <SessionDocRow d={mkDoc()} busy={false} onMaterialize={vi.fn()} onPreview={onPreview} />
      </tbody>
    </table>,
  )
  return onPreview
}

function withBridge(open = vi.fn().mockResolvedValue({ ok: true })) {
  ;(window as { fileOpenAPI?: unknown }).fileOpenAPI = { open }
  return open
}

function stubKind(kind: StubKind) {
  return stubPathKinds(() => kind)
}

// The button appears only after a chained wait: the effect enqueues the path,
// the microtask flush sends the /api/file-kinds batch, and the stubbed reply
// settles the probe.
const PROBE_TIMEOUT = { timeout: 5000 }
const openButton = () => screen.findByRole('button', { name: `Open ${PATH} in editor` }, PROBE_TIMEOUT)

beforeEach(() => {
  brandingEnv.directLocal = true
  __resetPathKindCache()
})

afterEach(() => {
  delete (window as { fileOpenAPI?: unknown }).fileOpenAPI
  globalThis.fetch = realFetch
  vi.restoreAllMocks()
})

describe('SessionDocPath', () => {
  it('opens a confirmed file in the editor without also opening the preview', async () => {
    const open = withBridge()
    stubKind('file')
    const onPreview = renderRow()

    fireEvent.click(await openButton())

    await waitFor(() => expect(open).toHaveBeenCalledWith(PATH), PROBE_TIMEOUT)
    expect(onPreview).not.toHaveBeenCalled()
  })

  it('is keyboard-operable: Enter on the focused path opens the editor, not the preview', async () => {
    const user = userEvent.setup()
    const open = withBridge()
    stubKind('file')
    const onPreview = renderRow()

    const btn = await openButton()
    btn.focus()
    await user.keyboard('{Enter}')

    await waitFor(() => expect(open).toHaveBeenCalledWith(PATH), PROBE_TIMEOUT)
    expect(onPreview).not.toHaveBeenCalled()
  })

  it('stays static text in a plain browser tab and never probes the path', async () => {
    const fetchFn = stubKind('file')
    const onPreview = renderRow()

    const text = screen.getByText(PATH)
    expect(text.closest('button')).toBeNull()
    // The row keeps its own gesture: clicking the path opens the preview.
    fireEvent.click(text)
    expect(onPreview).toHaveBeenCalledTimes(1)
    await Promise.resolve()
    expect(kindsRequestCount(fetchFn)).toBe(0)
  })

  it('stays static text when the gateway does not confirm a regular file', async () => {
    withBridge()
    const fetchFn = stubKind(null)
    renderRow()

    // Chained on the effect enqueueing the path and the microtask flush.
    await waitFor(() => expect(kindsRequestCount(fetchFn)).toBe(1), PROBE_TIMEOUT)
    // Let the probe's answer render before asserting nothing became a button.
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.queryByRole('button', { name: `Open ${PATH} in editor` })).toBeNull()
    expect(screen.getByText(PATH).closest('button')).toBeNull()
  })

  it('shows the shell refusal in place, and dismissing it does not open the preview', async () => {
    withBridge(vi.fn().mockResolvedValue({ ok: false, error: 'no application to open .md' }))
    stubKind('file')
    const onPreview = renderRow()

    fireEvent.click(await openButton())

    // Chained on the bridge's promise settling after the click.
    const notice = await screen.findByTestId('session-doc-editor-error', {}, PROBE_TIMEOUT)
    expect(notice.textContent).toContain("Couldn't open this file in your editor")
    expect(notice.textContent).toContain('no application to open .md')
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(screen.queryByTestId('session-doc-editor-error')).toBeNull(), PROBE_TIMEOUT)
    expect(onPreview).not.toHaveBeenCalled()
  })

  it('stays static text in a desktop window on a remote or tunnelled gateway, and never probes', async () => {
    brandingEnv.directLocal = false
    withBridge()
    const fetchFn = stubKind('file')
    renderRow()
    // Flush the microtask a probe would be sent from.
    await new Promise((r) => setTimeout(r, 0))
    expect(kindsRequestCount(fetchFn)).toBe(0)
    expect(screen.getByText(PATH).closest('button')).toBeNull()
  })

  it.each([
    ['a type the shell will not open', '/ws/reports/q3-review.pdf'],
    ['a relative path', 'notes/plan.md'],
  ])('never probes or offers a launch for %s', async (_what, path) => {
    withBridge()
    const fetchFn = stubKind('file')
    render(
      <table>
        <tbody>
          <SessionDocRow d={mkDoc(path)} busy={false} onMaterialize={vi.fn()} onPreview={vi.fn()} />
        </tbody>
      </table>,
    )
    // Flush the microtask a probe would be sent from.
    await new Promise((r) => setTimeout(r, 0))
    expect(kindsRequestCount(fetchFn)).toBe(0)
    expect(screen.getByText(path).closest('button')).toBeNull()
  })
})
