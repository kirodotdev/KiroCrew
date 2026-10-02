import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import {
  atomicPublishPlugin,
  incompleteReason,
  isMountPoint,
  publishDist,
  renameWithRetry,
  IN_GAP_BUDGET_MS,
} from '../../scripts/publish-dist.mjs'

/** The completeness cases test_frontend_dist_resolve.py checks the Python gate against. */
const COMPLETENESS_CASES = (
  JSON.parse(
    readFileSync(path.resolve(__dirname, '../../../test/fixtures/dist_completeness_cases.json'), 'utf-8'),
  ) as { cases: { ref: string; reason: string }[] }
).cases

/** A complete build: an index.html and the one chunk it references. */
function makeBuild(dir: string, chunk: string): void {
  mkdirSync(path.join(dir, 'assets'), { recursive: true })
  writeFileSync(
    path.join(dir, 'index.html'),
    `<script type="module" src="/manifest.js"></script><script type="module" src="/assets/${chunk}"></script>`,
  )
  writeFileSync(path.join(dir, 'assets', chunk), 'console.log(1)')
}

function errorWithCode(code: string): Error {
  return Object.assign(new Error(code), { code })
}

const quiet = () => {}

let root: string
let live: string
let next: string

beforeEach(() => {
  root = mkdtempSync(path.join(tmpdir(), 'publish-dist-'))
  live = path.join(root, 'dist')
  next = path.join(root, '.dist.next-1-test')
})

afterEach(() => {
  rmSync(root, { recursive: true, force: true })
})

const leftovers = () => readdirSync(root).filter(name => name !== 'dist')

