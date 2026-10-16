/**
 * Production renderer code must never reintroduce a browser-native dialog.
 *
 * The ESLint rule blocks bare and window-qualified calls. This test protects the
 * explicit window-qualified spellings directly, which keeps the contract visible
 * if a future config change narrows the lint scope.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const DIRECT_NATIVE_DIALOG = /window\.(?:confirm|alert|prompt)\s*\(/

function productionSources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const path = resolve(dir, entry.name)
    if (entry.isDirectory()) return productionSources(path)
    if (!/\.(?:ts|tsx)$/.test(entry.name) || /\.test\.(?:ts|tsx)$/.test(entry.name)) return []
    return [path]
  })
}

describe('native dialog ratchet', () => {
  it('has no window-qualified browser dialog calls in production renderer code', () => {
    const offenders = productionSources(SRC).filter(path => DIRECT_NATIVE_DIALOG.test(readFileSync(path, 'utf-8')))
    expect(offenders).toEqual([])
  })
})
