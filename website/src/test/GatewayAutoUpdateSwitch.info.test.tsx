// "Update the gateway automatically" keeps its label and switch on the row; the
// sentence that explains it is in the info button beside the label, reachable
// from the keyboard, and not in the row's body copy.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import { i18nT } from '../i18n/t'
import { GatewayAutoUpdateSwitch } from '../pages/settings/AboutPanelUpdateSwitches'

afterEach(() => vi.restoreAllMocks())

describe('GatewayAutoUpdateSwitch', () => {
  it('shows the label and switch, with the description in the info button', async () => {
    renderWithProviders(<GatewayAutoUpdateSwitch />, { store: createTestStore() })
    const description = i18nT('pages.settings.aboutPanel.gateway_auto_update_description')
    expect(screen.getByText(i18nT('pages.settings.aboutPanel.automatic_updates'))).toBeInTheDocument()
    expect(screen.getByRole('switch')).toBeInTheDocument()
    expect(screen.queryByText(description)).not.toBeInTheDocument()
    const info = screen.getByRole('button', { name: i18nT('components.infoTip.more_information') })
    fireEvent.focus(info)
    expect(screen.getByRole('tooltip')).toHaveTextContent(description)
    expect(info).toHaveAccessibleDescription(description)
  })
})
