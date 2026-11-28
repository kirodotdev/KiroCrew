/**
 * A split pane, a Bot thread and a Bot session panel all draw their transcript
 * through `useChatPaneRenderers`. A signed-out (`auth_required`) row there must
 * offer the same Sign in button as the single-chat page, opening the sign-in
 * dialog in place, and read as text only where the host withholds it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { useChatPaneRenderers } from '../components/ChatPaneTranscript'
import { resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { api } from '../api/client'
import type { ChatMessage } from '../types'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, kasLoginStatus: vi.fn() } }
})

const signedOut = { role: 'error', cls: '', content: 'Not signed in to Kiro. Sign in from Settings → Kiro account, then send your message again.', meta: { kind: 'auth_required', notice: 'kas_signed_out' } } as ChatMessage

const ctx: MessageRenderContext = {
  index: 0,
  messages: [signedOut],
  running: false,
  key: 'k0',
  hideCardOwnedOAuth: false,
  autoDeniedIds: new Set<string>(),
  wrapper: (children) => children,
  row: (children) => children,
} as MessageRenderContext

function Pane({ noSignIn }: { noSignIn?: boolean }) {
  const { renderers } = useChatPaneRenderers({
    slotKey: 's1',
    busyMode: 'split',
    crewmateTranscript: undefined,
    messages: [signedOut],
    noSignIn,
    resume: { continuable: false, interrupted: false, continuing: false, onContinue: () => {} },
  })
  return <>{resolveRenderer(signedOut, renderers)?.render(signedOut, ctx)}</>
}

describe('a chat pane offers Kiro sign-in on a signed-out row', () => {
  beforeEach(() => {
    vi.mocked(api.kasLoginStatus).mockResolvedValue({ authenticated: false, provider: null, identity: null, transport: 'device' } as never)
  })

  it('draws the Sign in button, which opens the sign-in dialog in place', async () => {
    renderWithProviders(<Pane />)
    expect(screen.getByTestId('error-card')).toHaveAttribute('data-auth-required', 'true')
    fireEvent.click(screen.getByTestId('kiro-sign-in-open'))
    expect(await screen.findByTestId('kiro-sign-in-dialog')).toBeInTheDocument()
  })

  it('keeps the row as text where the host withholds sign-in (embedded or popout)', () => {
    renderWithProviders(<Pane noSignIn />)
    expect(screen.queryByTestId('kiro-sign-in-open')).toBeNull()
    expect(screen.getByTestId('error-card')).toHaveTextContent('Not signed in to Kiro.')
  })
})
