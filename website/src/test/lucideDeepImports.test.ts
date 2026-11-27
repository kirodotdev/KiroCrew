import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it, vi } from 'vitest'
import { lucideExportMap, lucideDeepImportsPlugin, rewriteLucideImports } from '../../scripts/lib/lucideDeepImports.mjs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const BARREL = `${root}/node_modules/lucide-react/dist/esm/lucide-react.js`
const map = lucideExportMap(readFileSync(BARREL, 'utf8'))

describe('lucideExportMap', () => {
  it('maps every icon name and alias, plus the non-icon exports', () => {
    expect(map.size).toBeGreaterThan(1000)
    expect(map.get('Search')).toEqual({ file: 'icons/search.js', imported: 'default' })
    expect(map.get('SearchIcon')).toEqual(map.get('Search'))
    expect(map.get('LucideProvider')).toEqual({ file: 'context.js', imported: 'LucideProvider' })
    expect(map.get('createLucideIcon')).toEqual({ file: 'createLucideIcon.js', imported: 'default' })
  })
})

describe('rewriteLucideImports', () => {
  it('turns named barrel imports into per-module imports', () => {
    const { code, left } = rewriteLucideImports(
      `import {\n  Search,\n  X as Close,\n  LucideProvider,\n} from "lucide-react";\nuse(Search, Close)`,
      map,
    )
    expect(left).toBe(false)
    expect(code).toContain(`import Search from 'lucide-react/dist/esm/icons/search.js';`)
    expect(code).toContain(`import Close from 'lucide-react/dist/esm/icons/x.js';`)
    expect(code).toContain(`import { LucideProvider } from 'lucide-react/dist/esm/context.js';`)
    expect(code).not.toMatch(/from ['"]lucide-react['"]/)
  })

  it('leaves code without a barrel import alone', () => {
    expect(rewriteLucideImports(`import x from 'lucide-react/dist/esm/icons/x.js'`, map))
      .toEqual({ code: null, left: false })
  })

  it.each([
    [`import { NotAnIcon } from 'lucide-react'`],
    [`import { icons } from 'lucide-react'`],
    [`import * as all from 'lucide-react'`],
    [`export { Search } from 'lucide-react'`],
    [`const m = import('lucide-react')`],
  ])('reports a barrel reference it cannot rewrite: %s', src => {
    expect(rewriteLucideImports(src, map).left).toBe(true)
  })
})

describe('lucideDeepImportsPlugin', () => {
  const allow = `${root}/src/app-sdk/shared-modules.ts`
  const plugin = lucideDeepImportsPlugin({ root, allow })
  const ctx = { error: vi.fn((msg: string) => { throw new Error(msg) }) }
  plugin.buildStart.call(ctx)

  it('fails the build when a host file keeps a barrel import', () => {
    expect(() => plugin.transform.call(ctx, `import { icons } from 'lucide-react'`, `${root}/src/x.tsx`))
      .toThrow(/still imports 'lucide-react'/)
  })

  it('lets the app-sdk registry import the whole namespace', () => {
    expect(plugin.transform.call(ctx, `import * as lucideReact from 'lucide-react'`, allow)).toBeNull()
  })

  it('rewrites host imports', () => {
    const out = plugin.transform.call(ctx, `import { Search } from 'lucide-react'`, `${root}/src/x.tsx`)
    expect(out?.code).toBe(`import Search from 'lucide-react/dist/esm/icons/search.js';`)
  })
})
