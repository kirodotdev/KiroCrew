import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { mkdtempSync, mkdirSync, writeFileSync, readdirSync, existsSync, readFileSync, rmSync, symlinkSync } from 'node:fs'
import { gunzipSync, brotliDecompressSync } from 'node:zlib'
import { tmpdir } from 'node:os'
import path from 'node:path'

import {
  shouldCompress,
  compressDir,
  precompressPlugin,
  MIN_SIZE_BYTES,
  COMPRESSIBLE_EXTENSIONS,
} from '../../scripts/precompress.mjs'

/** Compressible filler — repetitive so gzip/brotli always shrink it. */
const filler = (bytes: number) => 'console.log("aaaaaaaaaaaaaaaa");'.repeat(Math.ceil(bytes / 32)).slice(0, bytes)

let dir: string

beforeEach(() => { dir = mkdtempSync(path.join(tmpdir(), 'precompress-')) })
afterEach(() => { rmSync(dir, { recursive: true, force: true }) })

describe('shouldCompress', () => {
  it('accepts the compressible extensions above the size floor', () => {
    for (const ext of COMPRESSIBLE_EXTENSIONS) {
      expect(shouldCompress(`index-abc123${ext}`, MIN_SIZE_BYTES)).toBe(true)
    }
  })

  it('skips files below the size floor', () => {
    // Below this, a second representation costs more than it saves.
    expect(shouldCompress('tiny-abc.js', MIN_SIZE_BYTES - 1)).toBe(false)
  })

  it('skips already-compressed media and fonts', () => {
    for (const name of ['logo-abc.png', 'demo-abc.webp', 'font-abc.woff2', 'clip-abc.mp4']) {
      expect(shouldCompress(name, 500_000)).toBe(false)
    }
  })

  it('never recurses into siblings it emitted', () => {
    // Guards against .js.gz.gz.gz growth across repeated builds.
    expect(shouldCompress('index-abc.js.gz', 500_000)).toBe(false)
    expect(shouldCompress('index-abc.js.br', 500_000)).toBe(false)
  })
})

