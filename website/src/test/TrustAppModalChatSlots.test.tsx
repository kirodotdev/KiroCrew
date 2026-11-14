/**
 * The trust gate names `permissions.chatSlots` before the owner says yes.
 *
 * An app that declares it opens and drives chat sessions of its own, listed
 * under it in the sidebar, so the prompt says so in its own box outside the
 * three-row ceiling. Every entry point builds the gate's target through
 * `trustGrants`, which reads only the manifest's literal `true`; the detail page
 * names the declaration in its permissions card.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'

vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length ? `${key} ${Object.values(vars).join(' ')}` : key,
}))

import TrustAppModal, { type TrustAppTarget } from '../components/appstore/TrustAppModal'
import { trustGrants } from '../components/appstore/trustGrants'
import { ChatSlotsBox } from '../pages/AppDetailGrantBoxes'

const K = 'components.appstore.trustAppModal'

const BASE: TrustAppTarget = {
  name: 'crew-app',
  displayName: 'Crew App',
  origin: 'registry',
  trustRepository: 'https://git.example.test/org/crew-app',
  retryInstalls: true,
}

function renderModal(app: TrustAppTarget) {
  render(
    <TrustAppModal app={app} pending={false} failed={false} granted={false} onCancel={() => {}} onConfirm={() => {}} />,
  )
  return within(screen.getByRole('dialog'))
}

describe('TrustAppModal chat slots disclosure', () => {
  it('names the declaration in its own box when the app declares it', () => {
    const dialog = renderModal({ ...BASE, chatSlots: true })
    expect(dialog.getByTestId('trust-chat-slots')).toBeTruthy()
    expect(dialog.getByText(`${K}.chat_slots_heading`)).toBeTruthy()
    expect(dialog.getByText(`${K}.chat_slots_desc`)).toBeTruthy()
    expect(dialog.getByText(`${K}.capability_note`)).toBeTruthy()
  })

  it('shows no such box for an app that does not declare it', () => {
    const dialog = renderModal(BASE)
    expect(dialog.queryByTestId('trust-chat-slots')).toBeNull()
    expect(dialog.queryByText(`${K}.chat_slots_heading`)).toBeNull()
  })
})

describe('trustGrants', () => {
  it('carries every disclosed grant, each flag only as a literal true', () => {
    expect(trustGrants({
      sessionApproval: true, memberThreads: true, workerAutoApprove: true, chatSlots: true,
      reachableByUserSessions: ['read'],
    })).toEqual({
      sessionApproval: true, memberThreads: true, workerAutoApprove: true, chatSlots: true,
      reachableByUserSessions: ['read'],
    })
    const loose = { chatSlots: 'true', workerAutoApprove: 1 } as never
    expect(trustGrants(loose)).toMatchObject({ chatSlots: false, workerAutoApprove: false })
    expect(trustGrants(undefined)).toEqual({
      sessionApproval: false, memberThreads: false, workerAutoApprove: false, chatSlots: false,
      reachableByUserSessions: undefined,
    })
  })
})

describe('ChatSlotsBox', () => {
  it('names the manifest key on the detail page', () => {
    render(<ChatSlotsBox />)
    const box = screen.getByTestId('chat-slots')
    expect(within(box).getByText('pages.appDetailPage.chat_slots_desc')).toBeTruthy()
    expect(within(box).getByText('chatSlots')).toBeTruthy()
  })
})
