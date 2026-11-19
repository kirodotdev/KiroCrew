/**
 * While the organization form's chunk loads, the stand-in takes focus once.
 *
 * Clicking Work account unmounts that button, so the slot takes focus instead of
 * the page. A later re-render of the chooser (a status poll, the host page) must
 * not move focus again: a person who has moved on to another field keeps it.
 */
import '../i18n/all'
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { useState } from 'react'

import KasLoginGate from '../components/KasLoginGate'
import { renderWithProviders } from './helpers'

// The form's chunk never arrives in this test, so the stand-in stays mounted.
vi.mock('../components/KasOrganizationForm', () => new Promise(() => {}))

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasLoginStatus: vi.fn().mockResolvedValue({
        authenticated: false,
        provider: null,
        identity: null,
        transport: 'loopback',
      }),
      sessionsUsageRefresh: vi.fn().mockRejectedValue(new Error('no refresh in this test')),
    },
  }
})

const WORK_ACCOUNT = 'Work account — sign in with my work email'

// A host page that re-renders the chooser on demand and has its own field.
function Host() {
  const [renders, setRenders] = useState(0)
  return (
    <>
      <input aria-label="host field" />
      <button type="button" onClick={() => setRenders(n => n + 1)}>re-render {renders}</button>
      <KasLoginGate />
    </>
  )
}

describe('KasLoginGate while the organization form chunk loads', () => {
  it('focuses the slot once, and a re-render leaves focus where the person moved it', async () => {
    renderWithProviders(<Host />)
    fireEvent.click(await screen.findByRole('button', { name: WORK_ACCOUNT }))

    const standIn = await screen.findByRole('button', { name: WORK_ACCOUNT })
    expect(standIn, 'the stand-in holds the row').toBeDisabled()
    const slot = document.activeElement
    expect(slot, 'focus is not left on the page').not.toBe(document.body)
    expect(slot?.contains(standIn), 'focus is on the slot holding the stand-in').toBe(true)

    const field = screen.getByRole('textbox', { name: 'host field' })
    field.focus()
    fireEvent.click(screen.getByRole('button', { name: /^re-render/ }))
    expect(await screen.findByRole('button', { name: 're-render 1' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 're-render 1' }))
    expect(await screen.findByRole('button', { name: 're-render 2' })).toBeInTheDocument()
    expect(document.activeElement, 'a re-render does not take focus back').toBe(field)
  })
})
