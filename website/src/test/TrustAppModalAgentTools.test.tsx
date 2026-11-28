/**
 * A manifest's `agentTools` before and after the owner says yes.
 *
 * The trust gate lists each tool the registry row disclosed, by name and
 * description, and the install it confirms sends back exactly those entries:
 * the gateway approves only them. On the app's page, a tool an install or
 * update holds back is offered in the approval notice by name, never by its
 * digest, and approving it sends the entry back. A tool the owner turned off is
 * only an off switch in its own card (`AppDetailAgentTools.test.tsx`).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'

vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length ? `${key} ${Object.values(vars).join(' ')}` : key,
}))

import TrustAppModal, { type TrustAppTarget } from '../components/appstore/TrustAppModal'
import { agentToolEntries, trustGrants } from '../components/appstore/trustGrants'
import { api } from '../api/client'
import { stagedGrantsOf } from '../pages/appDetailPageStagedGrants'
import AppDetailAgentTools from '../pages/AppDetailAgentTools'
import { AgentToolsStaged } from '../pages/AppDetailGrantBoxes'
import type { AppInfo } from '../pages/appDetailPageTypes'

const K = 'components.appstore.trustAppModal'

const BASE: TrustAppTarget = {
  name: 'widgets',
  displayName: 'Widgets',
  origin: 'registry',
  trustRepository: 'https://git.example.test/org/widgets',
  retryInstalls: true,
}

const BLOCK = {
  route: 'agent-tools',
  tools: [
    { name: 'widgets_note', description: 'Post one note to a widget.', entry: 'widgets_note@aaaaaaaaaaaa' },
    { name: 'widgets_list', description: 'List the widgets.', entry: 'widgets_list@bbbbbbbbbbbb' },
  ],
}

function renderModal(app: TrustAppTarget) {
  render(
    <TrustAppModal app={app} pending={false} failed={false} granted={false} onCancel={() => {}} onConfirm={() => {}} />,
  )
  return within(screen.getByRole('dialog'))
}

describe('agent tools at the trust gate', () => {
  it('lists every declared tool with its description and the note on what installing approves', () => {
    const dialog = renderModal({ ...BASE, ...trustGrants({}, BLOCK) })
    const box = within(dialog.getByTestId('trust-agent-tools'))
    expect(box.getByText(`${K}.agent_tools_heading`)).toBeTruthy()
    expect(box.getByText('widgets_note')).toBeTruthy()
    expect(box.getByText('Post one note to a widget.', { exact: false })).toBeTruthy()
    expect(box.getByText('widgets_list')).toBeTruthy()
    expect(box.getByText(`${K}.agent_tools_note`)).toBeTruthy()
  })

  it('sends back exactly the entries the card listed', () => {
    expect(agentToolEntries(BLOCK)).toEqual(['widgets_note@aaaaaaaaaaaa', 'widgets_list@bbbbbbbbbbbb'])
  })

  it('shows no box for an app that declares none, a block that is not one, or tools with no entry', () => {
    // An installed app's own app.json carries no entries: no card can approve from it.
    const noEntry = { tools: [{ name: 'widgets_note', description: 'Post one note to a widget.' }] }
    expect(agentToolEntries(noEntry)).toEqual([])
    for (const block of [undefined, {}, { tools: 'x' }, { tools: [{ description: 'no name', entry: 'x@1' }] }, noEntry]) {
      const { unmount } = render(
        <TrustAppModal app={{ ...BASE, ...trustGrants({}, block) }} pending={false} failed={false} granted={false} onCancel={() => {}} onConfirm={() => {}} />,
      )
      expect(screen.queryByTestId('trust-agent-tools')).toBeNull()
      unmount()
    }
  })
})

describe('the install request', () => {
  it('carries the listed entries, and nothing when no card listed any', async () => {
    const calls: unknown[] = []
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      calls.push(JSON.parse(String(init?.body)))
      return new Response(JSON.stringify({ error: 'stop', code: 'stop' }), { status: 400, headers: { 'Content-Type': 'application/json' } })
    })
    vi.stubGlobal('fetch', fetchMock)
    try {
      await api.installFromRegistryStream('widgets', () => {}, undefined, ['widgets_note@aaaaaaaaaaaa']).catch(() => {})
      await api.installFromRegistryStream('widgets', () => {}).catch(() => {})
    } finally {
      vi.unstubAllGlobals()
    }
    expect(calls).toEqual([
      { name: 'widgets', agentToolsConsent: ['widgets_note@aaaaaaaaaaaa'] },
      { name: 'widgets' },
    ])
  })
})

describe('agent tools on the app page', () => {
  const grant = {
    approved: [{ name: 'widgets_list', description: 'List the widgets.', entry: 'widgets_list@111111111111' }],
    staged: [{ name: 'widgets_note', description: 'Post one note to a widget.', entry: 'widgets_note@222222222222' }],
    revoked: [{ name: 'widgets_tag', description: 'Tag a widget.', entry: 'widgets_tag@333333333333' }],
  }

  it('offers only the held-back tools for approval, not the revoked ones', () => {
    const app = { name: 'widgets', agentToolsGrant: grant } as unknown as AppInfo
    const staged = stagedGrantsOf(app)
    expect(staged.stagedAgentTools).toEqual(grant.staged)
    expect(staged.hasStagedGrants).toBe(true)
  })

  it('holds nothing back when the only tool off is one the owner revoked', () => {
    const app = { name: 'widgets', agentToolsGrant: { ...grant, staged: [] } } as unknown as AppInfo
    expect(stagedGrantsOf(app).hasStagedGrants).toBe(false)
  })

  it('shows a held-back tool by name and description, never by its entry', () => {
    render(<AgentToolsStaged staged={grant.staged} />)
    const list = within(screen.getByTestId('staged-agent-tools'))
    expect(list.getByText('widgets_note')).toBeTruthy()
    expect(list.getByText('Post one note to a widget.', { exact: false })).toBeTruthy()
    expect(list.queryByText(/222222222222/)).toBeNull()
  })

  it('shows a revoked tool only as an off switch, never by its entry', () => {
    render(<AppDetailAgentTools appName="widgets" enabled grant={grant} />)
    const card = within(screen.getByTestId('app-agent-tools'))
    expect(card.getByRole('switch', { name: 'widgets_tag' }).getAttribute('aria-checked')).toBe('false')
    expect(card.queryByText('widgets_note')).toBeNull()
    expect(card.queryByText(/333333333333/)).toBeNull()
  })
})