describe('compressDir', () => {
  it('writes gzip and brotli siblings whose contents round-trip', () => {
    const body = filler(4096)
    writeFileSync(path.join(dir, 'index-abc123.js'), body)
    const stats = compressDir(dir)

    expect(stats.files).toBe(1)
    expect(existsSync(path.join(dir, 'index-abc123.js.gz'))).toBe(true)
    expect(existsSync(path.join(dir, 'index-abc123.js.br'))).toBe(true)
    // The bytes a browser will decode must equal the original, or we ship
    // silently-corrupt JS with a fresh-looking ETag.
    expect(gunzipSync(readFileSync(path.join(dir, 'index-abc123.js.gz'))).toString()).toBe(body)
    expect(brotliDecompressSync(readFileSync(path.join(dir, 'index-abc123.js.br'))).toString()).toBe(body)
  })

  it('reports a real size reduction', () => {
    writeFileSync(path.join(dir, 'main-abc123.js'), filler(200_000))
    const stats = compressDir(dir)
    expect(stats.gzipBytes).toBeLessThan(stats.rawBytes / 2)
    expect(stats.brotliBytes).toBeLessThan(stats.gzipBytes)
  })

  it('recurses into subdirectories', () => {
    mkdirSync(path.join(dir, 'nested'))
    writeFileSync(path.join(dir, 'nested', 'chunk-abc123.js'), filler(4096))
    expect(compressDir(dir).files).toBe(1)
    expect(existsSync(path.join(dir, 'nested', 'chunk-abc123.js.gz'))).toBe(true)
  })

  it('leaves ineligible files alone', () => {
    writeFileSync(path.join(dir, 'logo-abc123.png'), filler(4096))
    writeFileSync(path.join(dir, 'tiny-abc123.js'), 'x')
    expect(compressDir(dir).files).toBe(0)
    expect(existsSync(path.join(dir, 'logo-abc123.png.gz'))).toBe(false)
    expect(existsSync(path.join(dir, 'tiny-abc123.js.gz'))).toBe(false)
  })

  it('does not keep a sibling that would be larger than the source', () => {
    // Incompressible payload: serving a bigger "compressed" response would be
    // a pessimisation, and aiohttp would happily do it if the sibling existed.
    const random = Buffer.alloc(2048)
    for (let i = 0; i < random.length; i++) random[i] = Math.floor(Math.random() * 256)
    writeFileSync(path.join(dir, 'noise-abc123.json'), random)
    compressDir(dir)
    const gz = path.join(dir, 'noise-abc123.json.gz')
    if (existsSync(gz)) {
      expect(readFileSync(gz).length).toBeLessThan(random.length)
    }
  })

  it('is idempotent across repeated builds', () => {
    writeFileSync(path.join(dir, 'index-abc123.js'), filler(4096))
    const first = compressDir(dir)
    const second = compressDir(dir)
    expect(second.files).toBe(first.files)
    expect(existsSync(path.join(dir, 'index-abc123.js.gz.gz'))).toBe(false)
  })

  it('skips symlinks rather than following them', () => {
    const real = path.join(dir, 'real-abc123.js')
    writeFileSync(real, filler(4096))
    symlinkSync(real, path.join(dir, 'link-abc123.js'))
    compressDir(dir)
    expect(existsSync(path.join(dir, 'link-abc123.js.gz'))).toBe(false)
  })

  it('treats a missing directory as nothing to do', () => {
    // Library/test builds may not emit an assets dir at all.
    expect(compressDir(path.join(dir, 'does-not-exist')).files).toBe(0)
  })

  it('leaves no temp file behind (siblings are written via rename, not in place)', () => {
    // A plain writeFileSync to the final .gz/.br path would be visible to a
    // concurrent reader mid-write; the fix writes a temp sibling and renames
    // it into place, so nothing but the finished .gz/.br/.tmp-free files exist.
    writeFileSync(path.join(dir, 'index-abc123.js'), filler(4096))
    compressDir(dir)
    const leftover = readdirSync(dir).filter(name => name.includes('.tmp'))
    expect(leftover).toEqual([])
  })
})

describe('precompressPlugin', () => {
  it('is a build-only vite plugin', () => {
    const plugin = precompressPlugin()
    expect(plugin.name).toBe('kirocrew-precompress')
    expect(plugin.apply).toBe('build')
    expect(typeof plugin.closeBundle).toBe('function')
  })

  it('compresses the configured outDir/subdir relative to cwd', () => {
    const assets = path.join(dir, 'dist', 'assets')
    mkdirSync(assets, { recursive: true })
    writeFileSync(path.join(assets, 'index-abc123.js'), filler(4096))
    const cwd = process.cwd()
    try {
      process.chdir(dir)
      precompressPlugin({ log: false }).closeBundle()
    } finally {
      process.chdir(cwd)
    }
    expect(existsSync(path.join(assets, 'index-abc123.js.gz'))).toBe(true)
  })

  it('leaves stable-named files outside assets/ uncompressed', () => {
    // aiohttp does no staleness check, so a sibling for a stable-named file
    // (index.html, /vendor, /fonts) could be served stale indefinitely.
    const dist = path.join(dir, 'dist')
    mkdirSync(path.join(dist, 'assets'), { recursive: true })
    mkdirSync(path.join(dist, 'vendor'), { recursive: true })
    writeFileSync(path.join(dist, 'index.html'), filler(4096))
    writeFileSync(path.join(dist, 'vendor', 'tailwind.js'), filler(4096))
    const cwd = process.cwd()
    try {
      process.chdir(dir)
      precompressPlugin({ log: false }).closeBundle()
    } finally {
      process.chdir(cwd)
    }
    expect(existsSync(path.join(dist, 'index.html.gz'))).toBe(false)
    expect(existsSync(path.join(dist, 'vendor', 'tailwind.js.gz'))).toBe(false)
  })
})
