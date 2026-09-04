/**
 * Generator for `crew-ghost-sprite.png` — the baked Kiro-ghost mascot sheet that
 * `CrewGhost.tsx` imports.
 *
 * Why this exists: `website/AUTOSDE.yaml`'s blocking `use-lucide-icons` rule
 * grants a mascot a BRAND-MARK EXCEPTION only when the art is "asset, not code",
 * so the drawing program cannot live in the component. It lives here instead, is
 * run by hand, and its output is committed beside the component. Regenerate with
 * (Node 18+ / the repo's Playwright image, from `website/`):
 *
 *   node src/apps/issue-radar/components/crew-ghost-sprite.gen.mjs
 *
 * Fidelity: the rects below are a VERBATIM transcription of the `drawCrewGhost`
 * program this file replaced (itself a port of `drawGhost` in
 * `pages/scenes/GhostScene.tsx`), and the body bitmap is PARSED out of
 * `src/hooks/sceneText.ts` rather than re-typed, so the shared 24×28 art cannot
 * drift from the scene's. Rasterisation is done by real Chromium — the same
 * engine that painted the canvas before — so the accessories' fractional rects
 * get the browser's own antialiasing rather than a reimplementation of it.
 *
 * Sheet layout: 8 outfit columns × 2 rows (blush off, blush on), each cell one
 * sprite frame plus a 1-sprite-pixel transparent gutter on its right/bottom. The
 * gutter is what makes the crop safe on a fractional device-pixel ratio, where a
 * frame edge can otherwise oversample its neighbour. Baked at 4× so the largest
 * caller (size 78 → 2 CSS px per sprite px, doubled again on a retina display)
 * still shows a whole source pixel per device pixel; every display ratio in use
 * is an integer divisor of 4, which keeps the body bitmap pixel-exact.
 */
import { chromium } from 'playwright'
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const OUT = resolve(HERE, 'crew-ghost-sprite.png')
const SCENE_TEXT = resolve(HERE, '../../../hooks/sceneText.ts')

/* ── Geometry — must stay in step with CREW_GHOST_FRAME / CREW_GHOST_SHEET ── */
const BITMAP_W = 24
const BITMAP_H = 28
const PAD_TOP = 8
const PAD_LEFT = 4
const PAD_RIGHT = 1
const PAD_BOTTOM = 1
const FRAME_W = PAD_LEFT + BITMAP_W + PAD_RIGHT // 29
const FRAME_H = PAD_TOP + BITMAP_H + PAD_BOTTOM // 37
const GUTTER = 1
const CELL_W = FRAME_W + GUTTER // 30
const CELL_H = FRAME_H + GUTTER // 38
const SCALE = 4

const GHOST_COLOR = '#e8ecf4'
const EYE_COLOR = '#14141e'
const EYE_SHIFT = 0.5

/** The scene's eight looks, in the scene's order. */
const OUTFITS = [
  { hat: 'none', glasses: 'round', cape: false, capeColor: '' },
  { hat: 'witch', glasses: 'none', cape: false, capeColor: '' },
  { hat: 'none', glasses: 'none', cape: true, capeColor: '#c0392b' },
  { hat: 'top', glasses: 'none', cape: false, capeColor: '' },
  { hat: 'none', glasses: 'shades', cape: true, capeColor: '#27408b' },
  { hat: 'beanie', glasses: 'none', cape: false, capeColor: '' },
  { hat: 'party', glasses: 'round', cape: false, capeColor: '' },
  { hat: 'crown', glasses: 'none', cape: true, capeColor: '#5b2c6f' },
]

/** Pull the shared body bitmap out of `sceneText.ts` instead of re-typing it. */
function readGhostPixels() {
  const src = readFileSync(SCENE_TEXT, 'utf8')
  const block = /export const KIRO_GHOST_PIXELS = \[([\s\S]*?)\]/.exec(src)
  if (!block) throw new Error(`KIRO_GHOST_PIXELS not found in ${SCENE_TEXT}`)
  const rows = [...block[1].matchAll(/'([.#]+)'/g)].map(m => m[1])
  if (rows.length !== BITMAP_H) throw new Error(`expected ${BITMAP_H} bitmap rows, got ${rows.length}`)
  for (const r of rows) if (r.length !== BITMAP_W) throw new Error(`bitmap row is ${r.length} wide, expected ${BITMAP_W}`)
  return rows
}

/**
 * VERBATIM transcription of the component's former `drawCrewGhost`. Body
 * top-left at (gx, gy) in sprite pixels; the caller has already scaled the
 * context, so every number matches GhostScene's one-for-one.
 */
