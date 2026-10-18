/**
 * Settings -> Security -> Defense-in-depth architecture (`SecurityPanelLayers.tsx`).
 *
 * The section is a static list rendered from `FEATURES`: one row per feature,
 * each with its catalog label and description and the badge of the layer it
 * belongs to. These pins read the expected copy through `i18nT`, so they hold
 * in any test locale. Each waits for the section heading first: the providers'
 * theme settles after the first render, and the asserts read the settled tree.
 */
import { describe, expect, it } from 'vitest'
import { screen, within } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import { i18nT } from '../../i18n/t'
import { FEATURES, FEATURE_DESCRIPTION_KEY, FEATURE_LABEL_KEY, LayersSection } from './SecurityPanelLayers'

const HEADING = () => i18nT('pages.settings.securityPanel.defense_in_depth_architecture')

describe('SecurityPanel defense-in-depth layers', () => {
  it('renders one row per feature with its label and description', async () => {
    renderWithProviders(<LayersSection />)
    await screen.findByText(HEADING())
    for (const f of FEATURES) {
      expect(screen.getByText(i18nT(FEATURE_LABEL_KEY[f.key]))).toBeInTheDocument()
      expect(screen.getByText(i18nT(FEATURE_DESCRIPTION_KEY[f.key]))).toBeInTheDocument()
    }
  })

  it('badges each row with the layer its feature belongs to', async () => {
    renderWithProviders(<LayersSection />)
    await screen.findByText(HEADING())
    for (const f of FEATURES) {
      const label = screen.getByText(i18nT(FEATURE_LABEL_KEY[f.key]))
      const row = label.parentElement as HTMLElement
      const badge = f.layer === 'auth'
        ? i18nT('pages.settings.securityPanel.layer_auth')
        : i18nT('pages.settings.securityPanel.layer_n', { n: f.layer })
      expect(within(row).getByText(badge)).toBeInTheDocument()
    }
  })

  it('names the section by its rail heading and explains the layering', async () => {
    renderWithProviders(<LayersSection />)
    expect(await screen.findByText(HEADING())).toBeInTheDocument()
    expect(screen.getByText(i18nT('pages.settings.securityPanel.kirocrew_implements_6_security_layers_each_layer'))).toBeInTheDocument()
  })
})
