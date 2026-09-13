import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

const SRC = resolve(__dirname, '..')

const STAMPING_HELPERS = ['utils/storageGc.ts', 'utils/safeStorage.ts']

const walk = (dir: string, out: string[] = []): string[] => {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) {
      if (name === 'node_modules') continue
      walk(full, out)
    } else if (/\.(ts|tsx)$/.test(name) && !/\.test\.[tj]sx?$/.test(name)) {
      out.push(full)
    }
  }
  return out
}

const SESSION_PREFIXES = (() => {
  const gc = readFileSync(join(SRC, 'utils/storageGc.ts'), 'utf-8')
  const block = gc.slice(gc.indexOf('SESSION_PREFIXES'))
  return [...block.slice(0, block.indexOf(']')).matchAll(/'([^']+)'/g)].map(m => m[1])
})()

describe('a session-scoped write cannot skip the owner ledger', () => {
  it('finds the prefixes it is meant to police', () => {
    expect(SESSION_PREFIXES.length).toBeGreaterThan(2)
    expect(SESSION_PREFIXES.some(p => p.startsWith('vc_') || p.startsWith('mc-'))).toBe(true)
  })

  it('routes every session-scoped setItem through a stamping helper', () => {
    const offenders: string[] = []
    for (const file of walk(SRC)) {
      const rel = file.slice(SRC.length + 1).replace(/\\/g, '/')
      if (STAMPING_HELPERS.includes(rel)) continue
      const lines = readFileSync(file, 'utf-8').split('\n')
      lines.forEach((line, i) => {
        if (!/\.setItem\s*\(/.test(line)) return
        if (!SESSION_PREFIXES.some(p => line.includes(p))) return
        offenders.push(`${rel}:${i + 1}`)
      })
    }

    expect(offenders, 'these write a session-scoped key without stamping its owner').toEqual([])
  })
})
