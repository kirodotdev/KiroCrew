import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGet = vi.fn()
const mockPost = vi.fn()
const mockRaw = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost, raw: mockRaw }),
  useAppInfo: () => ({ name: 'host', version: '0', permissions: { api: [], events: [] } }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: () => <div data-testid="chat-message-list" />,
}))

import ChatEmbed from '../app-sdk/ChatEmbed'
import { AppSdkContext, apiPathAllowed, type AppSdkContextValue } from '../app-sdk/scopedApi'
import { EMBED_UPLOAD_PATH } from '../app-sdk/useEmbedAttachments'
import { ApiError } from '../api/apiError'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'

let queryClient: QueryClient

/** Mount the embed under a host context that declares `api`. */
function mount(api: string[], props: Partial<React.ComponentProps<typeof ChatEmbed>> = {}) {
  const ctx = {
    api: { get: mockGet, post: mockPost, raw: mockRaw },
    info: { name: 'host', version: '0', permissions: { api, events: [] } },
    subscribe: () => () => {},
    navigate: () => {},
    notify: () => {},
  } as unknown as AppSdkContextValue
  return render(
    <QueryClientProvider client={queryClient}>
      <AppSdkContext.Provider value={ctx}>
        <ChatEmbed slotKey="s1" {...props} />
      </AppSdkContext.Provider>
    </QueryClientProvider>,
  )
}

const png = () => new File(['x'], 'shot.png', { type: 'image/png' })
const pdf = () => new File(['x'], 'spec.pdf', { type: 'application/pdf' })
const uploadReturns = (paths: string[]) =>
  mockRaw.mockResolvedValueOnce(new Response(JSON.stringify({ paths }), { status: 200 }))

const GRANTED = ['/api/chat', '/api/chat/slots', EMBED_UPLOAD_PATH]
const UNGRANTED = ['/api/chat', '/api/chat/slots']

