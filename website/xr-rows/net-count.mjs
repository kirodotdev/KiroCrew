// P20e, the cause of taps-f1 on the seventeenth's pack (the lead, 20:47): what the tab asks of 6777 in its first 25 s
// in the room, and what 6777 answers. Read only: it opens the room as the rows do (XR_DIST, the sample crew is NOT
// asked for: the real 6777 crew, as taps-f1 has it), presses nothing, and counts the responses by path and status.
// usage: XR_DIST=<dist> XR_DEV=gateway render-slot.sh node net-count.mjs <tag>
import { openRoom } from './chatcard-lib.mjs'

const tag = process.argv[2] ?? 'net'
const seen = new Map()
const got = await openRoom({ emulate: true })
const { page, browser } = got
const t0 = Date.now()
page.on('response', r => {
  const u = new URL(r.url())
  if (!u.pathname.startsWith('/api/') && !u.pathname.startsWith('/v1/')) return
  const key = `${r.request().method()} ${u.pathname.replace(/[0-9a-f]{8,}/g, '<id>')} ${r.status()}`
  seen.set(key, (seen.get(key) ?? 0) + 1)
})
page.on('requestfailed', r => {
  const u = new URL(r.url())
  const key = `${r.method()} ${u.pathname.replace(/[0-9a-f]{8,}/g, '<id>')} FAILED`
  seen.set(key, (seen.get(key) ?? 0) + 1)
})
await new Promise(done => setTimeout(done, 25000))
const rows = [...seen].sort((a, b) => b[1] - a[1])
console.log(`NET ${tag} ${Math.round((Date.now() - t0) / 1000)} s: ${rows.reduce((n, r) => n + r[1], 0)} responses`)
for (const [k, n] of rows) console.log(`  ${String(n).padStart(4)} ${k}`)
console.log(`PASS counted: ${rows.length} kinds`)
await browser.close()
process.exit(0)
