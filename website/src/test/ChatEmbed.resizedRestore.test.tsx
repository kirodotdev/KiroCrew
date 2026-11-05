/**
 * A failed embed send hands its staged images back with their "resized"
 * badge: on the live path, and through a recovery parked while the embed was
 * unmounted and drained by the next one for the same slot.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react'
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

// The upload answers as if the client had shrunk the image, which is the only
// way a resize reaches the composer.
const resize = { name: 'big.png', fromW: 4000, fromH: 3000, toW: 2000, toH: 1500, fromBytes: 9, toBytes: 3 }
vi.mock('../utils/composerUpload', async importOriginal => ({
  ...(await importOriginal<typeof import('../utils/composerUpload')>()),
  uploadComposerFiles: vi.fn(async () => ({
    paths: ['/up/big.png'], resized: [resize], resizedByPath: { '/up/big.png': resize },
  })),
}))

import ChatEmbed from '../app-sdk/ChatEmbed'
import { AppSdkContext, type AppSdkContextValue } from '../app-sdk/scopedApi'
import { EMBED_UPLOAD_PATH } from '../app-sdk/useEmbedAttachments'
import { resetEmbedRecoveryForTests } from '../app-sdk/embedSendRecovery'

const BADGE = 'Resized to fit model limits: 4000×3000 to 2000×1500'
const ctx = {
  api: { get: mockGet, post: mockPost, raw: mockRaw },
  info: { name: 'host', version: '0', permissions: { api: ['/api/chat', '/api/chat/slots', EMBED_UPLOAD_PATH], events: [] } },
  subscribe: () => () => {},
  navigate: () => {},
  notify: () => {},
} as unknown as AppSdkContextValue
let queryClient: QueryClient

function embed() {
  return (
    <QueryClientProvider client={queryClient}>
      <AppSdkContext.Provider value={ctx}>
        <ChatEmbed slotKey="s1" />
      </AppSdkContext.Provider>
    </QueryClientProvider>
  )
}

/** Mount, stage the resized image, and wait until Send is ready. */
async function stageResizedImage() {
  const view = render(embed())
  await act(async () => {})
  fireEvent.drop(screen.getByLabelText('Chat message'), {
    dataTransfer: { files: [new File(['x'], 'big.png', { type: 'image/png' })], types: ['Files'] },
  })
  expect(await screen.findByRole('button', { name: BADGE })).toBeInTheDocument()
  await waitFor(() => expect(screen.getByLabelText('Send message')).not.toBeDisabled())
  return view
}

beforeEach(() => {
  vi.clearAllMocks()
  resetEmbedRecoveryForTests()
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
  mockPost.mockResolvedValue({})
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

describe('ChatEmbed resized badge on a failed send', () => {
  it('comes back with the image the failed send hands back', async () => {
    await stageResizedImage()
    mockPost.mockRejectedValueOnce(new Error('offline'))
    await act(async () => { fireEvent.click(screen.getByLabelText('Send message')) })

    await waitFor(() => expect(screen.getByRole('group', { name: '/up/big.png' })).toBeInTheDocument())
    expect(screen.getByRole('button', { name: BADGE })).toBeInTheDocument()
  })

  it('comes back through a recovery parked while the embed was unmounted', async () => {
    const view = await stageResizedImage()
    let fail!: (err: Error) => void
    mockPost.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject }))
    await act(async () => { fireEvent.click(screen.getByLabelText('Send message')) })
    await waitFor(() => expect(fail).toBeTypeOf('function'))
    expect(screen.queryByRole('group', { name: '/up/big.png' })).toBeNull()

    view.unmount()
    await act(async () => { fail(new Error('offline')) })
    render(embed())

    await waitFor(() => expect(screen.getByRole('group', { name: '/up/big.png' })).toBeInTheDocument())
    expect(screen.getByRole('button', { name: BADGE })).toBeInTheDocument()
  })
})
