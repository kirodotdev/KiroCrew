/**
 * Settings → Skills must lead to the pending auto-skill queue.
 *
 * The queue — Approve / Dismiss / Dismiss-all and the `?review=<slug>` deep
 * link — lives on Capabilities → Skills. Settings → Skills is where the two
 * policy toggles that FILL that queue live, so it is where a user lands after
 * turning auto-generation on, and it used to only mention the queue in prose.
 * Two ways out are pinned here: a link a user can click, and a forwarded
 * `?review=<slug>`, so the URL #12543 reports typing on this page reaches the
 * candidate instead of doing nothing.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() =>
    Promise.resolve({ skills: { auto_create_from_sessions: true, approval_required: true } }),
  ),
}))

vi.mock('../api/client', () => ({
  api: { kirocrewConfig: kirocrewConfigMock, patchConfig: patchConfigMock },
}))

import { SkillsPanel } from '../pages/settings/SkillsPanel'

/** Surfaces the live URL so a forwarded deep link can be read back. */
function UrlProbe() {
  const loc = useLocation()
  return <div data-testid="url">{loc.pathname + loc.search}</div>
}

function wrap(route: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[route]}>
        <SkillsPanel />
        <UrlProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const url = () => screen.getByTestId('url').textContent

describe('Settings → Skills reaches the pending queue', () => {
  beforeEach(() => {
    patchConfigMock.mockClear()
    kirocrewConfigMock.mockClear()
  })

  it('links to the Capabilities skills tab that hosts the queue', async () => {
    wrap('/settings/skills')
    const link = await screen.findByRole('link', { name: /pending/i })
    expect(link).toHaveAttribute('href', '/capabilities?tab=skills')
  })

  it('forwards a ?review=<slug> deep link to that tab, carrying the slug', async () => {
    // Landing on a page with no queue is the failure this forward exists to
    // prevent, so the slug must survive the hop — arriving without it means
    // "go find it", which is the whole friction the link removes.
    wrap('/settings/skills?review=beta-skill')
    await waitFor(() => expect(url()).toBe('/capabilities?tab=skills&review=beta-skill'))
  })

  it('percent-encodes a slug so it cannot open a second query parameter', async () => {
    // `useSearchParams` hands back a DECODED value, so re-encoding is the only
    // thing keeping a slug with a reserved character from rewriting the URL.
    wrap('/settings/skills?review=a%26tab%3Dcrews')
    await waitFor(() => expect(url()).toBe('/capabilities?tab=skills&review=a%26tab%3Dcrews'))
  })
})
