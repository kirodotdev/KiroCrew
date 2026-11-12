/**
 * A `kirocrew://remote-crew/add` link that arrives while Settings → Remote Crew
 * is already open remounts the panel: its add form reads the link's values only
 * when it mounts, and the panel's own tab state starts on "Your crews", where
 * that form is. Anything else the store changes leaves the panel mounted.
 */
import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { act, screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import { prefillCrewAddForm, setCrewAddForm } from '../store/instancesSlice'
import { EMPTY_INSTANCE_FORM } from '../pages/settings/InstanceFormFields'

const mounts = vi.hoisted(() => ({ count: 0 }))
vi.mock('../pages/settings/RemoteCrewPanel', () => ({
  RemoteCrewPanel: function RemoteCrewPanelStub() {
    const [mount] = React.useState(() => ++mounts.count)
    return <div data-testid="remote-crew-panel" data-mount={mount} />
  },
}))

import SettingsPage from '../pages/SettingsPage'

describe('Settings → Remote Crew and add links', () => {
  it('remounts an open Remote Crew panel for each add link, and only for those', async () => {
    const store = createTestStore()
    renderWithProviders(<SettingsPage />, { store, route: '/settings/instances' })
    const panel = await screen.findByTestId('remote-crew-panel')
    const first = Number(panel.dataset.mount)

    act(() => { store.dispatch(setCrewAddForm({ ...EMPTY_INSTANCE_FORM, name: 'typed' })) })
    expect(Number(screen.getByTestId('remote-crew-panel').dataset.mount)).toBe(first)

    act(() => { store.dispatch(prefillCrewAddForm({ host: 'devbox', port: '', name: '' })) })
    expect(Number(screen.getByTestId('remote-crew-panel').dataset.mount)).toBe(first + 1)

    act(() => { store.dispatch(prefillCrewAddForm({ host: 'other', port: '', name: '' })) })
    expect(Number(screen.getByTestId('remote-crew-panel').dataset.mount)).toBe(first + 2)
  })
})
