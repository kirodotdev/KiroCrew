/**
 * The build-time marker step for the find_ui auto tier: inserts
 * `data-ui-auto="<site id>"` into the opening tag of each pointable auto site,
 * as the generator's stamp manifest lists them (`gen-ui-index.mjs
 * --sites-out`, built by `autoStampManifest` in ./ui-index.mjs).
 *
 * Deliberately dumb. It never parses a file and never derives a site id: it
 * copies the generator's ids to the generator's offsets, and only into the
 * exact text the generator scanned (same sha256). Every offset is re-checked
 * against that text before anything is inserted -- the tag name ends there,
 * and the opening tag carries no curated `data-ui-location` (curated wins) and
 * no marker already. A file whose text moved on since the manifest was written
 * is left unstamped (`stale`): a dev server editing sources just loses its
 * auto stamps until the next generator run, it never stamps a wrong element.
 *
 * Kept free of the TypeScript compiler so vite.config.ts can import it cheaply.
 */
import { createHash } from 'node:crypto'

export const AUTO_MARKER_ATTR = 'data-ui-auto'
/** Same shape as AUTO_SITE_ID_RE in ./ui-index.mjs: never a quote or a space. */
const SITE_RE = /^auto:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+(?::[0-9]+)?$/
const CURATED = /data-ui-location|\buiLocation\(/

/**
 * Stamp one file. `entry` is the manifest's `files[<rel>]`. Returns
 * `{ code, stamped }`, or `{ code, skipped: 'stale' }` (unchanged) when the
 * text is not the one the manifest was built from. Throws on a manifest that
 * does not fit its own text (a generator bug, never a user edit).
 */
export function applyAutoStamps(code, entry) {
  if (!entry || !Array.isArray(entry.stamps)) return { code, stamped: 0 }
  if (createHash('sha256').update(code).digest('hex') !== entry.sha256) return { code, skipped: 'stale' }
  let out = code
  let stamped = 0
  for (const s of [...entry.stamps].sort((a, b) => b.offset - a.offset)) {
    const { offset, open_end: openEnd, tag, site } = s
    if (typeof site !== 'string' || !SITE_RE.test(site)) throw new Error(`auto stamp: malformed site id ${JSON.stringify(site)}`)
    if (!Number.isInteger(offset) || !Number.isInteger(openEnd) || openEnd <= offset || openEnd > code.length) {
      throw new Error(`auto stamp ${site}: offsets out of range`)
    }
    if (code.slice(offset - tag.length, offset) !== tag || code[offset - tag.length - 1] !== '<' || !/[\s/>]/.test(code[offset])) {
      throw new Error(`auto stamp ${site}: no <${tag} tag ends at offset ${offset}`)
    }
    const opening = code.slice(offset, openEnd)
    // Curated wins: an element the registry already marks is never also auto.
    if (CURATED.test(opening) || opening.includes(AUTO_MARKER_ATTR)) continue
    out = `${out.slice(0, offset)} ${AUTO_MARKER_ATTR}="${site}"${out.slice(offset)}`
    stamped++
  }
  return { code: out, stamped }
}

/**
 * The Vite plugin around {@link applyAutoStamps}. Reads the stamp manifest and
 * the auto-tier artifact the generator wrote (`npm run build` runs it first,
 * into `node_modules/.cache/kc-ui-auto/`), then:
 *
 * - stamps every listed source as it is transformed (`enforce: 'pre'`, before
 *   JSX is compiled), in a build and on the dev server alike. In a build a
 *   stale file fails the build; on the dev server it is left unstamped;
 * - defines `__UI_AUTO_BUILD_DIGEST__` (the manifest's `build_digest`, which
 *   is the artifact's), so the bundle can tell an auto guide of its own build
 *   from another's;
 * - emits the artifact as `ui-index.auto.json` into the build output, so the
 *   shipped auto tier is byte for byte the one these stamps were cut from.
 *
 * No manifest: nothing is stamped, the digest is empty (every auto guide is
 * refused as `build_mismatch`) and nothing is emitted. Under Vitest the plugin
 * does nothing at all, so a test never depends on whether a build ran.
 */
export function uiAutoStampPlugin({ root, manifestPath, artifactPath, readFile, exists, env = process.env }) {
  const name = 'kirocrew-ui-auto-stamp'
  if (env.VITEST) return { name }
  const manifest = exists(manifestPath) ? JSON.parse(readFile(manifestPath)) : null
  const files = manifest?.files ?? {}
  const digest = typeof manifest?.build_digest === 'string' ? manifest.build_digest : ''
  let command = 'serve'
  const warned = new Set()
  return {
    name,
    enforce: 'pre',
    config(_config, { command: cmd }) {
      command = cmd
      return { define: { __UI_AUTO_BUILD_DIGEST__: JSON.stringify(digest) } }
    },
    transform(code, id) {
      const file = id.split('?')[0]
      if (!file.startsWith(root)) return null
      const rel = file.slice(root.length).replace(/^[\\/]+/, '').split('\\').join('/')
      const entry = Object.hasOwn(files, rel) ? files[rel] : null
      if (!entry) return null
      const r = applyAutoStamps(code, entry)
      if (r.skipped) {
        if (command === 'build') this.error(`${rel} changed after the auto stamp manifest was written; rerun the build`)
        if (!warned.has(rel)) {
          warned.add(rel)
          this.warn(`${rel} changed since the auto stamp manifest; its auto sites are unstamped until the next build`)
        }
        return null
      }
      // `map: null` keeps the existing mappings: an insert never adds or
      // moves a line, it only shifts the rest of that line by a few columns.
      return r.stamped ? { code: r.code, map: null } : null
    },
    generateBundle() {
      if (command !== 'build' || !manifest || !exists(artifactPath)) return
      const source = readFile(artifactPath)
      if (JSON.parse(source).build_digest !== digest) this.error('the auto tier artifact and the stamp manifest come from different generator runs')
      this.emitFile({ type: 'asset', fileName: 'ui-index.auto.json', source })
    },
  }
}