describe('publishDist', () => {
  it('swaps the new build in and removes the previous one', () => {
    makeBuild(live, 'old-1.js')
    makeBuild(next, 'new-2.js')

    publishDist({ next, live, warn: quiet })

    expect(existsSync(path.join(live, 'assets', 'new-2.js'))).toBe(true)
    expect(existsSync(path.join(live, 'assets', 'old-1.js'))).toBe(false)
    expect(leftovers()).toEqual([])
  })

  it('never shows a reader an empty or half-written dist', () => {
    // Every rename is a point at which a reader (a gateway pinned to dist, the
    // stale-asset watchdog) can look, including between the two renames. dist
    // must then be a complete build, or absent for that one in-gap rename --
    // never present but incomplete, which is what an in-place build shows.
    makeBuild(live, 'old-1.js')
    makeBuild(next, 'new-2.js')
    const seen: string[] = []
    const observe = () => seen.push(existsSync(live) ? incompleteReason(live) || 'complete' : 'absent')

    publishDist({
      next,
      live,
      warn: quiet,
      rename: (from: string, to: string) => {
        observe()
        renameSync(from, to)
      },
    })
    observe()

    // probe rename of the new tree, move-aside, in-gap rename, after.
    expect(seen).toEqual(['complete', 'complete', 'absent', 'complete'])
  })

  it('creates dist on a first build', () => {
    makeBuild(next, 'new-2.js')

    publishDist({ next, live, warn: quiet })

    expect(incompleteReason(live)).toBe('')
  })

  it('refuses an incomplete build and leaves dist as it was', () => {
    makeBuild(live, 'old-1.js')
    makeBuild(next, 'new-2.js')
    rmSync(path.join(next, 'assets', 'new-2.js'))

    expect(() => publishDist({ next, live, warn: quiet })).toThrow(/not a complete build/)

    expect(existsSync(path.join(live, 'assets', 'old-1.js'))).toBe(true)
    expect(existsSync(next)).toBe(true)
  })

  it('rewrites a live tree that cannot be renamed, copying before it removes', () => {
    makeBuild(live, 'old-1.js')
    makeBuild(next, 'new-2.js')
    const warnings: string[] = []
    const rename = (from: string, to: string) => {
      if (from === live) throw errorWithCode('EBUSY') // a bind mount the st_dev test missed
      renameSync(from, to)
    }

    publishDist({ next, live, rename, isWindows: false, warn: (m: string) => warnings.push(m) })

    expect(incompleteReason(live)).toBe('')
    expect(existsSync(path.join(live, 'assets', 'new-2.js'))).toBe(true)
    expect(existsSync(path.join(live, 'assets', 'old-1.js'))).toBe(false)
    expect(leftovers()).toEqual([])
    expect(warnings.join('\n')).toMatch(/rewritten in place/)
  })

  it('rolls back inside the in-gap budget and says what is where', () => {
    makeBuild(live, 'old-1.js')
    makeBuild(next, 'new-2.js')
    let clock = 0
    let inGapAttempts = 0
    const rename = (from: string, to: string) => {
      if (to === live && from.includes('.ready-')) {
        inGapAttempts += 1
        throw errorWithCode('EPERM')
      }
      renameSync(from, to)
    }

    expect(() =>
      publishDist({
        next,
        live,
        warn: quiet,
        rename,
        isWindows: true,
        sleep: (ms: number) => {
          clock += ms
        },
        now: () => clock,
      }),
    ).toThrow(/dist was left as it was; the new build is at .*\.dist\.ready-/)

    // The gap was held to the in-gap budget, not the outside-gap one.
    expect(clock).toBeLessThanOrEqual(IN_GAP_BUDGET_MS)
    expect(inGapAttempts).toBeGreaterThan(1)
    expect(readFileSync(path.join(live, 'assets', 'old-1.js'), 'utf-8')).toBe('console.log(1)')
  })

  it('names no previous build on a failed first publish, and keeps the new one', () => {
    makeBuild(next, 'new-2.js')
    const rename = (from: string, to: string) => {
      if (to === live) throw errorWithCode('EXDEV')
      renameSync(from, to)
    }

    expect(() => publishDist({ next, live, warn: quiet, rename })).toThrow(
      /there was no previous build; the new build is at /,
    )
    expect(readdirSync(root).some(name => name.startsWith('.dist.ready-'))).toBe(true)
  })

  it('moves a dangling dist link aside instead of failing over it', () => {
    symlinkSync(path.join(root, 'gone'), live, 'junction')
    makeBuild(next, 'new-2.js')

    publishDist({ next, live, warn: quiet })

    expect(incompleteReason(live)).toBe('')
  })

  // chmod cannot stop root (or Windows) from removing a child, so there the
  // removal this test needs to fail succeeds.
  it.skipIf(process.platform === 'win32' || process.getuid?.() === 0)('keeps a publish that landed when the old tree cannot be removed', () => {
    makeBuild(live, 'old-1.js')
    mkdirSync(path.join(live, 'locked', 'inner'), { recursive: true })
    chmodSync(path.join(live, 'locked'), 0o555) // its child cannot be unlinked
    makeBuild(next, 'new-2.js')
    const warnings: string[] = []

    try {
      publishDist({ next, live, warn: (m: string) => warnings.push(m) })
    } finally {
      for (const name of readdirSync(root)) {
        const locked = path.join(root, name, 'locked')
        if (existsSync(locked)) chmodSync(locked, 0o755)
      }
    }

    expect(existsSync(path.join(live, 'assets', 'new-2.js'))).toBe(true)
    expect(warnings.join('\n')).toMatch(/could not remove .*\.dist\.prev-/)
  })
})

describe('incompleteReason', () => {
  // test/fixtures/dist_completeness_cases.json, which the Python gate is checked
  // against too: the two gates must accept and refuse the same trees.
  it.each(COMPLETENESS_CASES.map(c => [c.ref, c.reason]))('%s -> %j', (ref, reason) => {
    mkdirSync(path.join(next, 'assets'), { recursive: true })
    writeFileSync(path.join(next, 'assets', 'x.js'), '')
    writeFileSync(path.join(next, 'index.html'), `<script ${ref}></script>`)
    expect(incompleteReason(next)).toBe(reason)
  })

  it('reports a missing index', () => {
    expect(incompleteReason(next)).toBe('no index.html')
  })
})

describe('isMountPoint', () => {
  it('is false for a link, which is renamed like a file', () => {
    makeBuild(path.join(root, 'elsewhere'), 'x-1.js')
    symlinkSync(path.join(root, 'elsewhere'), live, 'junction')
    expect(isMountPoint(live)).toBe(false)
  })

  it('is false for a plain directory and for nothing at all', () => {
    expect(isMountPoint(live)).toBe(false)
    makeBuild(live, 'x-1.js')
    expect(isMountPoint(live)).toBe(false)
  })
})

