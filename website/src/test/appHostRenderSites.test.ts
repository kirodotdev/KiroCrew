/**
 * Every place that renders `AppHost`, and why each one is right about the reload
 * hold (`components/keptAppReloadHold.ts`). A window reload ends every kept page's
 * work, so a host under `KeptAppPagesProvider` must read the hold; AppHost reads it
 * itself, so a site needs no prop. A new render site fails this test until it is
 * listed here with the reason it is correct.
 */
import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const SRC = join(__dirname, '..')

const SITES: Record<string, string> = {
  // Under the provider: the routed page's host, the hold read from the context.
  'components/KeptAppPages.tsx': 'under the provider',
  // Under the provider (ChatPage and the Members page are routes inside it).
  'pages/chat/SidePanelTabBodies.tsx': 'under the provider',
  // The inline fallback for a surface without the provider: nothing is kept there.
  'components/InstalledAppSurface.tsx': 'outside the provider',
  // Startup mounts never reload (`suppressReload`).
  'components/StartupModules.tsx': 'never reloads',
  // A desktop window is its own document; a reload there reloads only that window.
  'desktopWindow/DesktopWindowRoot.tsx': 'own document',
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap(name => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return name === 'test' ? [] : sourceFiles(path)
    return /\.tsx?$/.test(name) && !/\.test\./.test(name) ? [path] : []
  })
}

describe('AppHost render sites', () => {
  it('are exactly the listed ones', () => {
    const found = sourceFiles(SRC)
      .filter(path => /<AppHost\b/.test(readFileSync(path, 'utf-8')))
      .map(path => relative(SRC, path).split('\\').join('/'))
      .sort()
    expect(found).toEqual(Object.keys(SITES).sort())
  })

  it('pass no reload-hold prop, so the hold comes from AppHost itself', () => {
    for (const site of Object.keys(SITES)) {
      expect(readFileSync(join(SRC, site), 'utf-8')).not.toMatch(/holdReload=/)
    }
  })
})
