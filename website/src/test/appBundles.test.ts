/**
 * The builtin app bundles `vite.app-bundles.config.ts` builds agree with the
 * manifests that mount them: every listed entry exists, lands where the app's
 * `panelTabs`/`ui.entry` points, and leaves the host's modules external.
 */
import { describe, expect, it } from 'vitest'
import { existsSync, readFileSync } from 'fs'
import { resolve } from 'path'

const WEBSITE = resolve(__dirname, '..', '..')
const BUILTINS = resolve(WEBSITE, '..', 'src', 'kiro_crew', 'apps', 'builtins')
const config = readFileSync(resolve(WEBSITE, 'vite.app-bundles.config.ts'), 'utf-8')

/** The `BUNDLES` rows, read from the config text so the test needs no vite import. */
function bundles(): { app: string; name: string; entry: string }[] {
  const block = config.match(/const BUNDLES[^=]*=\s*\[([\s\S]*?)\n\]/)?.[1] ?? ''
  return [...block.matchAll(/app:\s*'([^']+)',\s*name:\s*'([^']+)',\s*entry:\s*'([^']+)'/g)]
    .map(m => ({ app: m[1], name: m[2], entry: m[3] }))
}

describe('builtin app bundles', () => {
  it('lists at least one bundle, each with a source entry that exists', () => {
    const rows = bundles()
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) {
      expect(existsSync(resolve(WEBSITE, row.entry)), `${row.app}: ${row.entry}`).toBe(true)
    }
  })

  it('emits each bundle where its manifest mounts it', () => {
    for (const row of bundles()) {
      const manifest = JSON.parse(readFileSync(resolve(BUILTINS, row.app, 'app.json'), 'utf-8')) as {
        ui?: { entry?: string }
        contributes?: { panelTabs?: { entry?: string }[] }
      }
      const declared = [manifest.ui?.entry, ...(manifest.contributes?.panelTabs ?? []).map(t => t.entry)].filter(Boolean)
      expect(declared, `${row.app} declares no bundle entry`).toContain(`dist/${row.name}.mjs`)
    }
  })

  it('keeps every host-provided module external', () => {
    // The import map resolves these to the host's copies; a bundle carrying its
    // own React would break hooks, and its own SDK would lose the host's identity.
    for (const spec of ['react', 'react-dom', 'react/jsx-runtime', '@kirocrew/app-sdk', '@kirocrew/app-sdk/ui', '@tanstack/react-query', 'lucide-react']) {
      expect(config, spec).toContain(`'${spec}'`)
    }
  })

  it('is what `npm run build:app-bundles` runs, once per app', () => {
    const pkg = JSON.parse(readFileSync(resolve(WEBSITE, 'package.json'), 'utf-8')) as { scripts: Record<string, string> }
    for (const row of bundles()) {
      expect(pkg.scripts['build:app-bundles']).toContain(`APP_BUNDLE=${row.app}`)
    }
    expect(pkg.scripts.build).toContain('build:app-bundles')
  })
})
