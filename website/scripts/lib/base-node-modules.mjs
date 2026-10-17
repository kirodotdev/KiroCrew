/**
 * The `node_modules` an exported base tree builds against, for the i18n render
 * gate's `[vs-base]` half (see `buildBaseBundle` in ../check-i18n-render.mjs).
 *
 * The base reuses HEAD's installed packages rather than paying for a second
 * `npm ci`, but it must NOT reuse HEAD's `node_modules/.cache`. That directory
 * holds build outputs, not packages, and one of them is the auto stamp manifest
 * (`.cache/kc-ui-auto/sites.json`) that `vite.config.ts` reads from beside
 * itself: it lists, per source file, the sha256 of the exact text it was cut
 * from, and `vite build` FAILS on any listed file whose text differs. Shared with the base, HEAD's manifest describes HEAD's
 * text, so every branch that edits a stamped file (any control with a static
 * label) killed the base build -- on code that is not in its diff.
 *
 * So the base gets a view: every top-level entry of HEAD's `node_modules`
 * linked in one by one, except `.cache`, which is a fresh empty directory the
 * base tree's own generator then fills (`baseUiAutoArgs`). Linking per entry
 * rather than the whole directory keeps the install cost at zero; module
 * resolution follows each link to the same real path it did before.
 *
 * Directories are linked as junctions on Windows (a directory symlink there
 * needs elevated privileges or Developer Mode). Plain files at the top level
 * (`.package-lock.json`, and so on) are copied, because a FILE symlink has the
 * same privilege problem and a junction cannot point at a file.
 */
import { copyFileSync, mkdirSync, readdirSync, statSync, symlinkSync } from 'node:fs'
import { join } from 'node:path'

/** Build outputs, never packages: each tree must produce its own. */
export const PER_TREE_ENTRIES = new Set(['.cache'])

/** Where `vite.config.ts` reads the auto stamp manifest and auto tier from, relative to website/. */
export const UI_AUTO_DIR = join('node_modules', '.cache', 'kc-ui-auto')

/**
 * Fill `dest` (which must not exist yet) with a view of `source`: every entry
 * linked or copied, except {@link PER_TREE_ENTRIES}, which are created empty.
 * Returns the names linked and copied, for a caller that wants to report them.
 */
export function linkNodeModulesView(source, dest, { platform = process.platform } = {}) {
  mkdirSync(dest)
  const linked = []
  const copied = []
  for (const name of readdirSync(source)) {
    if (PER_TREE_ENTRIES.has(name)) continue
    const from = join(source, name)
    const to = join(dest, name)
    if (statSync(from).isDirectory()) {
      symlinkSync(from, to, platform === 'win32' ? 'junction' : 'dir')
      linked.push(name)
    } else {
      copyFileSync(from, to)
      copied.push(name)
    }
  }
  for (const name of PER_TREE_ENTRIES) mkdirSync(join(dest, name))
  return { linked, copied }
}

/**
 * The generator arguments that write the base tree's OWN auto stamp manifest
 * and auto tier to the paths its `vite.config.ts` reads. `--out` sends the
 * index to a scratch file, so the exported tree's committed index and plans
 * module are left exactly as the base commit has them.
 */
export function baseUiAutoArgs(scratchIndex) {
  return [
    '--out', scratchIndex,
    '--auto-out', join(UI_AUTO_DIR, 'ui-index.auto.json'),
    '--sites-out', join(UI_AUTO_DIR, 'sites.json'),
  ]
}
