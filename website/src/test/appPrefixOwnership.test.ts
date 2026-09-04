/**
 * No host query may sit under a builtin app's key prefix.
 *
 * This is the invariant that makes `[appId]` retention safe. The host registers
 * `gcTime` for an app's whole prefix, so any query keyed `['<some-appId>', ...]`
 * from OUTSIDE that app inherits the app's retention the moment the app's route
 * renders — a host cache decision made by an app, which is exactly what
 * `appCacheRetention.ts` refuses to do on purpose.
 *
 * It held when this feature landed, established by hand. A hand audit is a fact
 * about one afternoon, so this turns it into a property of the tree: the registry
 * is the ownership map (it already pairs each appId with the module that owns it),
 * and any other file claiming that prefix fails here.
 *
 * The detector is exercised against a PLANTED violation as well as the real tree,
 * because a scanner that finds nothing and a scanner that looks at nothing are
 * indistinguishable from a green test.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

const SRC = resolve(__dirname, '..')

/** One source file, as the detector sees it. */
interface SourceFile {
  /** Posix-style path relative to `src/`. */
  path: string
  text: string
}

/**
 * appId -> the directory that owns it, derived from the registry's own import.
 *
 * `'/aws-control': { component: lazy(() => import('./aws-control/AwsControlPage')),
 * appId: 'aws-control' }` yields `apps/aws-control`; a page filed outside `apps/`
 * (`import('../pages/DevFleetPage')`) yields `pages`. Deriving ownership from the
 * registry rather than a hand-written list is the point — a new app is covered
 * the moment it is registered, with nothing to remember to update.
 */
export function parseRegistryOwners(registrySource: string): Map<string, string> {
  const owners = new Map<string, string>()
  const entry = /import\('([^']+)'\)\),\s*appId:\s*'([^']+)'/g
  for (const [, importPath, appId] of registrySource.matchAll(entry)) {
    // Import paths are relative to `src/apps/`. Resolve to a `src/`-relative dir.
    const fromApps = importPath.replace(/^\.\//, 'apps/').replace(/^\.\.\//, '')
    const dir = fromApps.slice(0, fromApps.lastIndexOf('/'))
    owners.set(appId, dir)
  }
  return owners
}

/** A host file claiming an app's key prefix. */
export interface ForeignPrefixUse {
  appId: string
  path: string
  ownerDir: string
}

/**
 * Every use of `queryKey: ['<appId>'` from outside that app's owning directory.
 *
 * Tests are excluded: a test may legitimately assert on an app's key from
 * anywhere, and this is about what the shipped dashboard queries.
 */
export function findForeignPrefixUses(
  owners: Map<string, string>,
  files: readonly SourceFile[],
): ForeignPrefixUse[] {
  const found: ForeignPrefixUse[] = []
  for (const [appId, ownerDir] of owners) {
    const claim = new RegExp(`queryKey:\\s*\\[\\s*'${appId.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}'`)
    for (const file of files) {
      if (file.path.includes('/test/') || /\.test\.tsx?$/.test(file.path)) continue
      if (file.path === ownerDir || file.path.startsWith(`${ownerDir}/`)) continue
      if (claim.test(file.text)) found.push({ appId, path: file.path, ownerDir })
    }
  }
  return found
}

/** Every .ts/.tsx file under `src/`, as `src/`-relative paths. */
function readSourceTree(dir = SRC, prefix = ''): SourceFile[] {
  const out: SourceFile[] = []
  for (const name of readdirSync(dir)) {
    const abs = join(dir, name)
    const rel = prefix ? `${prefix}/${name}` : name
    if (statSync(abs).isDirectory()) {
      out.push(...readSourceTree(abs, rel))
    } else if (/\.tsx?$/.test(name)) {
      out.push({ path: rel, text: readFileSync(abs, 'utf8') })
    }
  }
  return out
}

describe('app key-prefix ownership', () => {
  const registry = readFileSync(join(SRC, 'apps', 'builtinRegistry.ts'), 'utf8')
  const owners = parseRegistryOwners(registry)

  it('derives an owner for every registered app', () => {
    // If the registry's shape changes and this parse silently yields nothing, the
    // scan below would pass by having no appIds to check — the classic vacuous
    // ratchet. Pinning the count and one known-awkward case closes that.
    expect(owners.size).toBeGreaterThan(15)
    // The route and the app name differ here, which is why ownership is read from
    // the registry rather than from the URL.
    expect(owners.get('agent-worlds')).toBe('pages')
    expect(owners.get('aws-control')).toBe('apps/aws-control')
  })

  it('finds no host query sitting under an app prefix', () => {
    const violations = findForeignPrefixUses(owners, readSourceTree())
    expect(
      violations,
      `A query outside an app claims its key prefix, so it would inherit that app's `
        + `30-minute retention once the app's route renders:\n`
        + violations.map((v) => `  ['${v.appId}', ...] in ${v.path} (owner: ${v.ownerDir})`).join('\n'),
    ).toEqual([])
  })

  it('detects a planted violation, so a green scan means something', () => {
    // MUTATION-equivalent, without editing a real host file: the detector is fed a
    // host file that claims an app's prefix and must report it. Deleting the
    // `claim.test(...)` check reds this while leaving the case above green.
    const planted: SourceFile[] = [
      { path: 'pages/SomeHostPage.tsx', text: "useQuery({ queryKey: ['aws-control', 'drive'] })" },
    ]
    expect(findForeignPrefixUses(owners, planted)).toEqual([
      { appId: 'aws-control', path: 'pages/SomeHostPage.tsx', ownerDir: 'apps/aws-control' },
    ])
  })

  it('does not flag the app using its own prefix', () => {
    // The app's own files are exactly what SHOULD claim the prefix; flagging them
    // would make the invariant unsatisfiable.
    const own: SourceFile[] = [
      { path: 'apps/aws-control/DrivePage.tsx', text: "queryKey: ['aws-control', 'drive', a]" },
    ]
    expect(findForeignPrefixUses(owners, own)).toEqual([])
  })

  it('ignores tests, which may assert on any app key', () => {
    const inTest: SourceFile[] = [
      { path: 'test/somewhere.test.tsx', text: "queryKey: ['aws-control', 'drive']" },
      { path: 'apps/aws-control/DrivePage.test.tsx', text: "queryKey: ['issue-radar', 'x']" },
    ]
    expect(findForeignPrefixUses(owners, inTest)).toEqual([])
  })
})