const DRAW_PROGRAM = /* js */ `
function drawCrewGhost(X, gx, gy, o, blush, pixels, ghostColor, eyeColor, eyeShift) {
  const d = (x, y, w, h, c) => { X.fillStyle = c; X.fillRect(x, y, w, h) }
  const EYE_SHIFT = eyeShift

  if (o.cape) {
    const back = gx - 3
    d(back, gy + 6, 4, 14, o.capeColor)
    d(back - 1, gy + 9, 2, 9, o.capeColor)
    d(gx + 4, gy + 6, 17, 1.8, o.capeColor)
  }

  X.fillStyle = ghostColor
  pixels.forEach((row, ry) => {
    let run = -1
    for (let cx = 0; cx <= row.length; cx++) {
      const on = cx < row.length && row[cx] === '#'
      if (on && run < 0) run = cx
      else if (!on && run >= 0) {
        X.fillRect(gx + run, gy + ry, cx - run, 1)
        run = -1
      }
    }
  })

  d(gx + 11.5 + EYE_SHIFT, gy + 7, 2, 1, eyeColor)
  d(gx + 11 + EYE_SHIFT, gy + 8, 3, 3, eyeColor)
  d(gx + 11.5 + EYE_SHIFT, gy + 11, 2, 1, eyeColor)
  d(gx + 17.5 + EYE_SHIFT, gy + 7, 2, 1, eyeColor)
  d(gx + 17 + EYE_SHIFT, gy + 8, 3, 3, eyeColor)
  d(gx + 17.5 + EYE_SHIFT, gy + 11, 2, 1, eyeColor)

  if (o.glasses === 'round') {
    d(gx + 10 + EYE_SHIFT, gy + 6.2, 5, 0.9, '#3a3a4a')
    d(gx + 16 + EYE_SHIFT, gy + 6.2, 5, 0.9, '#3a3a4a')
    d(gx + 10 + EYE_SHIFT, gy + 6.2, 0.9, 6.5, '#3a3a4a')
    d(gx + 14.1 + EYE_SHIFT, gy + 6.2, 0.9, 6.5, '#3a3a4a')
    d(gx + 20.1 + EYE_SHIFT, gy + 6.2, 0.9, 6.5, '#3a3a4a')
    d(gx + 10 + EYE_SHIFT, gy + 11.8, 5, 0.9, '#3a3a4a')
    d(gx + 16 + EYE_SHIFT, gy + 11.8, 5, 0.9, '#3a3a4a')
    d(gx + 14.9 + EYE_SHIFT, gy + 7.5, 1.2, 0.9, '#3a3a4a')
  } else if (o.glasses === 'shades') {
    d(gx + 10 + EYE_SHIFT, gy + 6.8, 4.6, 4.4, '#111')
    d(gx + 15.6 + EYE_SHIFT, gy + 6.8, 4.6, 4.4, '#111')
    d(gx + 14.4 + EYE_SHIFT, gy + 7.4, 1.4, 1, '#111')
    d(gx + 10.8 + EYE_SHIFT, gy + 7.5, 1.2, 0.9, '#8fa8ff')
    d(gx + 16.4 + EYE_SHIFT, gy + 7.5, 1.2, 0.9, '#8fa8ff')
  }

  if (o.hat === 'witch') {
    d(gx + 6, gy - 1, 16, 1.8, '#2d1b4e')
    d(gx + 10, gy - 4.5, 7, 3.5, '#2d1b4e')
    d(gx + 11.8, gy - 7.5, 3.4, 3.4, '#2d1b4e')
    d(gx + 10, gy - 2, 7, 1.1, '#8e44ad')
  } else if (o.hat === 'top') {
    d(gx + 7, gy - 1, 13, 1.4, '#181820')
    d(gx + 9, gy - 7, 9, 6, '#181820')
    d(gx + 9, gy - 2.2, 9, 1.2, '#b03a2e')
  } else if (o.hat === 'party') {
    d(gx + 11, gy - 2.4, 4.6, 2.4, '#f39c12')
    d(gx + 12, gy - 4.8, 2.7, 2.4, '#e74c3c')
    d(gx + 12.7, gy - 6.4, 1.2, 1.6, '#f1c40f')
  } else if (o.hat === 'beanie') {
    d(gx + 8, gy - 1.2, 11, 2.8, '#16a085')
    d(gx + 8, gy + 0.6, 11, 1, '#0e6655')
    d(gx + 12.7, gy - 2.8, 1.8, 1.8, '#f4d03f')
  } else if (o.hat === 'crown') {
    d(gx + 9.5, gy - 2.4, 7.5, 2.4, '#f1c40f')
    d(gx + 9.5, gy - 4, 1.7, 1.9, '#f1c40f')
    d(gx + 12.4, gy - 4, 1.7, 1.9, '#f1c40f')
    d(gx + 15.3, gy - 4, 1.7, 1.9, '#f1c40f')
    d(gx + 12.8, gy - 1.6, 1.1, 1.1, '#e74c3c')
  }

  if (blush) {
    X.globalAlpha = 0.35
    d(gx + 8 + EYE_SHIFT, gy + 12.5, 2, 1.1, '#ff8899')
    d(gx + 21 + EYE_SHIFT, gy + 12.5, 2, 1.1, '#ff8899')
    X.globalAlpha = 1
  }
}
`

const pixels = readGhostPixels()

