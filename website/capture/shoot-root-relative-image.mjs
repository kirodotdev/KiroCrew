/**
 * Screenshot harness for root-relative images in a previewed markdown file
 * (issue #23623).
 *
 * Builds a throwaway repository on disk — `articles/post.md` plus
 * `images/post/architecture.png` at the repository root, the Zenn/GitHub
 * layout — then serves capture/root-relative-image.html from a programmatic
 * Vite dev server on loopback. Every /api/file-raw request is answered from
 * the real filesystem: an existing file is served, anything else is a 404,
 * the same contract as the backend endpoint. Each requested path is printed,
 * so the log shows the order the candidates were tried in.
 *
 * Usage (from website/): node capture/shoot-root-relative-image.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const OUT = process.argv[2] || resolve(here, '../../temp-screenshots/root-relative-image')
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const repo = mkdtempSync(join(tmpdir(), 'root-relative-image-'))
mkdirSync(join(repo, 'articles'), { recursive: true })
mkdirSync(join(repo, 'images/post'), { recursive: true })
writeFileSync(join(repo, 'articles/post.md'), '![Architecture diagram](/images/post/architecture.png)\n')
copyFileSync(resolve(here, '../public/icon-512.png'), join(repo, 'images/post/architecture.png'))
const file = join(repo, 'articles/post.md')

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})
const page = await browser.newPage({ viewport: { width: 760, height: 900 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
await page.route('**/api/file-raw*', route => {
  const path = new URL(route.request().url()).searchParams.get('path') || ''
  const hit = existsSync(path) && statSync(path).isFile()
  console.log(`${hit ? '200' : '404'} ${path}`)
  return hit
    ? route.fulfill({ status: 200, contentType: 'image/png', body: readFileSync(path) })
    : route.fulfill({ status: 404, contentType: 'application/json', body: '{"error":"not found"}' })
})

for (const theme of ['dark', 'light']) {
  await page.goto(`${base}/capture/root-relative-image.html?theme=${theme}&file=${encodeURIComponent(file)}`)
  // Settled when the site-root image has loaded (or been replaced by the chip)
  // and the missing one shows its chip.
  await page.waitForFunction(() => {
    const imgs = [...document.querySelectorAll('img')]
    const pending = imgs.some(i => !i.complete)
    return !pending && document.body.textContent?.includes('Screenshot that was never committed')
      && document.body.textContent?.match(/Image (file no longer exists|failed to load)/g)
  })
  const shot = join(OUT, `${PREFIX}-${theme}.png`)
  await page.locator('#root > div').screenshot({ path: shot })
  console.log('wrote', shot)
}

await browser.close()
await server.close()
