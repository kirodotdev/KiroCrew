import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../i18n/t', async importOriginal => {
  const orig = await importOriginal<typeof import('../i18n/t')>()
  return {
    ...orig,
    i18nT: (key: string, params?: Record<string, unknown>) =>
      params ? `${key.split('.').pop()} ${JSON.stringify(params)}` : key,
  }
})

import { i18next } from '../i18n/all'
import {
  registryListingFailureMessage, registryRefreshFailureMessage,
} from '../components/appstore/registryRefreshMessage'

describe('registryRefreshFailureMessage', () => {
  it('is empty when every registry refreshed', () => {
    expect(registryRefreshFailureMessage({ ok: true, failed: [], results: [] })).toBe('')
  })

  it('keeps the plain line when no registry refused sign-in', () => {
    expect(registryRefreshFailureMessage({
      ok: false, failed: ['a'], results: [{ name: 'a', ok: false }],
    })).toBe('could_not_refresh_still_showing_last_synced {"names":"a"}')
  })

  it('leads with one sign-in sentence per host, then the plain line', () => {
    expect(registryRefreshFailureMessage({
      ok: false,
      failed: ['a', 'b', 'c'],
      results: [
        { name: 'a', ok: false, reason: 'auth', host: 'one.example.com' },
        { name: 'b', ok: false, reason: 'auth', host: 'one.example.com' },
        { name: 'c', ok: false, reason: 'auth', host: 'two.example.com' },
      ],
    })).toBe([
      'refresh_sign_in_refused {"host":"one.example.com","names":"a, b"}',
      'refresh_sign_in_refused {"host":"two.example.com","names":"c"}',
      'could_not_refresh_still_showing_last_synced {"names":"a, b, c"}',
    ].join(' '))
  })

  it('falls back to the host-free sentence when the host is unknown', () => {
    expect(registryRefreshFailureMessage({
      ok: false, failed: ['a'], results: [{ name: 'a', ok: false, reason: 'auth' }],
    })).toBe([
      'refresh_sign_in_refused_no_host {"names":"a"}',
      'could_not_refresh_still_showing_last_synced {"names":"a"}',
    ].join(' '))
  })
})

describe('registryRefreshFailureMessage names the remedy for every classified cause', () => {
  it('leads with the VPN remedy for a host that did not resolve, and known_hosts for a host key', () => {
    expect(registryRefreshFailureMessage({
      ok: false,
      failed: ['a', 'b', 'c'],
      results: [
        { name: 'a', ok: false, reason: 'dns', host: 'git.example.com' },
        { name: 'b', ok: false, reason: 'host_key', host: 'git.example.com' },
        { name: 'c', ok: false, reason: 'timeout', host: 'other.example.com' },
      ],
    })).toBe([
      'registry_host_not_found {"host":"git.example.com","names":"a"}',
      'registry_host_key_untrusted {"host":"git.example.com","names":"b"}',
      'registry_unreachable {"names":"c"}',
      'could_not_refresh_still_showing_last_synced {"names":"a, b, c"}',
    ].join(' '))
  })

  it('folds a host-less name lookup into the unreachable sentence and drops a host-less host key', () => {
    expect(registryRefreshFailureMessage({
      ok: false,
      failed: ['a', 'b', 'c'],
      results: [
        { name: 'a', ok: false, reason: 'dns' },
        { name: 'b', ok: false, reason: 'network', host: 'x.example.com' },
        { name: 'c', ok: false, reason: 'host_key' },
      ],
    })).toBe([
      'registry_unreachable {"names":"a, b"}',
      'could_not_refresh_still_showing_last_synced {"names":"a, b, c"}',
    ].join(' '))
  })
})

describe('registryRefreshFailureMessage tells missing apps from last-synced ones', () => {
  it('names a failed registry with no cached index as missing, not as last synced', () => {
    expect(registryRefreshFailureMessage({
      ok: false,
      failed: ['community', 'team'],
      results: [
        { name: 'community', ok: false, reason: 'dns', host: 'git.example.com', cached: false },
        { name: 'team', ok: false, cached: true },
      ],
    })).toBe([
      'registry_host_not_found {"host":"git.example.com","names":"community"}',
      'registry_apps_missing {"names":"community"}',
      'could_not_refresh_still_showing_last_synced {"names":"team"}',
    ].join(' '))
  })
})

