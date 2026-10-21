import { describe, expect, it, vi } from 'vitest'

vi.mock('../i18n/t', async importOriginal => {
  const orig = await importOriginal<typeof import('../i18n/t')>()
  return {
    ...orig,
    i18nT: (key: string, params?: Record<string, unknown>) =>
      params ? `${key.split('.').pop()} ${JSON.stringify(params)}` : key,
  }
})

import { registryRefreshFailureMessage } from '../components/appstore/registryRefreshMessage'

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
