// Renders compare.html (which references before.png / after.png in this dir)
// into a single comparison PNG. Usage: node compose.mjs <dir>
import { chromium } from 'playwright'
import { pathToFileURL } from 'node:url'
import { join, resolve } from 'node:path'

const dir = resolve(process.argv[2] ?? '.')
const b = await chromium.launch()
const p = await b.newPage({ viewport: { width: 900, height: 1400 }, deviceScaleFactor: 2 })
await p.goto(pathToFileURL(join(dir, 'compare.html')).href, { waitUntil: 'load' })
await p.waitForFunction(() => [...document.images].every(i => i.complete && i.naturalWidth > 0))
const el = await p.$('body')
await el.screenshot({ path: join(dir, 'mobile-nav-drawer-top-inset.png') })
await b.close()