beforeEach(() => {
  vi.clearAllMocks()
  resetEmbedRecoveryForTests()
  // A Once-queued response a failed test never consumed must not leak.
  mockRaw.mockReset()
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
  mockPost.mockResolvedValue({})
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

describe('apiPathAllowed', () => {
  it('matches the scoped client rules and grants nothing implicitly', () => {
    expect(apiPathAllowed([EMBED_UPLOAD_PATH], EMBED_UPLOAD_PATH)).toBe(true)
    expect(apiPathAllowed(['/api/upload/*'], EMBED_UPLOAD_PATH)).toBe(true)
    expect(apiPathAllowed(['/api/chat', '/api/chat/slots'], EMBED_UPLOAD_PATH)).toBe(false)
    expect(apiPathAllowed([], EMBED_UPLOAD_PATH)).toBe(false)
    expect(apiPathAllowed([EMBED_UPLOAD_PATH], '/api/upload/file/../../secret')).toBe(false)
    expect(apiPathAllowed(['/api/*'], '//evil.example/api/upload/file')).toBe(false)
  })
})

describe('ChatEmbed attachments without the upload grant', () => {
  it('hides the attach control and uploads nothing', async () => {
    await act(async () => { mount(UNGRANTED) })
    expect(screen.queryByLabelText('Attach files')).toBeNull()
    expect(screen.queryByTestId('chat-embed-file-input')).toBeNull()
  })

  it('explains a pasted file instead of dropping it silently', async () => {
    await act(async () => { mount(UNGRANTED) })
    const box = screen.getByLabelText('Chat message')
    const notPrevented = fireEvent.paste(box, { clipboardData: { files: [png()], types: ['Files'] } })
    expect(notPrevented).toBe(false)
    expect(await screen.findByTestId('chat-embed-attach-notice')).toHaveTextContent("This app doesn't accept files")
    // The page is named by its own sidebar label, not a description of it.
    expect(screen.getByTestId('chat-embed-attach-notice')).toHaveTextContent('Attach them in Sessions instead.')
    expect(mockRaw).not.toHaveBeenCalled()
  })

  it('swallows a dropped file so the page does not navigate to it, and explains', async () => {
    await act(async () => { mount(UNGRANTED) })
    const box = screen.getByLabelText('Chat message')
    const notPrevented = fireEvent.drop(box, { dataTransfer: { files: [pdf()], types: ['Files'] } })
    expect(notPrevented).toBe(false)
    expect(await screen.findByTestId('chat-embed-attach-notice')).toBeInTheDocument()
    expect(mockRaw).not.toHaveBeenCalled()
  })

  it('fails closed with no host context at all', async () => {
    await act(async () => {
      render(<QueryClientProvider client={queryClient}><ChatEmbed slotKey="s1" /></QueryClientProvider>)
    })
    expect(screen.queryByLabelText('Attach files')).toBeNull()
  })
})

describe('ChatEmbed attachments with the upload grant', () => {
  it('keeps the send control while a turn runs and a file is staged', async () => {
    // Staged files are a draft, as in the main composer: Stop must not take
    // the only control that sends them.
    mockGet.mockResolvedValue({ messages: [{ role: 'user', content: 'hi', ts: 1 }], running: true, title: '' })
    uploadReturns(['/up/spec.pdf'])
    await act(async () => { mount(GRANTED) })
    expect(await screen.findByTestId('stop-button-armed')).toBeInTheDocument()
    fireEvent.change(screen.getByTestId('chat-embed-file-input'), { target: { files: [pdf()] } })
    await waitFor(() => expect(screen.queryByTestId('stop-button-armed')).toBeNull())
    expect(screen.getByRole('button', { name: 'Queue message' })).toBeInTheDocument()
  })

  it('leaves a plain-text paste to the textarea', async () => {
    await act(async () => { mount(GRANTED) })
    const box = screen.getByLabelText('Chat message')
    const notPrevented = fireEvent.paste(box, { clipboardData: { files: [], types: ['text/plain'] } })
    expect(notPrevented).toBe(true)
    expect(mockRaw).not.toHaveBeenCalled()
  })

  it('uploads a pasted image through the scoped client and sends it inline', async () => {
    uploadReturns(['/up/shot.png'])
    await act(async () => { mount(GRANTED) })
    expect(screen.getByLabelText('Attach files')).toBeInTheDocument()
    const box = screen.getByLabelText('Chat message')
    fireEvent.paste(box, { clipboardData: { files: [png()], types: ['Files'] } })
    await waitFor(() => expect(mockRaw).toHaveBeenCalledTimes(1))
    const [path, init] = mockRaw.mock.calls[0]
    expect(path).toBe(EMBED_UPLOAD_PATH)
    expect(init.method).toBe('POST')
    expect(init.body).toBeInstanceOf(FormData)
    expect((init.body as FormData).getAll('file')).toHaveLength(1)

    // An attachment alone is a sendable turn.
    const sendBtn = screen.getByLabelText('Send message')
    await waitFor(() => expect(sendBtn).not.toBeDisabled())
    await act(async () => { fireEvent.click(sendBtn) })
    const chat = mockPost.mock.calls.find(c => c[0] === '/api/chat?ws=1')
    expect(chat).toBeTruthy()
    expect(chat![1].message).toContain('/up/shot.png')
  })

  it('sends a picked non-image file as meta.files with its marker', async () => {
    uploadReturns(['/up/spec.pdf'])
    await act(async () => { mount(GRANTED) })
    const input = screen.getByTestId('chat-embed-file-input') as HTMLInputElement
    fireEvent.change(input, { target: { files: [pdf()] } })
    await waitFor(() => expect(mockRaw).toHaveBeenCalledTimes(1))
    const box = screen.getByLabelText('Chat message')
    fireEvent.change(box, { target: { value: 'review this' } })
    const sendBtn = screen.getByLabelText('Send message')
    await waitFor(() => expect(sendBtn).not.toBeDisabled())
    await act(async () => { fireEvent.click(sendBtn) })
    const chat = mockPost.mock.calls.find(c => c[0] === '/api/chat?ws=1')!
    expect(chat[1].meta).toEqual({ files: ['/up/spec.pdf'], sendId: expect.any(String) })
    expect(chat[1].message).toContain('[attached_file 1]')
    expect(chat[1].message).toContain('review this')
  })

  it('keeps the staged files when the send fails', async () => {
    uploadReturns(['/up/spec.pdf'])
    await act(async () => { mount(GRANTED) })
    fireEvent.drop(screen.getByLabelText('Chat message'), { dataTransfer: { files: [pdf()], types: ['Files'] } })
    const sendBtn = screen.getByLabelText('Send message')
    await waitFor(() => expect(sendBtn).not.toBeDisabled())
    mockPost.mockRejectedValueOnce(new Error('offline'))
    await act(async () => { fireEvent.click(sendBtn) })
    // The file is staged again, so a retry carries it without a new upload.
    await waitFor(() => expect(sendBtn).not.toBeDisabled())
    await act(async () => { fireEvent.click(sendBtn) })
    const chats = mockPost.mock.calls.filter(c => c[0] === '/api/chat?ws=1')
    expect(chats).toHaveLength(2)
    expect(chats[1][1].meta).toEqual({ files: ['/up/spec.pdf'], sendId: expect.any(String) })
    expect(mockRaw).toHaveBeenCalledTimes(1)
  })

  it('gives the staged files back when the server refuses the send', async () => {
    uploadReturns(['/up/spec.pdf'])
    await act(async () => { mount(GRANTED) })
    fireEvent.drop(screen.getByLabelText('Chat message'), { dataTransfer: { files: [pdf()], types: ['Files'] } })
    const sendBtn = screen.getByLabelText('Send message')
    await waitFor(() => expect(sendBtn).not.toBeDisabled())
    mockPost.mockRejectedValueOnce(new ApiError(409, 'API 409: {"error":"slot agent mismatch"}', '{"error":"slot agent mismatch"}'))
    await act(async () => { fireEvent.click(sendBtn) })
    await waitFor(() => expect(screen.getByRole('group', { name: '/up/spec.pdf' })).toBeInTheDocument())
  })

  it('unstages the handed-back files once the poll proves the send was delivered', async () => {
    uploadReturns(['/up/spec.pdf'])
    // The poll answers with this send's own user row once the send has gone out.
    const sentId = () => (mockPost.mock.calls.find(c => c[0] === '/api/chat?ws=1')?.[1] as { meta?: { sendId?: string } } | undefined)?.meta?.sendId
    mockGet.mockImplementation(async () => {
      const id = sentId()
      return { messages: id ? [{ role: 'user', content: 'x', cls: '', meta: { sendId: id } }] : [], running: false, title: '' }
    })
    await act(async () => { mount(GRANTED) })
    fireEvent.drop(screen.getByLabelText('Chat message'), { dataTransfer: { files: [pdf()], types: ['Files'] } })
    const sendBtn = screen.getByLabelText('Send message')
    await waitFor(() => expect(sendBtn).not.toBeDisabled())
    // A rejected fetch is unconfirmed: the files come back, then proof takes them out.
    mockPost.mockRejectedValueOnce(new Error('offline'))
    await act(async () => { fireEvent.click(sendBtn) })
    expect(sentId()).toMatch(/^s-/)
    await waitFor(() => expect(screen.queryByRole('group', { name: '/up/spec.pdf' })).toBeNull())
    expect(sendBtn).toBeDisabled()
  })

  it('hands meta to a host onSend', async () => {
    uploadReturns(['/up/spec.pdf'])
    const onSend = vi.fn().mockResolvedValue(undefined)
    await act(async () => { mount(GRANTED, { onSend }) })
    fireEvent.drop(screen.getByLabelText('Chat message'), { dataTransfer: { files: [pdf()], types: ['Files'] } })
    const sendBtn = screen.getByLabelText('Send message')
    await waitFor(() => expect(sendBtn).not.toBeDisabled())
    await act(async () => { fireEvent.click(sendBtn) })
    expect(onSend).toHaveBeenCalledWith(expect.stringContaining('[attached_file 1]'), { files: ['/up/spec.pdf'], sendId: expect.any(String) })
  })

  it('keeps the text of a paste that carries text and an image of it', async () => {
    await act(async () => { mount(GRANTED) })
    const box = screen.getByLabelText('Chat message')
    const notPrevented = fireEvent.paste(box, { clipboardData: { files: [png()], types: ['text/plain', 'Files'] } })
    expect(notPrevented).toBe(true)
    expect(mockRaw).not.toHaveBeenCalled()
    expect(screen.queryByTestId('chat-embed-attach-notice')).toBeNull()
  })

  it('does not send on Enter while an upload is still running', async () => {
    let answer: (r: Response) => void = () => {}
    mockRaw.mockReturnValueOnce(new Promise<Response>(r => { answer = r }))
    await act(async () => { mount(GRANTED) })
    const box = screen.getByLabelText('Chat message')
    fireEvent.paste(box, { clipboardData: { files: [pdf()], types: ['Files'] } })
    await waitFor(() => expect(mockRaw).toHaveBeenCalledTimes(1))
    fireEvent.change(box, { target: { value: 'see attached' } })
    await act(async () => { fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' }) })
    expect(mockPost.mock.calls.find(c => c[0] === '/api/chat?ws=1')).toBeUndefined()
    expect(box).toHaveValue('see attached')
    await act(async () => { answer(new Response(JSON.stringify({ paths: ['/up/spec.pdf'] }), { status: 200 })) })
    await act(async () => { fireEvent.keyDown(box, { key: 'Enter', code: 'Enter' }) })
    const chat = mockPost.mock.calls.find(c => c[0] === '/api/chat?ws=1')!
    expect(chat[1].meta).toEqual({ files: ['/up/spec.pdf'], sendId: expect.any(String) })
  })

  // This surface carries no `meta.dirs`, so a folder token must reach the
  // agent as typed, the same with a file as without one.
  it.each([
    ['a non-image file', pdf, '/up/spec.pdf', '[attached_file 1]'],
    ['an image', png, '/up/shot.png', '![image](/up/shot.png)'],
  ])('keeps an @folder/ token as typed when the send carries %s', async (_label, file, path, marker) => {
    uploadReturns([path])
    await act(async () => { mount(GRANTED) })
    const box = screen.getByLabelText('Chat message')
    fireEvent.drop(box, { dataTransfer: { files: [file()], types: ['Files'] } })
    fireEvent.change(box, { target: { value: 'compare with @src/utils/' } })
    const sendBtn = screen.getByLabelText('Send message')
    await waitFor(() => expect(sendBtn).not.toBeDisabled())
    await act(async () => { fireEvent.click(sendBtn) })
    const chat = mockPost.mock.calls.find(c => c[0] === '/api/chat?ws=1')!
    expect(chat[1].message).toContain('compare with @src/utils/')
    expect(chat[1].message).toContain(marker)
    expect(chat[1].message).not.toContain('[attached_dir')
    expect(chat[1].meta.dirs).toBeUndefined()
  })

  it('sends the same @folder/ draft unchanged when no file is staged', async () => {
    await act(async () => { mount(GRANTED) })
    fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: 'compare with @src/utils/' } })
    await act(async () => { fireEvent.click(screen.getByLabelText('Send message')) })
    const chat = mockPost.mock.calls.find(c => c[0] === '/api/chat?ws=1')!
    expect(chat[1].message).toBe('compare with @src/utils/')
    expect(chat[1].meta).toEqual({ sendId: expect.any(String) })
    expect(mockRaw).not.toHaveBeenCalled()
  })

  it('names a file the moment it is picked and holds the picker until it lands', async () => {
    let answer: (r: Response) => void = () => {}
    mockRaw.mockReturnValueOnce(new Promise<Response>(r => { answer = r }))
    await act(async () => { mount(GRANTED) })
    fireEvent.change(screen.getByTestId('chat-embed-file-input'), { target: { files: [pdf()] } })
    const status = await screen.findByTestId('chat-embed-uploading')
    expect(status).toHaveTextContent('Uploading files…')
    expect(status).toHaveTextContent('spec.pdf')
    const picker = screen.getByLabelText('Uploading files…')
    expect(picker).toBeDisabled()
    expect(picker).toHaveAttribute('title', 'Uploading files…')
    // Text alone would make the turn sendable; the running upload still holds it.
    fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: 'see attached' } })
    expect(screen.getByLabelText('Send message')).toBeDisabled()
    await act(async () => { answer(new Response(JSON.stringify({ paths: ['/up/spec.pdf'] }), { status: 200 })) })
    await waitFor(() => expect(screen.queryByTestId('chat-embed-uploading')).toBeNull())
    expect(screen.getByLabelText('Attach files')).not.toBeDisabled()
    expect(screen.getByRole('group', { name: '/up/spec.pdf' })).toBeInTheDocument()
  })

  it('shows the server refusal when an upload fails', async () => {
    mockRaw.mockRejectedValueOnce(Object.assign(new Error('API 413'), { status: 413, body: '{"error":"file too large"}' }))
    await act(async () => { mount(GRANTED) })
    fireEvent.paste(screen.getByLabelText('Chat message'), { clipboardData: { files: [pdf()], types: ['Files'] } })
    expect(await screen.findByTestId('chat-embed-attach-notice')).toHaveTextContent('Upload failed: file too large')
  })
})

