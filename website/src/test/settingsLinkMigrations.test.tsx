/**
 * Settings-redirect migration: prose that names a Settings tab now renders the
 * tab name as a <SettingsLink> (via the react-i18next <Trans> `<0>` idiom)
 * instead of plain text, and prose that names an Agent Capabilities tab
 * interpolates the rail / tab labels instead of re-spelling them.
 *
 * The link hosts (OpsMissionControl SettingsPanel, InstancesViewport) are
 * pinned in their own test files; this file covers the shared idiom through a
 * catalog value rendered standalone, and the label single-sourcing.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Trans } from 'react-i18next'
import type { ReactNode } from 'react'
import { i18nT } from '../i18n/t'
import { SettingsLink } from '../components/SettingsLink'
import { capabilitiesVars } from '../components/destinationVars'

const wrap = (ui: ReactNode) => render(<MemoryRouter>{ui}</MemoryRouter>)

describe('Settings-link migrations', () => {
  it('InstancesViewport hint: catalog value + idiom yield a link to /settings/instances', () => {
    wrap(
      <Trans
        i18nKey="components.instancesViewport.this_tab_stays_until_you_disconnect_the_instance"
        components={[<SettingsLink key="l" tab="instances" />]}
      />,
    )
    const link = screen.getByRole('link', { name: /Settings → Remote Instances/ }) as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe('/settings/instances')
    // The sentence around the link survives the wrapping.
    expect(screen.getByText(/This tab stays until you disconnect the instance in/)).toBeInTheDocument()
  })

  it('Capabilities destinations are spelled by the rail and tab label keys, not by the sentence', () => {
    // A relabel of the rail entry or a tab must change these sentences with
    // it — that is the drift the audit found (a "Settings → MCP" tab that
    // never existed). So the catalog values carry only the placeholders, and
    // the rendered text equals what the Capabilities page itself renders.
    const rail = i18nT('nav.agent_capabilities')
    expect(i18nT('apps.mochi.settingsPanel.mcp_empty', capabilitiesVars('connections'))).toContain(
      `${rail} → ${i18nT('pages.capabilitiesPage.connections_label')}`,
    )
    expect(i18nT('apps.mdNotebook.knowledge.connectHelp', capabilitiesVars('knowledge'))).toContain(
      `${rail} → ${i18nT('pages.capabilitiesPage.knowledge_label')}`,
    )
    expect(i18nT('components.projectSkillsTrust.withdraw_hint', capabilitiesVars('skills'))).toContain(
      `${rail} → ${i18nT('pages.capabilitiesPage.skills_label')}`,
    )
    // And the raw values no longer hardcode the words.
    expect(i18nT('apps.mochi.settingsPanel.mcp_empty', { capabilities: 'X', tab: 'Y' })).toContain('X → Y')
  })
})
