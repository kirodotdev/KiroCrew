/**
 * "Import a session from a file" — the session menu's file-import row.
 *
 * The other half of `ExportSessionItem`: until this row existed the product
 * could WRITE a `.kcsession.json.gz` and had no way to read one back, so the
 * contract worth locking is that the file the export row produces is the file
 * this row consumes, unaltered.
 *
 * Four contracts are locked:
 *   (1) the file's BYTES are posted as they came off disk — no gunzip, no
 *       re-encode, no JSON wrapper — because the endpoint decides the format
 *       from them and a browser that unpacked first would defeat that;
 *   (2) the pick survives the menu closing: the picker blurs the window and
 *       Radix closes the menu on blur, so the file input must not live in the
 *       row, and the imported session is opened so the outcome is visible;
 *   (3) a refusal surfaces the endpoint's own message rather than a generic one;
 *   (4) the same file can be chosen twice and imports twice, which is the
 *       documented "import only ever adds" behaviour and is defeated by a file
 *       input that keeps its previous value.
 */
import * as React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mocks = vi.hoisted(() => ({ importSessionFromFile: vi.fn() }))
vi.mock('../api/client', () => ({
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

const switched = vi.hoisted(() => [] as unknown[])
const chatState = vi.hoisted(() => ({ activeSlot: 'origin' as string | null }))
vi.mock('../store', () => ({
  useAppDispatch: () => (a: unknown) => a,
  useAppStore: () => ({ getState: () => ({ chat: chatState }) }),
}))
const slotRefresh = vi.hoisted(() => ({ rejected: false }))
vi.mock('../store/dashboardSlice', () => ({
  // The action a real dispatch of the thunk resolves to: fulfilled, or rejected
  // with RTK's async-thunk metadata so `isRejected` recognises it.
  fetchSlots: () => (slotRefresh.rejected
    ? { type: 'dashboard/fetchSlots/rejected', meta: { requestId: 'r1', requestStatus: 'rejected' }, error: { message: 'HTTP 500' } }
    : { type: 'dashboard/fetchSlots/fulfilled', meta: { requestId: 'r1', requestStatus: 'fulfilled' } }),
}))
vi.mock('../store/chatSlice', () => ({
  switchSlot: (arg: unknown) => { switched.push(arg); return { type: 'test/switchSlot' } },
}))

import ImportSessionItem, { ImportSessionOutcomeNotice } from '../components/ImportSessionItem'
import { ApiError } from '../api/apiError'
import { __resetErrorJournalForTests, consumeChatHandoff, recordError } from '../utils/errorReport'

/** A plain stand-in for the Radix menu-item primitive. */
function StubItem({ disabled, onSelect, children }: {
  disabled?: boolean
  onSelect?: (event: Event) => void
  children?: React.ReactNode
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      data-testid="row"
      onClick={() => onSelect?.(new Event('select'))}
    >
      {children}
    </button>
  )
}

function withQuery(node: React.ReactNode) {
  return (
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}
    >
      {node}
    </QueryClientProvider>
  )
}

function renderRow() {
  return render(withQuery(<ImportSessionItem Item={StubItem} />))
}

/** The input the row opened the picker from — owned by `document.body`. */
function pickerInput() {
  return document.body.querySelector('input[type="file"]') as HTMLInputElement | null
}

/** Select the row, then choose `file` in the picker it opened. */
async function pick(file: File) {
  await userEvent.click(screen.getByTestId('row'))
  const input = pickerInput()
  expect(input).not.toBeNull()
  await userEvent.upload(input!, file)
}

const GZ_MAGIC = new Uint8Array([0x1f, 0x8b, 0x08, 0x00])

function exportedFile(name = 'chat.kcsession.json.gz') {
  // Named and typed the way a real pick of an exported file is, and carrying
  // gzip's own magic so a test that asserts the bytes travelled is asserting
  // something a JSON round-trip could not survive.
  return new File([GZ_MAGIC], name, { type: 'application/gzip' })
}

describe('ImportSessionItem', () => {
  beforeEach(() => {
    mocks.importSessionFromFile.mockReset()
    switched.length = 0
    chatState.activeSlot = 'origin'
    slotRefresh.rejected = false
    window.history.pushState({}, '', '/chat')
    pickerInput()?.remove()
    __resetErrorJournalForTests()
    consumeChatHandoff()
    mocks.importSessionFromFile.mockResolvedValue({
      ok: true,
      key: 'imported-1',
      title: '⇄ chat (from mac)',
      messages: 3,
      resume_mode: 'prefix',
    })
  })

  it('posts the picked file unchanged', async () => {
    renderRow()
    const picked = exportedFile()

    await pick(picked)

    await waitFor(() => expect(mocks.importSessionFromFile).toHaveBeenCalledTimes(1))
    const sent = mocks.importSessionFromFile.mock.calls[0][0] as File
    // The File OBJECT itself, so the transport sends the bytes verbatim. A
    // component that gunzipped or JSON-wrapped first would hand over something
    // else here.
    expect(sent).toBe(picked)
    expect(new Uint8Array(await sent.arrayBuffer())).toEqual(GZ_MAGIC)
  })

  it('names what landed, not just that something did', async () => {
    renderRow()

    await pick(exportedFile())

    // The title, because a new session appears somewhere in a sidebar that may be
    // scrolled or collapsed and this row is the only place that knows which one
    // it is. Asserting only /imported/i would pass on a bare "Imported".
    expect(await screen.findByText(/Imported: ⇄ chat \(from mac\)/)).toBeInTheDocument()
  })

  it('falls back to the bare outcome when the response names no title', async () => {
    mocks.importSessionFromFile.mockResolvedValue({
      ok: true,
      key: 'imported-1',
      title: '',
      messages: 3,
      resume_mode: 'prefix',
    })
    renderRow()

    await pick(exportedFile())

    expect(await screen.findByText(/^Imported$/)).toBeInTheDocument()
  })

  it('surfaces the endpoint refusal rather than a generic failure', async () => {
    mocks.importSessionFromFile.mockRejectedValue(
      new Error('could not decompress the bundle'),
    )
    renderRow()

    await pick(exportedFile())

    expect(
      await screen.findByText(/could not decompress the bundle/i),
    ).toBeInTheDocument()
  })

  /** `{"error": …, "code": …}` — the envelope every refusal on this route uses. */
  const refusal = (status: number, error: string, code: string) =>
    new ApiError(status, error, JSON.stringify({ error, code }))

  it('speaks a refusal it recognises in the vocabulary of a file', async () => {
    // The person picked a FILE. The endpoint's own wording is written for the
    // wire -- "bundle", a byte ceiling -- and is English beside a row localized
    // into 13 languages, so a known code is spoken from the catalog instead.
    mocks.importSessionFromFile.mockRejectedValue(
      refusal(400, 'compressed bundle expands past 65 MiB', 'transfer_bundle_too_large'),
    )
    renderRow()

    await pick(exportedFile())

    expect(await screen.findByText(/this file is too large to import/i)).toBeInTheDocument()
    expect(screen.queryByText(/bundle/i)).not.toBeInTheDocument()
  })

  it('falls through to the endpoint text for a refusal it cannot translate', async () => {
    // The fallback is deliberate, not an oversight: a validation code added after
    // this map was written still says what the server said, which is far more use
    // than a generic failure that hides it.
    mocks.importSessionFromFile.mockRejectedValue(
      refusal(400, 'bundle carries no messages', 'transfer_bundle_empty'),
    )
    renderRow()

    await pick(exportedFile())

    expect(await screen.findByText(/bundle carries no messages/i)).toBeInTheDocument()
  })

  it('imports the same file twice', async () => {
    renderRow()
    const picked = exportedFile()

    await pick(picked)
    await waitFor(() => expect(mocks.importSessionFromFile).toHaveBeenCalledTimes(1))
    await pick(picked)

    // Two sessions, not one: each pick uses a fresh input, so the second choice
    // of the SAME file still fires a change event.
    await waitFor(() => expect(mocks.importSessionFromFile).toHaveBeenCalledTimes(2))
    expect(pickerInput()).toBeNull()
  })

  it('shows a spinner and disables the row while the import is in flight', async () => {
    // Hold the mutation open so the `importing` branch (the spinner and the
    // row's disabled state) is observable rather than flashing past.
    let resolve!: (r: unknown) => void
    mocks.importSessionFromFile.mockImplementation(
      () => new Promise((res) => { resolve = res }),
    )
    renderRow()

    await pick(exportedFile())

    // The row disables itself so a second pick cannot race the first.
    await waitFor(() => expect(screen.getByTestId('row')).toBeDisabled())

    resolve({ ok: true, key: 'imported-1', title: 'landed', messages: 1, resume_mode: 'prefix' })

    // And re-enables once the outcome lands.
    await waitFor(() => expect(screen.getByTestId('row')).not.toBeDisabled())
    expect(await screen.findByText(/Imported: landed/)).toBeInTheDocument()
  })

  it('translates each refusal code it recognises', async () => {
    // Exercise the remaining REFUSAL_COPY thunks, not just the one large-file
    // case: every recognised code must speak from the catalog, never the wire.
    const cases: Array<[string, RegExp]> = [
      ['transfer_invalid_gzip', /damaged/i],
      ['transfer_invalid_json', /not an exported session/i],
      ['transfer_body_not_object', /not an exported session/i],
      ['transfer_body_unreadable', /could not be uploaded/i],
      ['transfer_expansion_busy', /too many imports|try again/i],
      ['transfer_uploads_busy', /too many imports|try again/i],
    ]
    for (const [code, pattern] of cases) {
      mocks.importSessionFromFile.mockReset()
      mocks.importSessionFromFile.mockRejectedValue(
        refusal(400, 'wire-only english wording', code),
      )
      const { unmount } = renderRow()

      await pick(exportedFile())

      expect((await screen.findAllByText(pattern)).length).toBeGreaterThan(0)
      // The endpoint's own English wording is never shown for a recognised code.
      expect(screen.queryByText(/wire-only english wording/i)).not.toBeInTheDocument()
      unmount()
    }
  })

  it('shows a generic failure when the rejection carries no readable message', async () => {
    // Not an ApiError and not an Error with a message: the final fallback in
    // refusalMessage, which must still say SOMETHING rather than render blank.
    mocks.importSessionFromFile.mockRejectedValue('a bare string, no .message')
    renderRow()

    await pick(exportedFile())

    // The generic fallback copy, matched exactly so it does not collide with the
    // "Failed" title; it renders in both the inline notice and the menu-item one.
    const shown = await screen.findAllByText('The import failed')
    expect(shown.length).toBeGreaterThan(0)
  })

  it('opens the picker without closing the menu', async () => {
    const selects: Event[] = []
    function Recorder({ onSelect, children }: {
      onSelect?: (event: Event) => void
      children?: React.ReactNode
    }) {
      return (
        <button
          type="button"
          data-testid="row"
          onClick={() => {
            const e = new Event('select', { cancelable: true })
            onSelect?.(e)
            selects.push(e)
          }}
        >
          {children}
        </button>
      )
    }
    render(withQuery(<ImportSessionItem Item={Recorder} />))

    await userEvent.click(screen.getByTestId('row'))

    // Radix closes the menu unless the select event is prevented, and a menu
    // that closes takes the outcome note with it.
    expect(selects).toHaveLength(1)
    expect(selects[0].defaultPrevented).toBe(true)
  })

  it('still imports when the menu unmounts while the picker is open', async () => {
    // The real sequence: the native picker blurs the window, Radix closes the
    // menu on blur, and the row unmounts BEFORE the user confirms a file.
    const { unmount } = renderRow()
    await userEvent.click(screen.getByTestId('row'))
    const input = pickerInput()
    unmount()

    await userEvent.upload(input!, exportedFile())

    await waitFor(() => expect(mocks.importSessionFromFile).toHaveBeenCalledTimes(1))
    // The imported session is opened: the one outcome signal that survives
    // the menu having closed.
    await waitFor(() => expect(switched).toEqual([{ key: 'imported-1', keepTargetOnMissing: true }]))
    expect(pickerInput()).toBeNull()
  })

  it('opens the imported session wherever the user is, and shows nothing when the list refresh fails', async () => {
    // A successful import always opens: the page, a session opened meanwhile
    // and a failed slot-list refresh change nothing, because the switch loads
    // the session by key and the next slot frame lists it.
    window.history.pushState({}, '', '/popout/chat/origin')
    slotRefresh.rejected = true
    try {
      render(<ImportSessionOutcomeNotice />)
      const { unmount } = renderRow()
      await userEvent.click(screen.getByTestId('row'))
      const input = pickerInput()
      unmount()
      chatState.activeSlot = 'opened-meanwhile'

      await userEvent.upload(input!, exportedFile())

      await waitFor(() => expect(switched).toEqual([{ key: 'imported-1', keepTargetOnMissing: true }]))
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    } finally {
      window.history.pushState({}, '', '/chat')
    }
  })

  it('renders a refusal the unmounted row can no longer show in the app shell', async () => {
    mocks.importSessionFromFile.mockRejectedValue(new Error('could not decompress the bundle'))
    const alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
    render(<ImportSessionOutcomeNotice />)
    const { unmount } = renderRow()
    await userEvent.click(screen.getByTestId('row'))
    const input = pickerInput()
    unmount()

    await userEvent.upload(input!, exportedFile())

    // In-page through ErrorNotice, never a blocking native alert.
    expect(await screen.findByText(/could not decompress the bundle/)).toBeInTheDocument()
    expect(alert).not.toHaveBeenCalled()
    expect(switched).toEqual([])
    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByText(/could not decompress the bundle/)).not.toBeInTheDocument()
    alert.mockRestore()
  })

  it('does not repeat the generic failure as both title and message', async () => {
    mocks.importSessionFromFile.mockRejectedValue('a bare string, no .message')
    render(<ImportSessionOutcomeNotice />)
    const { unmount } = renderRow()
    await userEvent.click(screen.getByTestId('row'))
    const input = pickerInput()
    unmount()

    await userEvent.upload(input!, exportedFile())

    expect(await screen.findAllByText('The import failed')).toHaveLength(1)
    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }))
  })

  it('clears the refusal once it is handed to the agent', async () => {
    mocks.importSessionFromFile.mockRejectedValue(new Error('could not decompress the bundle'))
    render(<ImportSessionOutcomeNotice />)
    const { unmount } = renderRow()
    await userEvent.click(screen.getByTestId('row'))
    const input = pickerInput()
    unmount()
    await userEvent.upload(input!, exportedFile())
    await screen.findByText(/could not decompress the bundle/)

    await userEvent.click(screen.getByRole('button', { name: /ask the agent/i }))

    await waitFor(() => expect(screen.queryByText(/could not decompress the bundle/)).not.toBeInTheDocument())
  })

  it('hands a translated refusal to the agent with its request context', async () => {
    // The row speaks a recognised code from the catalog, and that wording is not
    // the journal entry's message. The hand-off must still carry the report the
    // client recorded, or the agent is told a sentence and nothing about the call.
    const raw = 'compressed bundle expands past 65 MiB'
    recordError({ source: 'api', message: raw, status: 400, code: 'transfer_bundle_too_large', endpoint: '/api/chat/slots/import' })
    mocks.importSessionFromFile.mockRejectedValue(refusal(400, raw, 'transfer_bundle_too_large'))
    render(<ImportSessionOutcomeNotice />)
    const { unmount } = renderRow()
    await userEvent.click(screen.getByTestId('row'))
    const input = pickerInput()
    unmount()
    await userEvent.upload(input!, exportedFile())
    await screen.findByText(/this file is too large to import/i)

    await userEvent.click(screen.getByRole('button', { name: /ask the agent/i }))

    expect(consumeChatHandoff()).toContain('/api/chat/slots/import')
  })

  it('hands the mounted row\'s translated refusal to the agent with its request context', async () => {
    const raw = 'compressed bundle expands past 65 MiB'
    recordError({ source: 'api', message: raw, status: 400, code: 'transfer_bundle_too_large', endpoint: '/api/chat/slots/import' })
    mocks.importSessionFromFile.mockRejectedValue(refusal(400, raw, 'transfer_bundle_too_large'))
    renderRow()
    await pick(exportedFile())
    await screen.findByText(/this file is too large to import/i)

    await userEvent.click(screen.getByRole('button', { name: /ask the agent/i }))

    expect(consumeChatHandoff()).toContain('/api/chat/slots/import')
  })

  it('keeps a remounted row disabled while an import is still in flight', async () => {
    // Reopening the menu mounts a fresh row; it must not offer a second import
    // while the first, started from a row that is gone, is still uploading.
    let resolve!: (r: unknown) => void
    mocks.importSessionFromFile.mockImplementation(() => new Promise((res) => { resolve = res }))
    const first = renderRow()
    await userEvent.click(screen.getByTestId('row'))
    const input = pickerInput()
    first.unmount()
    await userEvent.upload(input!, exportedFile())
    await waitFor(() => expect(mocks.importSessionFromFile).toHaveBeenCalledTimes(1))

    renderRow()
    expect(screen.getByTestId('row')).toBeDisabled()

    resolve({ ok: true, key: 'imported-1', title: 'landed', messages: 1, resume_mode: 'prefix' })
    await waitFor(() => expect(screen.getByTestId('row')).not.toBeDisabled())
  })
})