const browser = await chromium.launch()
const page = await browser.newPage()
const { dataUrl, problems, report } = await page.evaluate(
  ({ program, geo, outfits, pixels }) => {
    // `program` is the drawing source built as a template literal in THIS file,
    // handed to the page so the browser context can call `drawCrewGhost`.
    // Nothing here is remote or user-supplied.
    //
    // `eslint.config.js` turns `no-eval` on for `src/**/*.mjs` precisely so this
    // exemption is a reviewed one: the directive below is live, and a SECOND
    // `eval()` in this file would be an error rather than a silent pass.
    // eslint-disable-next-line no-eval -- `program` is a template literal built in this file; nothing remote or user-supplied reaches it
    eval(program)
    const canvas = document.createElement('canvas')
    canvas.width = geo.CELL_W * outfits.length * geo.SCALE
    canvas.height = geo.CELL_H * 2 * geo.SCALE
    const ctx = canvas.getContext('2d')
    ctx.imageSmoothingEnabled = false
    ctx.setTransform(geo.SCALE, 0, 0, geo.SCALE, 0, 0)
    for (let row = 0; row < 2; row++) {
      for (let col = 0; col < outfits.length; col++) {
        drawCrewGhost(
          ctx,
          col * geo.CELL_W + geo.PAD_LEFT,
          row * geo.CELL_H + geo.PAD_TOP,
          outfits[col],
          row === 1,
          pixels,
          geo.GHOST_COLOR,
          geo.EYE_COLOR,
          geo.EYE_SHIFT,
        )
      }
    }
    // Bake-time invariants. The frame's padding exists because hats are drawn
    // ABOVE the 24×28 body and the cape to its LEFT, at negative offsets from
    // the body origin: too little headroom clips the witch cone, too little left
    // padding flattens the cape onto the frame edge. Checking the RASTER (not
    // the draw program's numbers) also proves the gutter is untouched, which is
    // what makes the component's crop safe. Asserted here rather than in a unit
    // test because it is a property of the asset, not of the render path.
    const sheet = ctx.getImageData(0, 0, canvas.width, canvas.height)
    const at = (x, y) => sheet.data[(y * canvas.width + x) * 4 + 3] !== 0
    const problems = []
    const report = []
    for (let row = 0; row < 2; row++) {
      for (let col = 0; col < outfits.length; col++) {
        const ox = col * geo.CELL_W * geo.SCALE
        const oy = row * geo.CELL_H * geo.SCALE
        // Gutter (the cell's right and bottom strip) must be fully transparent.
        for (let y = 0; y < geo.CELL_H * geo.SCALE; y++)
          for (let x = geo.FRAME_W * geo.SCALE; x < geo.CELL_W * geo.SCALE; x++)
            if (at(ox + x, oy + y)) problems.push(`r${row}c${col}: art in right gutter at ${x},${y}`)
        for (let y = geo.FRAME_H * geo.SCALE; y < geo.CELL_H * geo.SCALE; y++)
          for (let x = 0; x < geo.CELL_W * geo.SCALE; x++)
            if (at(ox + x, oy + y)) problems.push(`r${row}c${col}: art in bottom gutter at ${x},${y}`)
        // …and the frame must actually be USED, or the padding is just waste.
        let minX = Infinity, minY = Infinity, maxX = -1, maxY = -1, lit = 0
        for (let y = 0; y < geo.FRAME_H * geo.SCALE; y++)
          for (let x = 0; x < geo.FRAME_W * geo.SCALE; x++)
            if (at(ox + x, oy + y)) {
              lit++
              if (x < minX) minX = x
              if (y < minY) minY = y
              if (x > maxX) maxX = x
              if (y > maxY) maxY = y
            }
        if (lit < 200) problems.push(`r${row}c${col}: only ${lit} lit pixels — frame looks empty`)
        report.push({ row, col, lit, box: [minX, minY, maxX, maxY] })
      }
    }
    return { dataUrl: canvas.toDataURL('image/png'), problems, report }
  },
  {
    program: DRAW_PROGRAM,
    geo: { CELL_W, CELL_H, FRAME_W, FRAME_H, SCALE, PAD_LEFT, PAD_TOP, GHOST_COLOR, EYE_COLOR, EYE_SHIFT },
    outfits: OUTFITS,
    pixels,
  },
)
await browser.close()

for (const r of report) {
  const [minX, minY, maxX, maxY] = r.box
  console.log(
    `  row ${r.row} col ${r.col}: ${String(r.lit).padStart(5)} px, ` +
      `bbox ${minX},${minY}..${maxX},${maxY} (frame ${FRAME_W * SCALE}×${FRAME_H * SCALE})`,
  )
}
if (problems.length) {
  for (const p of problems) console.error(`FAIL ${p}`)
  process.exit(1)
}
console.log(`OK ${report.length} frames: no art in any gutter, every frame populated`)

writeFileSync(OUT, Buffer.from(dataUrl.split(',')[1], 'base64'))
console.log(
  `wrote ${OUT} — ${CELL_W * OUTFITS.length * SCALE}×${CELL_H * 2 * SCALE}px ` +
    `(${OUTFITS.length} outfits × 2 blush states, frame ${FRAME_W}×${FRAME_H} @${SCALE}×)`,
)
