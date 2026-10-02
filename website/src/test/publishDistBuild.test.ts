// The publish plugin driven by a REAL Vite build, because what decides whether a
// failed build is published is the order Vite and Rolldown run the hooks in,
// which a hand-driven plugin cannot show.
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { mkdirSync, mkdtempSync, readdirSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { build, type Plugin } from 'vite'

import { atomicPublishPlugin } from '../../scripts/publish-dist.mjs'

let root: string
let live: string

beforeEach(() => {
  root = mkdtempSync(path.join(tmpdir(), 'publish-dist-build-'))
  live = path.join(root, 'dist')
  mkdirSync(path.join(root, 'src'))
  // A constant entry document written into this test's own temp dir; `root` is
  // the mkdtemp path above, not external input.
  // nosemgrep: javascript.lang.security.audit.unknown-value-with-script-tag.unknown-value-with-script-tag
  writeFileSync(path.join(root, 'index.html'), '<!doctype html><script type="module" src="/src/main.js"></script>')
  writeFileSync(path.join(root, 'src', 'main.js'), 'console.log(1)\n')
  mkdirSync(live)
  writeFileSync(path.join(live, 'index.html'), 'the previous build')
})

afterEach(() => {
  rmSync(root, { recursive: true, force: true })
})

const quiet = () => {}

/** Build `root` with the publish plugin registered last, as vite.config.ts does. */
function viteBuild(plugins: Plugin[]) {
  return build({
    root,
    configFile: false,
    logLevel: 'silent',
    plugins: [...plugins, atomicPublishPlugin({ warn: quiet }) as Plugin],
    build: { outDir: 'dist', emptyOutDir: true },
  })
}

const scratchLeft = () => readdirSync(root).filter(name => name.startsWith('.dist.'))

describe('a real Vite build', () => {
  it('publishes a build that succeeds, and leaves no scratch', async () => {
    await viteBuild([])

    expect(readdirSync(path.join(live, 'assets')).length).toBeGreaterThan(0)
    expect(scratchLeft()).toEqual([])
  }, 30_000)

  it.each([
    ['a transform', { name: 'boom', transform: (_c: string, id: string) => { if (id.endsWith('main.js')) throw new Error('BOOM') } }],
    ['a writeBundle', { name: 'boom', writeBundle() { throw new Error('BOOM') } }],
    ['an enforce:post writeBundle', { name: 'boom', enforce: 'post', writeBundle() { throw new Error('BOOM') } }],
    ['an order:post writeBundle', { name: 'boom', writeBundle: { order: 'post', handler() { throw new Error('BOOM') } } }],
  ] as [string, Plugin][])('never publishes when %s fails, and reports that failure', async (_label, failing) => {
    await expect(viteBuild([failing])).rejects.toThrow(/BOOM/)

    expect(readdirSync(live)).toEqual(['index.html'])
    expect(scratchLeft()).toEqual([])
  }, 30_000)

  it('strands its scratch when an earlier closeBundle fails, for the next build to sweep', async () => {
    await expect(viteBuild([{ name: 'boom', closeBundle() { throw new Error('BOOM') } }])).rejects.toThrow(/BOOM/)
    expect(readdirSync(live)).toEqual(['index.html'])
    expect(scratchLeft()).toHaveLength(1)
    // That tree carries this (live) process's pid; a later `npm run build` sees
    // the pid dead. pid 2^31-1 cannot be a live process.
    for (const name of scratchLeft()) {
      renameSync(path.join(root, name), path.join(root, name.replace(/-\d+-/, '-2147483647-')))
    }

    await viteBuild([])

    expect(scratchLeft()).toEqual([])
  }, 30_000)
})
