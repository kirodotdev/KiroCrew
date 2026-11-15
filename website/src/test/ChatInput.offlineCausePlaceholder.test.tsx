import { describe, it, expect, vi } from 'vitest'
import { act, screen } from '@testing-library/react'
import { createConnectedTestStore, renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import type { RootState } from '../store'
import { sseDisconnected } from '../store/dashboardSlice'
import en from '../i18n/locales/en.manual.json'

/**
 * The offline placeholder names the same cause as the shell's offline strip
 * (#34355): over the relay, "Gateway offline — message will not send" under a
 * strip that says the session ended reads as two different problems. A plain
 * drop (the direct dashboard, or a relay with no cause) keeps its wording.
 */
const props = { value: '', onChange: vi.fn(), onSend: vi.fn(), connected: false }

describe('ChatInput offline placeholder follows the offline cause', () => {
  it('says the gateway is connecting before the page\'s first socket opens or closes', () => {
    renderWithProviders(<ChatInput {...props} />)
    expect(screen.getByPlaceholderText(en.app.gateway_connecting)).toBeInTheDocument()
  })

  it('keeps the plain offline wording when the gateway dropped with no cause', () => {
    const { store } = renderWithProviders(<ChatInput {...props} />)
    act(() => { store.dispatch(sseDisconnected()) })
    expect(screen.getByPlaceholderText(en.components.chatInput.gateway_offline_message_will_not_send)).toBeInTheDocument()
  })

  it.each([
    ['session_ended', en.app.relay_session_ended],
    ['refused', en.app.relay_access_refused],
  ] as const)('says the relay %s when that is why the gateway is down', (cause, text) => {
    const { store } = renderWithProviders(<ChatInput {...props} />)
    act(() => { store.dispatch(sseDisconnected(cause)) })
    expect(screen.getByPlaceholderText(text)).toBeInTheDocument()
  })

  it('ignores the relay cause while the gateway itself is up', () => {
    // A crew window passes its own tunnel's `connected`; the relay's cause is
    // not about that tunnel, so it must not reword the placeholder.
    // A status frame turns `connected` on without clearing the cause, so a
    // stale cause beside a live gateway is a state the store can be in.
    const store = createConnectedTestStore({ dashboard: { offlineCause: 'session_ended' } as RootState['dashboard'] })
    renderWithProviders(<ChatInput {...props} />, { store })
    expect(screen.getByPlaceholderText(en.components.chatInput.gateway_offline_message_will_not_send)).toBeInTheDocument()
  })
})