describe('registryListingFailureMessage', () => {
  it('is empty when no registry failed', () => {
    expect(registryListingFailureMessage([])).toBe('')
  })

  it('names the remedy, then the missing and the last-synced registries apart', () => {
    expect(registryListingFailureMessage([
      { name: 'community', cause: 'dns', host: 'git.example.com', cached: false },
      { name: 'team', cause: 'auth', host: 'git.example.com', cached: true },
      { name: 'odd', cause: 'other', cached: false },
    ])).toBe([
      'registry_host_not_found {"host":"git.example.com","names":"community"}',
      'refresh_sign_in_refused {"host":"git.example.com","names":"team"}',
      'registry_apps_missing {"names":"community, odd"}',
      'could_not_refresh_still_showing_last_synced {"names":"team"}',
    ].join('\n'))
  })
})

describe('a private GitHub registry the GitHub CLI could not read', () => {
  it('names the repository and the fix for each gh cause, then the plain line', () => {
    expect(registryRefreshFailureMessage({
      ok: false,
      failed: ['a', 'b', 'c'],
      results: [
        { name: 'a', ok: false, reason: 'gh_missing', host: 'github.com', repo: 'acme/apps' },
        { name: 'b', ok: false, reason: 'gh_signed_out', host: 'github.com', repo: 'acme/apps' },
        { name: 'c', ok: false, reason: 'gh_no_access', host: 'github.com', repo: 'acme/other' },
      ],
    })).toBe([
      'registry_gh_missing {"repo":"acme/apps","names":"a"}',
      'registry_gh_signed_out {"repo":"acme/apps","names":"b"}',
      'registry_gh_no_access {"repo":"acme/other","names":"c"}',
      'could_not_refresh_still_showing_last_synced {"names":"a, b, c"}',
    ].join(' '))
  })

  it('shows the plain line only when the repository is unknown', () => {
    expect(registryListingFailureMessage([
      { name: 'team', cause: 'gh_signed_out', host: 'github.com', cached: false },
    ])).toBe('registry_apps_missing {"names":"team"}')
  })

  it('names the strict-mode remedy only when the repository is known', () => {
    expect(registryListingFailureMessage([
      { name: 'team', cause: 'gh_strict_mode', repo: 'acme/apps', cached: false },
    ])).toBe([
      'registry_gh_strict_mode {"repo":"acme/apps","names":"team"}',
      'registry_apps_missing {"names":"team"}',
    ].join('\n'))
    expect(registryListingFailureMessage([
      { name: 'team', cause: 'gh_strict_mode', cached: false },
    ])).toBe('registry_apps_missing {"names":"team"}')
    const text = i18next.t('components.registryManager.registry_gh_strict_mode', { repo: 'acme/apps', names: 'team' })
    expect(text).toContain('acme/apps')
    expect(text).toContain('strict provider-binary mode')
    expect(text).toContain('computer that runs Kiro Crew')
    expect(text.toLowerCase()).not.toContain('token')
  })

  it('reads in English as a GitHub CLI sign-in on the computer that runs Kiro Crew', () => {
    const text = i18next.t('components.registryManager.registry_gh_signed_out', { repo: 'acme/apps', names: 'team' })
    expect(text).toContain('acme/apps')
    expect(text).toContain('GitHub CLI')
    expect(text).toContain('computer that runs Kiro Crew')
    expect(text.toLowerCase()).not.toContain('token')
  })
})

describe('registryRefreshFailureMessage in Japanese', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('joins registry names with the Japanese list mark, not an English comma', async () => {
    await i18next.changeLanguage('ja')
    expect(registryRefreshFailureMessage({
      ok: false, failed: ['a', 'b', 'c'], results: [],
    })).toBe('could_not_refresh_still_showing_last_synced {"names":"a、b、c"}')
  })
})
