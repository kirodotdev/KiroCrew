import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

// The manifest is copied verbatim from public/, so Vite's `base` never reaches it.
// Behind a reverse proxy that serves the dashboard under a sub-path
// (`KIROCREW_BASE_PATH=/kirocrew`), a root-absolute `start_url` or icon `src` points
// outside the dashboard: the icons 404 and an installed app opens whatever is at `/`.
// Relative URLs resolve against the manifest's own location, so they follow the base.
type Manifest = {
  start_url: string
  icons: { src: string }[]
  screenshots?: { src: string }[]
}

const manifest = async (): Promise<Manifest> =>
  JSON.parse(await readFile(join(__dirname, '..', '..', 'public', 'manifest.json'), 'utf8'))

const urls = (m: Manifest): string[] => [
  m.start_url,
  ...m.icons.map((i) => i.src),
  ...(m.screenshots ?? []).map((s) => s.src),
]

describe('PWA manifest URLs', () => {
  it('resolve inside the dashboard when it is served under a sub-path', async () => {
    const at = 'https://host.example/kirocrew/manifest.json'
    for (const url of urls(await manifest())) {
      expect(new URL(url, at).pathname, url).toMatch(/^\/kirocrew\//)
    }
  })

  it('still resolve to the site root when the dashboard is served at /', async () => {
    const m = await manifest()
    const at = 'https://host.example/manifest.json'
    expect(new URL(m.start_url, at).pathname).toBe('/')
    expect(m.icons.map((i) => new URL(i.src, at).pathname)).toEqual(['/icon-192.png', '/icon-512.png'])
  })
})
