// P20e (D107 2): a third reading of the camera. A magenta ball (`RoomMark.tsx`, probe only) is set at known places in
// the room; for each head pose the row reads the build's projection of it (`__xrRoomMark.ndc`), its own projection
// (the same `onScreen` as `dash4-taken.mjs`), and where its pixels are in the still. The check: both projections lie
// within 3 px of the ball's pixel middle.
// usage: render-slot.sh node dash6-mark.mjs [out-prefix]
import { mkdirSync } from 'node:fs'
import { open, read, waitUntil } from './dash3d-lib.mjs'
import { AURA, EYE, enterAura, parkHands, emu } from './pages-lib.mjs'

const name = process.argv[2] ?? 'dash6-mark'
const OUT = process.env.HAND_OUT ?? (process.env.XR_UI ?? '..') + '/iterations/iter-15/dash6'
mkdirSync(OUT, { recursive: true })
let bad = 0
const check = (what, ok, got) => { if (!ok) bad += 1; console.log(`${ok ? 'PASS' : 'FAIL'} ${what}: ${JSON.stringify(got).slice(0, 900)}`) }
const DEG = 180 / Math.PI

const got = await open({ fixture: true, emulate: true, viewport: { width: AURA.width, height: AURA.height } })
const { page } = got
await waitUntil(async () => { const c = (await read(page)).crew; return c.length > 0 && c.every(b => b.drawn) }, 90000, 400)
await page.evaluate(() => { window.__xrPages.stroll?.(false) })
await enterAura(page)
await parkHands(page)
await emu.frames(page, 20)
await waitUntil(() => page.evaluate(() => !!window.__xrRoomMark), 20000, 200)

const head = { eye: [0, EYE, 0], yaw: 0, pitch: 0 }
const setHead = async (eye, yaw, pitch) => { head.eye = eye; head.yaw = yaw; head.pitch = pitch; await emu.setHead(page, { position: eye, yawDeg: -yaw, pitchDeg: pitch }); await emu.frames(page, 14) }
/** The row's camera, as in `dash4-taken.mjs`. */
const onScreen = p => {
  const y = head.yaw / DEG, t = head.pitch / DEG
  const d = [p[0] - head.eye[0], p[1] - head.eye[1], p[2] - head.eye[2]]
  const x1 = d[0] * Math.cos(y) + d[2] * Math.sin(y); const z1 = -d[0] * Math.sin(y) + d[2] * Math.cos(y)
  const y2 = d[1] * Math.cos(t) + z1 * Math.sin(t); const z2 = -d[1] * Math.sin(t) + z1 * Math.cos(t)
  const k = Math.tan(AURA.fovyDeg / 2 / DEG)
  if (z2 >= -0.01) return null
  return { x: AURA.width / 2 + (x1 / -z2 / (k * (AURA.width / AURA.height))) * (AURA.width / 2), y: AURA.height / 2 - (y2 / -z2 / k) * (AURA.height / 2) }
}
/** A place `dist` m from the eye, `dyaw` right and `dpitch` up of where the head looks. */
const placeAt = (dist, dyaw, dpitch) => {
  const y = (head.yaw + dyaw) / DEG, t = (head.pitch + dpitch) / DEG
  return [head.eye[0] + dist * Math.cos(t) * Math.sin(y), head.eye[1] + dist * Math.sin(t), head.eye[2] - dist * Math.cos(t) * Math.cos(y)]
}
/** The ball's pixels in a still: the middle of every pixel that is magenta. */
const ballPx = png => page.evaluate(async src => {
  const img = await new Promise((done, fail) => { const i = new Image(); i.onload = () => done(i); i.onerror = fail; i.src = `data:image/png;base64,${src}` })
  const c = document.createElement('canvas'); c.width = img.naturalWidth; c.height = img.naturalHeight
  const x = c.getContext('2d', { willReadFrequently: true }); x.drawImage(img, 0, 0)
  const d = x.getImageData(0, 0, c.width, c.height).data
  let n = 0, sx = 0, sy = 0
  for (let i = 0; i < d.length; i += 4) if (d[i] > 200 && d[i + 1] < 70 && d[i + 2] > 200) { const p = i / 4; n += 1; sx += p % c.width; sy += Math.floor(p / c.width) }
  return n ? { x: sx / n + 0.5, y: sy / n + 0.5, n } : null
}, png)

const poses = [
  { eye: [0, EYE, 0], yaw: 0, pitch: 0 },
  { eye: [0, EYE, 0], yaw: 0, pitch: -18.5 },
  { eye: [0.4, EYE, 0], yaw: 25, pitch: -10 },
  { eye: [-0.3, EYE, -0.45], yaw: -35, pitch: 8 },
]
const offsets = [[0, 0], [18, 9], [-22, -12], [12, -16], [-25, 14]]
const rows = []
for (const [i, p] of poses.entries()) {
  await setHead(p.eye, p.yaw, p.pitch)
  for (const [j, [dy, dp]] of offsets.entries()) {
    const at = placeAt(0.9, dy, dp)
    await page.evaluate(a => { window.__xrRoomMark.at = a }, at)
    await emu.frames(page, 4)
    const ndc = await page.evaluate(() => window.__xrRoomMark.ndc)
    const its = ndc ? { x: ((ndc[0] + 1) / 2) * AURA.width, y: ((1 - ndc[1]) / 2) * AURA.height } : null
    const mine = onScreen(at)
    const tag = `p${i}-o${j}`
    const png = (await page.screenshot({ path: `${OUT}/${name}-${tag}.png` })).toString('base64')
    const seen = await ballPx(png)
    const off = (a, b) => (a && b ? +Math.hypot(a.x - b.x, a.y - b.y).toFixed(2) : null)
    rows.push({ tag, pose: p, at: at.map(v => +v.toFixed(3)), seen, its, mine, buildOff: off(its, seen), rowOff: off(mine, seen) })
    check(`pose ${i} (yaw ${p.yaw}, pitch ${p.pitch}), mark ${dy} right ${dp} up at 0.9 m: the build's and the row's projections each within 3 px of the ball's pixels`, !!seen && off(its, seen) <= 3 && off(mine, seen) <= 3, { seen, buildOff: off(its, seen), rowOff: off(mine, seen) })
  }
}
await page.evaluate(() => { window.__xrRoomMark.at = null })
console.log('RESULTS', JSON.stringify(rows))
console.log(`TOTAL bad ${bad}`)
await got.browser.close()
process.exit(bad ? 1 : 0)
