import { describe, it, expect, afterEach } from 'vitest'
import { createHash } from 'node:crypto'
import { existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PER_TREE_ENTRIES, UI_AUTO_DIR, baseUiAutoArgs, linkNodeModulesView } from '../../scripts/lib/base-node-modules.mjs'
import { applyAutoStamps } from '../../scripts/lib/ui-auto-stamp.mjs'

/**
 * The `node_modules` the i18n render gate's base build runs against. The base
 * borrows HEAD's packages, and must not borrow HEAD's build cache: the auto
 * stamp manifest in it describes HEAD's text, and the stamp plugin fails a
 * build on any listed file whose text differs -- which, for a branch that edits
 * a stamped component, is the base's copy of that very component.
 */
const dirs: string[] = []
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true })
})

const sha256 = (s: string) => createHash('sha256').update(s).digest('hex')
const HEAD_TEXT = 'export const A = () => <button>{t("a.save")}</button> // edited on the branch\n'
const BASE_TEXT = 'export const A = () => <button>{t("a.save")}</button>\n'

/** A HEAD `node_modules` with one package, a top-level file and a stamp manifest cut from HEAD's text. */
function headNodeModules() {
  const root = mkdtempSync(join(tmpdir(), 'base-nm-'))
  dirs.push(root)
  const nm = join(root, 'head', 'node_modules')
  mkdirSync(join(nm, 'react'), { recursive: true })
  writeFileSync(join(nm, 'react', 'package.json'), '{"name":"react"}')
  mkdirSync(join(nm, '@scope', 'pkg'), { recursive: true })
  writeFileSync(join(nm, '.package-lock.json'), '{}')
  const manifest = join(nm, UI_AUTO_DIR.replace(/^node_modules[\\/]/, ''))
  mkdirSync(manifest, { recursive: true })
  writeFileSync(join(manifest, 'sites.json'), JSON.stringify({
    files: { 'src/A.tsx': { sha256: sha256(HEAD_TEXT), stamps: [] } },
  }))
  return { root, nm }
}

describe('linkNodeModulesView', () => {
  it('reproduces the failure it exists for: HEAD\'s manifest reads the base text as stale', () => {
    const { nm } = headNodeModules()
    const headManifest = JSON.parse(readFileSync(join(nm, '.cache', 'kc-ui-auto', 'sites.json'), 'utf-8'))
    // What the stamp plugin's build mode turns into a hard error.
    expect(applyAutoStamps(BASE_TEXT, headManifest.files['src/A.tsx'])).toEqual({ code: BASE_TEXT, skipped: 'stale' })
  })

  it('gives the base an empty .cache of its own, so HEAD\'s manifest never reaches it', () => {
    const { root, nm } = headNodeModules()
    const view = join(root, 'base', 'website', 'node_modules')
    mkdirSync(join(root, 'base', 'website'), { recursive: true })
    linkNodeModulesView(nm, view)

    expect(lstatSync(join(view, '.cache')).isSymbolicLink()).toBe(false)
    expect(readdirSync(join(view, '.cache'))).toEqual([])
    expect(existsSync(join(view, '.cache', 'kc-ui-auto', 'sites.json'))).toBe(false)
    // HEAD's own manifest is untouched.
    expect(existsSync(join(nm, '.cache', 'kc-ui-auto', 'sites.json'))).toBe(true)
  })

  it('still resolves every package to HEAD\'s installed copy, and copies top-level files', () => {
    const { root, nm } = headNodeModules()
    const view = join(root, 'view')
    const { linked, copied } = linkNodeModulesView(nm, view)

    expect(linked.sort()).toEqual(['@scope', 'react'])
    expect(copied).toEqual(['.package-lock.json'])
    expect(realpathSync(join(view, 'react', 'package.json'))).toBe(realpathSync(join(nm, 'react', 'package.json')))
    expect(lstatSync(join(view, '.package-lock.json')).isSymbolicLink()).toBe(false)
  })

  it('works when HEAD has no .cache at all', () => {
    const { root, nm } = headNodeModules()
    rmSync(join(nm, '.cache'), { recursive: true })
    const view = join(root, 'view')
    linkNodeModulesView(nm, view)
    for (const name of PER_TREE_ENTRIES) expect(readdirSync(join(view, name))).toEqual([])
  })
})

describe('baseUiAutoArgs', () => {
  it('writes the manifest and tier where vite.config.ts reads them, and the index to scratch', () => {
    const viteConfig = readFileSync(join(__dirname, '..', '..', 'vite.config.ts'), 'utf-8')
    const args = baseUiAutoArgs('/tmp/scratch.json')
    const at = (flag: string) => args[args.indexOf(flag) + 1].split('\\').join('/')

    expect(at('--out')).toBe('/tmp/scratch.json')
    expect(viteConfig).toContain(`'./${at('--sites-out')}'`)
    expect(viteConfig).toContain(`'./${at('--auto-out')}'`)
    // Never the committed index or plans module: the exported base stays as committed.
    expect(args).not.toContain('--check')
  })
})
