/**
 * The Local preview origins hint has to match what the gateway does with the list.
 *
 * The Preview tab frames only the listed origins, and the agent's `browser` tool
 * opens a loopback address only when its `host:port` is listed. So the hint may
 * not call the list preview-only: an entry also widens what Kiro's browser can open.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'

const HINT = ['pages', 'settings', 'browserPanel', 'origins_desc']

function hint(lang: string): string {
  let node: unknown = (CATALOGS as Record<string, { translation: unknown }>)[lang]?.translation
  for (const part of HINT) node = (node as Record<string, unknown> | undefined)?.[part]
  if (typeof node !== 'string' || node.trim() === '') throw new Error(`missing ${HINT.join('.')} in ${lang}`)
  return node
}

describe('Local preview origins copy matches gateway behavior', () => {
  it('says the agent’s browser can open the listed addresses too', () => {
    const copy = hint('en')
    expect(copy).toMatch(/Kiro’s browser can open these addresses/)
    expect(copy).not.toMatch(/\bonly\b/)
  })
})
