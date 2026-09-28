import { describe, it, expect } from 'vitest'
import { buildProviderList } from '../components/PublishHub'
import type { AppPublishProvider } from '../api/client'
import type { PublishProviderDescriptor } from '../types'

/**
 * The Publish panel reads TWO registries. The app endpoint deliberately omits built-in
 * destinations ("registered frontend-side and are not returned here"), so a destination
 * registered by the edition -- which is how a stock build gets any publish destination at
 * all -- appears nowhere unless the panel merges the core registry in as well.
 */

const app = (over: Partial<AppPublishProvider> = {}): AppPublishProvider =>
  ({
    id: 'deploy-web-aws',
    label: 'Publish to public web (your AWS)',
    icon: 'Globe',
    kinds: [],
    configured: true,
    setupRoute: '/deploy',
    endpoint: '/api/deploy/deploy',
    ...over,
  }) as AppPublishProvider

const core = (over: Partial<PublishProviderDescriptor> = {}): PublishProviderDescriptor => ({
  name: 'default',
  display_name: 'Public web (shared drive)',
  capabilities: ['sharing'],
  kind_support: 'native',
  capable: true,
  available: true,
  sharing_model: {
    supports_private: true,
    supports_shared: false,
    supports_public: true,
    principal_kind: 'none',
    supports_roles: false,
    supports_expiration: false,
    programmable: false,
  },
  sync_model: { authority: 'local', concurrency: 'token', collab_mode: 'mirror' },
  discovery_model: {
    list_mine: false,
    list_shared_with_me: false,
    list_public: false,
    full_text_search: false,
    pull_by_id: false,
  },
  ...over,
})

describe('buildProviderList — core registry rows', () => {
  it('lists a core destination alongside the app ones', () => {
    const rows = buildProviderList([app()], 'markdown', [core()])
    expect(rows.map(r => r.label)).toEqual([
      'Publish to public web (your AWS)',
      'Public web (shared drive)',
    ])
    // The core row must be tagged as such: its presence is what routes the publish at
    // the artifact endpoint instead of the app row's declared deploy endpoint.
    expect(rows[1].core).toBeTruthy()
    expect(rows[1].app).toBeUndefined()
  })

  it('renders a core destination on an edition with no apps at all', () => {
    const rows = buildProviderList([], 'markdown', [core()])
    expect(rows.map(r => r.label)).toEqual(['Public web (shared drive)'])
  })

  it('keeps the app row when an app claims a core destination id', () => {
    // Pre-existing resolution for this clash is app-first (test_publish_providers asserts
    // the APP's endpoint wins), so the merge must not quietly reverse it.
    const rows = buildProviderList([app({ id: 'default', label: 'App shadow' })], 'markdown', [core()])
    expect(rows).toHaveLength(1)
    expect(rows[0].label).toBe('App shadow')
    expect(rows[0].core).toBeUndefined()
  })

  it('omits a core destination that cannot host this kind', () => {
    const rows = buildProviderList([], 'markdown', [core({ capable: false, kind_support: 'unsupported' })])
    expect(rows).toEqual([])
  })

  it('lists an unavailable core destination and carries its remedy', () => {
    // Hiding it would make the destination undiscoverable until the user set it up by
    // hand -- they would have no way to learn the setup was needed.
    const rows = buildProviderList([], 'markdown', [
      core({ available: false, install_hint: 'Register a profile first.' }),
    ])
    expect(rows).toHaveLength(1)
    expect(rows[0].configured).toBe(false)
    expect(rows[0].installHint).toBe('Register a profile first.')
    // No setup route: a core destination explains itself in place, because the deploy
    // setup page describes a different destination's flow.
    expect(rows[0].setupRoute).toBe('')
  })

  it('treats a missing available flag as available', () => {
    // Documented as omitted by older gateways; reading absence as "needs setup" would
    // mark every destination on such a gateway unusable.
    const { available: _drop, ...withoutFlag } = core()
    const rows = buildProviderList([], 'markdown', [withoutFlag as PublishProviderDescriptor])
    expect(rows[0].configured).toBe(true)
  })
})