describe('shared Attach integration', () => {
  it('uses the shared picker without a store or router and queues a file-only turn', async () => {
    mockGet.mockResolvedValue({ messages: [], running: true, title: '' })
    uploadReturns(['/up/spec.pdf'])
    await act(async () => { mount(GRANTED) })
    const input = screen.getByTestId('chat-embed-file-input')
    expect(input).toHaveAttribute('data-composer-attach')
    expect(screen.getByLabelText('Chat message')).toHaveAttribute('data-composer-editor')
    fireEvent.change(input, { target: { files: [pdf()] } })
    const queue = await screen.findByRole('button', { name: 'Queue message' })
    await waitFor(() => expect(queue).not.toBeDisabled())
    await act(async () => { fireEvent.click(queue) })
    const chat = mockPost.mock.calls.find(c => c[0] === '/api/chat?ws=1')!
    expect(chat[1].meta.files).toEqual(['/up/spec.pdf'])
  })

  it('holds the shared Send button as well as Enter during an upload', async () => {
    let settle!: (response: Response) => void
    mockRaw.mockReturnValueOnce(new Promise<Response>(resolve => { settle = resolve }))
    await act(async () => { mount(GRANTED) })
    fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: 'wait for my file' } })
    fireEvent.change(screen.getByTestId('chat-embed-file-input'), { target: { files: [pdf()] } })
    await waitFor(() => expect(mockRaw).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled()
    await act(async () => settle(new Response(JSON.stringify({ paths: ['/up/spec.pdf'] }))))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send message' })).not.toBeDisabled())
  })

  it('restores files and text when the host onSend rejects, without posting chat itself', async () => {
    uploadReturns(['/up/spec.pdf'])
    const onSend = vi.fn().mockRejectedValue(new Error('no receipt'))
    await act(async () => { mount(GRANTED, { onSend }) })
    fireEvent.change(screen.getByLabelText('Chat message'), { target: { value: 'my draft' } })
    fireEvent.change(screen.getByTestId('chat-embed-file-input'), { target: { files: [pdf()] } })
    await waitFor(() => expect(screen.getByRole('group', { name: '/up/spec.pdf' })).toBeInTheDocument())
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Send message' })))
    await waitFor(() => expect(screen.getByLabelText('Chat message')).toHaveValue('my draft'))
    expect(screen.getByRole('group', { name: '/up/spec.pdf' })).toBeInTheDocument()
    expect(onSend).toHaveBeenCalledWith(expect.stringContaining('[attached_file 1]'), { files: ['/up/spec.pdf'], sendId: expect.any(String) })
    expect(mockPost).not.toHaveBeenCalled()
  })
})
