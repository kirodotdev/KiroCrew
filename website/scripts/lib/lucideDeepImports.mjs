/**
 * Host imports of `lucide-react` go straight to each icon's own module.
 *
 * The `lucide-react` barrel does `import * as index from './icons/index.js';
 * export { index as icons }`, so any import of the barrel keeps every icon
 * (1,695 modules) live. The host only uses a few hundred. Installed apps still
 * get the whole set: `src/app-sdk/shared-modules.ts` imports the barrel, and that
 * module loads lazily, before the first app bundle runs (`appBundleImport.ts`).
 *
 * So a build rewrites `import { X, Y as Z } from 'lucide-react'` in host code to
 * `import X from 'lucide-react/dist/esm/icons/x.js'`, using the barrel's own
 * `export { ... } from './...'` lines as the name map. Any host import of the
 * barrel left after the rewrite fails the build: one missed name would quietly
 * bring the whole icon set back into first load.
 */
import { readFileSync } from 'node:fs'

const PKG = 'lucide-react'
const BARREL_FILE = 'dist/esm/lucide-react.js'

/**
 * Name -> { file, imported } from the barrel source. `imported` is 'default' for
 * `default as X`, else the module's own export name.
 */
export function lucideExportMap(barrelSource) {
  const map = new Map()
  for (const m of barrelSource.matchAll(/export \{([^}]*)\} from '\.\/([^']+)';/g)) {
    for (const part of m[1].split(',')) {
      const s = part.trim()
      if (!s) continue
      const [imported, local = imported] = s.split(/\s+as\s+/)
      map.set(local, { file: m[2], imported })
    }
  }
  return map
}

const NAMED_IMPORT = /import\s*\{([^}]*)\}\s*from\s*(['"])lucide-react\2;?/g
const ANY_BARREL_REF = /\bfrom\s*['"]lucide-react['"]|\bimport\s*\(?\s*['"]lucide-react['"]/

/**
 * Rewrites named barrel imports in `code`. Returns the new code (or null when
 * nothing changed) and `left`: whether any barrel reference remains.
 */
export function rewriteLucideImports(code, map) {
  let changed = false
  const out = code.replace(NAMED_IMPORT, (whole, specs) => {
    const lines = []
    const rest = []
    for (const raw of specs.split(',')) {
      const s = raw.trim()
      if (!s || s.startsWith('type ')) continue
      const [imported, local = imported] = s.split(/\s+as\s+/)
      const hit = map.get(imported)
      if (!hit) { rest.push(s); continue }
      const from = `${PKG}/dist/esm/${hit.file}`
      lines.push(hit.imported === 'default'
        ? `import ${local} from '${from}';`
        : `import { ${hit.imported === local ? local : `${hit.imported} as ${local}`} } from '${from}';`)
    }
    if (rest.length) lines.push(`import { ${rest.join(', ')} } from '${PKG}';`)
    changed = true
    return lines.join('\n')
  })
  const result = changed ? out : code
  return { code: changed ? out : null, left: ANY_BARREL_REF.test(result) }
}

/**
 * Build-only plugin. `allow` is the absolute path of the one host module that
 * may import the barrel (the app-sdk registry).
 */
export function lucideDeepImportsPlugin({ root, allow }) {
  const barrel = `${root}/node_modules/${PKG}/${BARREL_FILE}`
  let map
  return {
    name: 'kirocrew-lucide-deep-imports',
    apply: 'build',
    buildStart() {
      map = lucideExportMap(readFileSync(barrel, 'utf8'))
      if (map.size < 1000) this.error(`lucide-react barrel at ${barrel} no longer matches the expected export lines`)
    },
    transform(code, id) {
      if (id.startsWith('\0') || id.includes('/node_modules/') || !code.includes(PKG)) return null
      if (id.split('?')[0] === allow) return null
      const { code: out, left } = rewriteLucideImports(code, map)
      if (left) {
        this.error(
          `${id} still imports '${PKG}' by its barrel. That keeps all of lucide's icons in the`
            + ` dashboard's first load. Import named icons only, or add the missing name to`
            + ` scripts/lib/lucideDeepImports.mjs.`,
        )
      }
      return out === null ? null : { code: out, map: null }
    },
  }
}
