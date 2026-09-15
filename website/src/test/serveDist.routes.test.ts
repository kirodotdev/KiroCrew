/**
 * `scripts/lib/serve-dist.mjs` — the static server every screenshot harness in
 * `website/scripts/` runs the built SPA behind.
 *
 * The bug this locks: `/logo.png` is not in `website/dist`. It is an aiohttp
 * route in the real dashboard (`server.py`: `add_get("/logo.png",
 * handlers.logo)`) reading the packaged PNG. A plain static server therefore
 * 404s it and falls through to the SPA `index.html`, so the brand mark rendered
 * as a broken-image placeholder in EVERY harness frame — for a long time, in
 * every capture script, unnoticed because reviewers were looking at whatever
 * else each frame was capturing.
 *
 * Locks the contract:
 *  (1) `/logo.png` is served as the packaged PNG, byte for byte — not the
 *      index.html fallback wearing a .png name.
 *  (2) The SPA fallback and ordinary asset serving still work.
 *  (3) The path-traversal guard is intact. This matters: the server-routed
 *      lookup was added BEFORE the containment check, so it is the one change
 *      here that could have widened the attack surface.
 *
 * Driven through a real `node` child process, not an import: this is a plain
 * `.mjs` harness module that resolves its own paths from `import.meta.url`, and
 * the test runner's transform rewrites that to a non-`file:` URL, which
 * `fileURLToPath` rejects. Spawning node also exercises the module exactly as
 * every capture script uses it — including the path resolution that finds the
 * packaged PNG, which an import-based test would have stubbed away.
 */
import { describe, it, expect } from 'vitest'
import { execFileSync } from 'node:child_process'
import { readFileSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

// `__dirname`, not `import.meta.url`: the runner's transform rewrites the latter
// to a non-`file:` URL, which `fileURLToPath` rejects. Every other path-reading
// test in this folder resolves the same way.
const WEBSITE = resolve(__dirname, '..', '..')
const LOGO = resolve(WEBSITE, '..', 'src', 'kiro_crew', 'static', 'kirocrew-logo.png')

interface Probe {
  status: number
  type: string | null
  len: number
  sha: string
  head: string
}

/** Boot serveDist in a child node process, probe the given paths, return JSON. */
function probe(paths: string[], root: string): Record<string, Probe> {
  const dir = mkdtempSync(join(tmpdir(), 'serve-dist-probe-'))
  const script = join(dir, 'probe.mjs')
  // `pathToFileURL(...).href`, NOT the raw path: on Windows the absolute path is
  // `C:\…`, and a JSON-stringified `"C:\\…"` is not a valid ESM specifier —
  // Node reads `C:` as a URL scheme and refuses with
  // ERR_UNSUPPORTED_ESM_URL_SCHEME before the child probes anything, failing the
  // Windows suite. A file:// URL is the portable form.
  const moduleUrl = pathToFileURL(join(WEBSITE, 'scripts/lib/serve-dist.mjs')).href
  writeFileSync(script, `
import { createHash } from 'node:crypto'
import { serveDist } from ${JSON.stringify(moduleUrl)}
const { srv, base } = await serveDist(${JSON.stringify(root)})
const out = {}
for (const p of ${JSON.stringify(paths)}) {
  const res = await fetch(base + p)
  const buf = Buffer.from(await res.arrayBuffer())
  out[p] = {
    status: res.status,
    type: res.headers.get('content-type'),
    len: buf.length,
    sha: createHash('sha256').update(buf).digest('hex'),
    head: buf.subarray(0, 16).toString('latin1'),
  }
}
srv.close()
process.stdout.write(JSON.stringify(out))
`)
  try {
    return JSON.parse(execFileSync(process.execPath, [script], { encoding: 'utf8', timeout: 30_000 }))
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

const sha256 = async (buf: Buffer) => {
  const { createHash } = await import('node:crypto')
  return createHash('sha256').update(buf).digest('hex')
}

// Serve website/ itself rather than dist/: dist only exists after a build, and
// nothing asserted here depends on the bundle's contents.
const RESULTS = probe(
  // Only ENCODED traversal is worth probing: a literal "/../x" is normalised
  // away by the client's URL parser and never reaches the server, so asserting
  // on it would test `fetch`, not `serveDist`. `pyproject.toml` sits one level
  // ABOVE the served root, so a 200 there would be an unambiguous escape.
  ['/logo.png', '/settings', '/package.json', '/%2e%2e%2fpyproject.toml',
    '/%2e%2e%2f%2e%2e%2fREADME.md', '/%ZZ'],
  WEBSITE,
)

describe('serveDist server-routed assets', () => {
  it('serves /logo.png as the packaged PNG, byte for byte', async () => {
    const r = RESULTS['/logo.png']
    expect(r.status).toBe(200)
    expect(r.type).toBe('image/png')
    expect(r.sha).toBe(await sha256(readFileSync(LOGO)))
  })

  it('is a decodable PNG, not index.html under a .png name', () => {
    // The exact failure mode being locked out: an <img> pointing at HTML renders
    // as a broken-image placeholder, which is what every frame showed.
    const { head } = RESULTS['/logo.png']
    expect([...Buffer.from(head, 'latin1').subarray(0, 4)]).toEqual([0x89, 0x50, 0x4e, 0x47])
    expect(head.toLowerCase()).not.toContain('<!')
  })

  it('still falls back to index.html for SPA deep links', () => {
    const r = RESULTS['/settings']
    expect(r.status).toBe(200)
    expect(r.head.toLowerCase()).toContain('<!doctype')
  })

  it('still serves ordinary files from the root', () => {
    expect(RESULTS['/package.json'].status).toBe(200)
  })

  it('keeps the traversal guard — the routed lookup runs before it', () => {
    // The routed map is an exact-key match against one hardcoded key, so no
    // user-controlled path can reach the filesystem through it. These prove the
    // ordinary path is still contained: percent-encoded "../" survives URL
    // normalisation and only becomes a traversal at decode time, which is why
    // normalising alone was never the defence.
    expect(RESULTS['/%2e%2e%2fpyproject.toml'].status).toBe(403)
    expect(RESULTS['/%2e%2e%2f%2e%2e%2fREADME.md'].status).toBe(403)
  })

  it('rejects malformed percent-escapes instead of crashing', () => {
    expect(RESULTS['/%ZZ'].status).toBe(400)
  })
})
