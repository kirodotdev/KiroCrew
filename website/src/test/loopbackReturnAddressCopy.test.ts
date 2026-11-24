/**
 * The OAuth paste box's copy across every shipped catalog.
 *
 * A sign-in's browser comes back to the loopback host the sign-in registered:
 * KAS returns to `http://localhost:PORT/…`, and Crew's own sign-ins return to
 * `http://127.0.0.1:PORT/…` (or `localhost`, for a vendor that accepts only that
 * spelling). The Connections card and the chat banner share these two strings,
 * so the placeholder shows the localhost form and the invalid-address hint names
 * both. A form missing from the copy reads as one the box refuses, so people
 * hand-edit an address that already worked. The paste box's own check accepts
 * both forms (`ConnectionsPage.test.tsx` pins that).
 *
 * Imports the full catalog map, so this file pays that load once (see
 * website/docs/testing.md, "What a setupFiles entry costs").
 */
import { describe, it, expect } from 'vitest'
import { CATALOGS } from '../i18n/catalogs'
import { SUPPORTED_LANGUAGES } from '../i18n/languages'
import { isValidLoopbackReturnAddress } from '../utils/loopbackReturnAddress'

const PLACEHOLDER = 'pages.connectionsPage.return_address_placeholder'
const INVALID_HINT = 'pages.connectionsPage.invalid_return_address'
const RETURN_HOSTS = ['localhost', '127.0.0.1']

function leaf(catalog: Record<string, unknown>, dotted: string): string {
  const value = dotted.split('.').reduce<unknown>(
    (node, part) => (node !== null && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined),
    catalog,
  )
  expect(typeof value, `${dotted} must be a string`).toBe('string')
  return value as string
}

describe('paste box copy', () => {
  it.each(SUPPORTED_LANGUAGES.map(l => l.code))('%s names every host a sign-in returns to', code => {
    const catalog = CATALOGS[code].translation
    expect(leaf(catalog, PLACEHOLDER)).toContain('http://localhost:')
    const hint = leaf(catalog, INVALID_HINT)
    for (const host of RETURN_HOSTS) expect(hint).toContain(`http://${host}`)
  })

  it('shows a placeholder that, filled in, is an address the box accepts', () => {
    const placeholder = leaf(CATALOGS.en.translation, PLACEHOLDER)
    const filledIn = placeholder.replace('PORT', '43123').replaceAll('…', 'one-time')
    expect(isValidLoopbackReturnAddress(filledIn)).toBe(true)
  })
})