describe('renameWithRetry', () => {
  it('rides out a transient Windows sharing violation', () => {
    let attempts = 0
    renameWithRetry('a', 'b', {
      rename: () => {
        attempts += 1
        if (attempts < 3) throw errorWithCode('EPERM')
      },
      isWindows: true,
      sleep: () => {},
    })
    expect(attempts).toBe(3)
  })

  it('gives up once the budget is spent', () => {
    let clock = 0
    let attempts = 0
    expect(() =>
      renameWithRetry('a', 'b', {
        rename: () => {
          attempts += 1
          throw errorWithCode('EBUSY')
        },
        isWindows: true,
        sleep: (ms: number) => {
          clock += ms
        },
        now: () => clock,
        budgetMs: 1_000,
      }),
    ).toThrow('EBUSY')
    expect(attempts).toBeGreaterThan(1)
    expect(clock).toBeLessThanOrEqual(1_000)
  })

  it('does not retry on POSIX, where a refused rename is real', () => {
    let attempts = 0
    expect(() =>
      renameWithRetry('a', 'b', {
        rename: () => {
          attempts += 1
          throw errorWithCode('EPERM')
        },
        isWindows: false,
        sleep: () => {},
      }),
    ).toThrow('EPERM')
    expect(attempts).toBe(1)
  })
})

describe('atomicPublishPlugin', () => {
  type Hooked = {
    enforce: string
    config: (c: object) => { build: { outDir: string } } | undefined
    writeBundle: { order: string; sequential: boolean; handler: () => void }
    closeBundle: { order: string; sequential: boolean; handler: () => void }
  }

  function drive(plugin: Hooked, { wrote = true }: { wrote?: boolean } = {}) {
    const { build } = plugin.config({ root, build: { outDir: './dist' } })!
    makeBuild(build.outDir, 'new-2.js')
    if (wrote) plugin.writeBundle.handler()
    plugin.closeBundle.handler()
    return build.outDir
  }

  it('redirects the build to a scratch sibling and publishes it last', () => {
    makeBuild(live, 'old-1.js')
    const plugin = atomicPublishPlugin({ warn: quiet }) as unknown as Hooked
    expect(plugin.enforce).toBe('post')
    expect(plugin.writeBundle.order).toBe('post')
    expect(plugin.closeBundle.order).toBe('post')
    expect(plugin.closeBundle.sequential).toBe(true)

    const scratch = drive(plugin)

    expect(path.dirname(scratch)).toBe(root)
    expect(path.basename(scratch)).toMatch(/^\.dist\.next-\d+-[0-9a-f]+$/)
    expect(existsSync(path.join(live, 'assets', 'new-2.js'))).toBe(true)
    expect(leftovers()).toEqual([])
  })

  it('never publishes a build whose writeBundle chain did not finish, and removes its scratch', () => {
    makeBuild(live, 'old-1.js')
    const plugin = atomicPublishPlugin({ warn: quiet }) as unknown as Hooked

    drive(plugin, { wrote: false })

    expect(existsSync(path.join(live, 'assets', 'old-1.js'))).toBe(true)
    expect(existsSync(path.join(live, 'assets', 'new-2.js'))).toBe(false)
    expect(leftovers()).toEqual([])
  })

  it('starts each build afresh, so a reused instance publishes its second build too', () => {
    const plugin = atomicPublishPlugin({ warn: quiet }) as unknown as Hooked
    drive(plugin)
    rmSync(path.join(live, 'assets', 'new-2.js'))

    drive(plugin)

    expect(existsSync(path.join(live, 'assets', 'new-2.js'))).toBe(true)
  })

  it('sweeps the scratch of a build whose process is gone before the next build', () => {
    // pid 2^31-1 cannot be a live process on any supported platform.
    const abandoned = path.join(root, '.dist.next-2147483647-dead')
    makeBuild(abandoned, 'old-1.js')
    const plugin = atomicPublishPlugin({ warn: quiet }) as unknown as Hooked

    plugin.config({ root, build: { outDir: './dist' } })

    expect(existsSync(abandoned)).toBe(false)
  })

  it('leaves a watch build alone', () => {
    const plugin = atomicPublishPlugin({ warn: quiet }) as unknown as Hooked
    expect(plugin.config({ root, build: { outDir: './dist', watch: {} } })).toBeUndefined()
  })

  it('is the last plugin vite.config.ts registers, so every Vite build publishes atomically', () => {
    const config = readFileSync(path.resolve(__dirname, '../../vite.config.ts'), 'utf-8')
    expect(config).toMatch(/atomicPublishPlugin\(\)\]/)
    const pkg = JSON.parse(readFileSync(path.resolve(__dirname, '../../package.json'), 'utf-8'))
    expect(pkg.scripts.build).not.toMatch(/--outDir/)
  })
})
