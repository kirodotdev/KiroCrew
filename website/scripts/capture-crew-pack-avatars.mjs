/**
 * Screenshots of the appearance-pack avatar tier (capture/crew-pack-avatars.html).
 *
 * The pack library is a NETWORK surface, so this harness answers it at the
 * network rather than stubbing a module: `/api/appearances` returns a fixture
 * listing, `/api/appearances/{id}` returns the pack a renderer reads to choose
 * its player, and `/api/appearances/{id}/slot/{slot}` returns real bytes under
 * the same content type the gateway serves. The cards' thumbnails and the crew's
 * face are therefore fetched exactly as they are in the product, and a URL this
 * change got wrong would photograph as a broken frame instead of passing.
 *
 * The lottie and sprite art is READ FROM THE SHIPPED FIXTURE BUNDLES
 * (`src/test/fixtures/appearance-packs/`), the same files a reviewer imports by
 * hand from the Library tab. So a frame here cannot show art no real import
 * could produce.
 *
 * Self-checking before every frame: the Library grid must carry every fixture
 * card, each format must reach the player that can draw it, and the pane must
 * not be rendering raw catalog keys. A screenshot of the wrong state is worse
 * evidence than none.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6832 --strictPort    # in another shell
 *   node scripts/capture-crew-pack-avatars.mjs http://127.0.0.1:6832 ../temp-screenshots/crew-pack-avatars
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const BASE = process.argv[2] || 'http://127.0.0.1:6832'
const OUT = process.argv[3] || '../temp-screenshots/crew-pack-avatars'
mkdirSync(OUT, { recursive: true })

const HERE = dirname(fileURLToPath(import.meta.url))

/** The shipped sample bundles, read rather than re-invented: this harness must
 *  photograph art a real import could produce. */
const bundle = (name) =>
  JSON.parse(readFileSync(join(HERE, '..', 'src/test/fixtures/appearance-packs', `${name}.json`), 'utf-8'))
const LOTTIE_BUNDLE = bundle('sample-lottie')
const SPRITE_BUNDLE = bundle('sample-sprite')

/** The fixture library: the built-in ghost, and one custom pack per format — so
 *  every card in the grid is a format a crew can now wear. */
const PACKS = [
  {
    id: 'kiro-ghost',
    name: 'Kiro',
    author: 'Kiro Crew',
    description: 'The default companion.',
    type: 'builtin',
    format: 'svg',
    recoloured: false,
  },
  {
    id: 'aurora',
    name: 'Aurora',
    author: 'zoe',
    description: 'a paper fox',
    type: 'custom',
    format: 'svg',
    recoloured: false,
  },
  {
    id: 'nebula',
    name: 'Nebula',
    author: 'wren',
    description: 'a drifting jellyfish',
    type: 'custom',
    format: 'lottie',
    recoloured: false,
  },
  {
    id: 'pixels',
    name: 'Pixels',
    author: 'wren',
    description: 'a pixel robot',
    type: 'custom',
    format: 'sprite',
    recoloured: false,
  },
]

const SLOT_ART = {
  idle: {
    paint: ['#f4a259', '#8c4a1f'],
    body: '<path d="M22 74 L50 22 L78 74 Z" fill="F"/><circle cx="40" cy="56" r="5" fill="B"/><circle cx="60" cy="56" r="5" fill="B"/><path d="M40 67 Q50 73 60 67" stroke="B" stroke-width="4" fill="none" stroke-linecap="round"/>',
  },
  working: {
    paint: ['#5fb0d8', '#1f4a6b'],
    body: '<circle cx="50" cy="50" r="27" fill="F"/><rect x="35" y="45" width="10" height="4" rx="2" fill="B"/><rect x="55" y="45" width="10" height="4" rx="2" fill="B"/><circle cx="50" cy="63" r="4" fill="B"/><path d="M50 12 L50 23" stroke="F" stroke-width="4" stroke-linecap="round"/><circle cx="50" cy="9" r="4" fill="F"/>',
  },
  done: {
    paint: ['#63c98a', '#1f5c38'],
    body: '<rect x="24" y="24" width="52" height="52" rx="14" fill="F"/><path d="M35 52 L46 63 L67 40" stroke="B" stroke-width="7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
  },
  error: {
    paint: ['#e2686d', '#6b1f24'],
    body: '<path d="M50 20 L80 74 L20 74 Z" fill="F"/><rect x="46" y="38" width="8" height="20" rx="4" fill="B"/><circle cx="50" cy="65" r="4.5" fill="B"/>',
  },
}

const slotSvg = (slot) => {
  const art = SLOT_ART[slot] || SLOT_ART.idle
  const body = art.body.replaceAll('"F"', `"${art.paint[0]}"`).replaceAll('"B"', `"${art.paint[1]}"`)
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">
  <rect width="100" height="100" rx="22" fill="${art.paint[1]}"/>
  ${body}
</svg>`
}

/**
 * What `GET /api/appearances/{id}` answers per pack — the read a renderer makes
 * to learn each slot's FORMAT before it can pick a player.
 *
 * The lottie and sprite entries carry the fixture bundles' own art, so a frame
 * shows the same drawing an imported sample does. `aurora` stays svg, whose
 * `content` is never drawn from here (the renderer uses the slot route, which
 * carries the inert-SVG content policy the detail body does not).
 */
const DETAIL = {
  aurora: {
    animations: Object.fromEntries(
      ['idle', 'working', 'done', 'error'].map(slot => [slot, { content: slotSvg(slot), format: 'svg' }]),
    ),
  },
  nebula: {
    animations: {
      idle: { content: LOTTIE_BUNDLE.files['idle.json'], format: 'lottie' },
      working: { content: LOTTIE_BUNDLE.files['working.json'], format: 'lottie' },
      done: { content: LOTTIE_BUNDLE.files['done.json'], format: 'lottie' },
    },
  },
  pixels: {
    animations: {
      idle: { content: SPRITE_BUNDLE.files['sheet.png'], format: 'sprite' },
      working: { content: SPRITE_BUNDLE.files['sheet.png'], format: 'sprite' },
    },
    sprite: SPRITE_BUNDLE.manifest.sprite,
  },
}

/**
 * One slot's art. Each slot gets its own SHAPE as well as its own palette: four
 * tints of one drawing photograph as "the same picture four times", which is
 * indistinguishable from every state having silently resolved back to idle.
 */
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 900, height: 940 }, deviceScaleFactor: 2 })

/** The listing the next frame should see. Mutable so one route can serve both
 *  the populated grid and the first-run empty state. */
let listing = PACKS
await page.route('**/api/appearances', route =>
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ packs: listing }) }),
)

/** How the next DELETE should answer. `409` is the in-use refusal, whose body
 *  names the crews wearing the pack — the message this frame is evidence for. */
let deleteAnswer = 'ok'
await page.route('**/api/appearances/*', async route => {
  const url = new URL(route.request().url())
  const [, , , id] = url.pathname.split('/')
  if (route.request().method() === 'GET') {
    // The pack a renderer reads to choose its player. A pack with no entry stands
    // in for one the user removed, and must fall back to the crew's own face.
    const detail = DETAIL[id]
    if (!detail) {
      return route.fulfill({ status: 404, contentType: 'application/json', body: '{"code":"pack_not_found"}' })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(detail) })
  }
  if (route.request().method() !== 'DELETE') return route.fallback()
  if (deleteAnswer === 'in-use') {
    return route.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'that pack is still worn by a crew', code: 'pack_in_use', crews: ['aurora-crew', 'radar'] }),
    })
  }
  return route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
})
await page.route('**/api/appearances/*/slot/*', route => {
  const url = new URL(route.request().url())
  // /api/appearances/{id}/slot/{slot} → ['', 'api', 'appearances', id, 'slot', slot]
  const [, , , id, , slot] = url.pathname.split('/')
  // The built-in pack's art ships in the bundle; the gateway answers 404
  // `builtin_no_content` for it, and `deleted-pack` stands in for a pack the
  // user removed. Both must fall back to the crew's own face.
  if (id === 'kiro-ghost' || id === 'deleted-pack') {
    return route.fulfill({ status: 404, contentType: 'application/json', body: '{"code":"pack_not_found"}' })
  }
  // A sprite sheet is served base64-DECODED as a real PNG, the way the gateway
  // does it: this route exists to be an <img src>, and base64 text under an image
  // content type is not an image.
  if (DETAIL[id]?.animations[slot]?.format === 'sprite') {
    return route.fulfill({
      status: 200,
      contentType: 'image/png',
      headers: { 'X-Resolved-Slot': slot, 'X-Content-Type-Options': 'nosniff' },
      body: Buffer.from(DETAIL[id].animations[slot].content, 'base64'),
    })
  }
  return route.fulfill({
    status: 200,
    contentType: 'image/svg+xml',
    headers: { 'X-Resolved-Slot': slot, 'X-Content-Type-Options': 'nosniff' },
    body: slotSvg(slot),
  })
})

/** Open the builder's Library tab and assert the grid before photographing it. */
async function openLibrary() {
  await page.getByRole('button', { name: 'Library' }).click()
  await page.getByTestId('avatar-library-pane').waitFor()
  for (const pack of PACKS) await page.getByTestId(`avatar-pack-card-${pack.id}`).waitFor()
  await page.getByTestId('avatar-pack-thumb-aurora').waitFor()
  // i18next returns the key itself for a missing key, so a pane full of
  // `components.avatarBuilder.*` renders as a plausible UI and photographs as
  // one. Assert the copy, not just the structure.
  const paneText = await page.getByTestId('avatar-library-pane').innerText()
  if (paneText.includes('components.avatarBuilder')) {
    throw new Error(`raw catalog keys are rendering instead of copy:\n${paneText}`)
  }
  for (const label of ['Aurora', 'Nebula', 'Pixels', 'Built-in', 'Custom', 'Import pack (.json)']) {
    if (!paneText.includes(label)) throw new Error(`the pane is missing its "${label}" label`)
  }
  // Every format the library holds is wearable now, so no card may be inert.
  for (const id of ['aurora', 'nebula', 'pixels']) {
    if (!(await page.getByTestId(`avatar-pack-select-${id}`).isEnabled())) {
      throw new Error(`the ${id} card is unselectable — a crew can wear every format`)
    }
  }
  // A non-image pack's thumbnail must come from the renderer, not from an <img>
  // pointed at JSON or at a whole sheet.
  // `.first()`: a scene may list two lottie packs (the broken-art frame adds one
  // whose box exists for a moment before its renderer refuses the clip).
  await page.getByTestId('pack-avatar-lottie').first().waitFor()
  await page.getByTestId('pack-avatar-sprite').first().waitFor()
}

for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/crew-pack-avatars.html?scene=library&theme=${theme}`)
  await openLibrary()
  await page.screenshot({ path: join(OUT, `library-${theme}.png`) })
  console.log(`captured library-${theme}.png`)
}

// A pack whose art cannot be drawn. `glitch` installs like any lottie pack, but
// its clip is `{}` — valid JSON, not a Lottie document — so the renderer refuses
// it and the card shows the placeholder with its caption instead of a tile.
// The card must stay selectable: the crew editor's own warning names the pack.
listing = [
  ...PACKS,
  { ...PACKS[2], id: 'glitch', name: 'Glitch', author: 'rue', description: 'a clip that is not one' },
]
DETAIL.glitch = { animations: { idle: { content: '{}', format: 'lottie' } } }
await page.goto(`${BASE}/capture/crew-pack-avatars.html?scene=library&theme=dark`)
await openLibrary()
const brokenTile = page.getByTestId('avatar-pack-noart-glitch')
await brokenTile.waitFor()
const brokenText = await brokenTile.innerText()
if (!brokenText.includes("Art won't draw. Use “Import pack”.")) {
  throw new Error(`the broken-art tile carries no caption: ${JSON.stringify(brokenText)}`)
}
if (!(await page.getByTestId('avatar-pack-select-glitch').isEnabled())) {
  throw new Error('the broken-art card is unselectable — the editor warning is what names the pack')
}
await page.screenshot({ path: join(OUT, 'library-broken-art-dark.png') })
console.log('captured library-broken-art-dark.png')
delete DETAIL.glitch
listing = PACKS

// The Expressions tab for a pack crew: the Sounds half stays, the eyes/mouth
// pickers are gone, and the note says why.
await page.goto(`${BASE}/capture/crew-pack-avatars.html?scene=library&theme=dark`)
await page.getByRole('button', { name: 'Expressions' }).click()
await page.getByTestId('avatar-expressions-pane').waitFor()
await page.getByTestId('avatar-expressions-pack-note').waitFor()
await page.getByTestId('avatar-state-sound-done').waitFor()
if (await page.getByTestId('avatar-expr-done-eyes').count()) {
  throw new Error('the eyes picker is rendering for a pack — a pack has no face to repaint')
}
await page.screenshot({ path: join(OUT, 'expressions-pack-dark.png') })
console.log('captured expressions-pack-dark.png')

// The faces scene: one served frame per state, plus the two local fallbacks.
for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/crew-pack-avatars.html?scene=faces&theme=${theme}`)
  await page.waitForFunction(() => document.body.innerText.includes('A crew wearing a pack'))
  // Four served frames must be four DIFFERENT drawings: identical art would
  // photograph as correct while every state silently resolved to idle.
  // Wait for the pack READ to land first: until it does there is nothing to
  // choose a player from, so the boxes hold their space and carry no art.
  await page.waitForFunction(
    () => document.querySelectorAll('[data-testid="pack-avatar-pending"]').length === 0,
  )
  await page.waitForFunction(
    () => document.querySelectorAll('img[src*="/api/appearances/aurora/slot/"]').length === 4,
  )
  const slots = await page.$$eval('img[src*="/api/appearances/aurora/slot/"]', imgs =>
    imgs.map(i => (i.getAttribute('src') || '').split('/slot/')[1]),
  )
  const expected = ['idle', 'working', 'done', 'error']
  if (slots.join(',') !== expected.join(',')) {
    throw new Error(`states asked for [${slots}] rather than [${expected}]`)
  }
  // Both fallbacks must have landed on a locally composed ghost.
  await page.waitForFunction(
    () => document.querySelectorAll('img[src^="data:image/svg+xml"]').length >= 2,
  )
  await page.locator('#root').screenshot({ path: join(OUT, `faces-${theme}.png`) })
  console.log(`captured faces-${theme}.png`)
}

// Every format worn on a crew: an <img> for the svg, a real lottie-web instance
// for the lottie document, and a canvas stepping the sprite row this slot is
// assigned to. One frame per theme, plus the roster-size row at the bottom.
for (const theme of ['dark', 'light']) {
  await page.goto(`${BASE}/capture/crew-pack-avatars.html?scene=formats&theme=${theme}`)
  await page.waitForFunction(() => document.body.innerText.includes('Every format a crew can wear'))
  // Every box must have resolved to a player: a pending box photographs as an
  // empty tile and would read as the feature not working.
  await page.waitForFunction(
    () => document.querySelectorAll('[data-testid="pack-avatar-pending"]').length === 0,
  )
  // One box per format per state, plus the 38px roster row, and the sprite canvas
  // must have drawn: an all-transparent canvas photographs as a blank tile.
  const tiers = await page.evaluate(() => ({
    svg: document.querySelectorAll('[data-testid="pack-avatar-svg"]').length,
    lottie: document.querySelectorAll('[data-testid="pack-avatar-lottie"]').length,
    sprite: document.querySelectorAll('[data-testid="pack-avatar-sprite"]').length,
    lottieDocs: document.querySelectorAll('[data-testid="pack-avatar-lottie"] svg').length,
  }))
  if (tiers.svg !== 3 || tiers.lottie !== 3 || tiers.sprite !== 3) {
    throw new Error(`format tiers did not all render: ${JSON.stringify(tiers)}`)
  }
  if (tiers.lottieDocs < 3) {
    throw new Error(`lottie-web drew no document in ${3 - tiers.lottieDocs} box(es)`)
  }
  // WAITED for, not asserted once: a sheet is decoded through an Image() and the
  // first frame is drawn on its `load`, which lands after the pack read that the
  // pending-box wait above proves. Asserting immediately photographed three blank
  // tiles on the second navigation.
  await page.waitForFunction(
    () =>
      [...document.querySelectorAll('[data-testid="pack-avatar-sprite"] canvas')].every(c => {
        const data = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
        for (let p = 3; p < data.length; p += 4) if (data[p] > 10) return true
        return false
      }),
    null,
    { timeout: 10000 },
  )
  await page.locator('#root').screenshot({ path: join(OUT, `formats-${theme}.png`) })
  console.log(`captured formats-${theme}.png`)
}

// The armed delete confirm. The UX reader "would not dare" touch Delete because
// no undo was mentioned — the two-click confirm and the in-use refusal both ship,
// and neither appeared in any frame.
await page.goto(`${BASE}/capture/crew-pack-avatars.html?scene=library&theme=dark`)
await openLibrary()
await page.getByTestId('avatar-pack-delete-aurora').click()
await page.getByTestId('avatar-pack-delete-confirm-aurora').waitFor()
await page.getByTestId('avatar-pack-delete-cancel-aurora').waitFor()
await page.screenshot({ path: join(OUT, 'library-delete-armed-dark.png') })
console.log('captured library-delete-armed-dark.png')

// The refusal itself: deleting a pack a crew wears names those crews and stops.
deleteAnswer = 'in-use'
await page.getByTestId('avatar-pack-delete-confirm-aurora').click()
const inUse = page.getByTestId('avatar-pack-error')
await inUse.waitFor()
const inUseText = await inUse.innerText()
if (!inUseText.includes('aurora-crew') || !inUseText.includes('radar')) {
  throw new Error(`the in-use refusal did not name the wearing crews: ${inUseText}`)
}
// The card must still be there — nothing was removed.
await page.getByTestId('avatar-pack-card-aurora').waitFor()
await page.screenshot({ path: join(OUT, 'library-in-use-dark.png') })
console.log('captured library-in-use-dark.png')
deleteAnswer = 'ok'

// First run: the library is empty until something is imported.
listing = []
await page.goto(`${BASE}/capture/crew-pack-avatars.html?scene=library&theme=dark`)
await page.getByRole('button', { name: 'Library' }).click()
await page.getByTestId('avatar-library-empty').waitFor()
const emptyText = await page.getByTestId('avatar-library-pane').innerText()
if (emptyText.includes('components.avatarBuilder')) {
  throw new Error(`raw catalog keys are rendering instead of copy:\n${emptyText}`)
}
await page.screenshot({ path: join(OUT, 'library-empty-dark.png') })
console.log('captured library-empty-dark.png')
listing = PACKS

// Narrow width, with a library big enough for the scroll region to mean
// something: three cards overflow the 380px grid by ~18px, so a frame of THAT
// shows a shaved card border rather than a list with more behind it.
listing = [
  ...PACKS,
  { ...PACKS[1], id: 'ember', name: 'Ember', author: 'rue' },
  { ...PACKS[1], id: 'moss', name: 'Moss', author: 'kit' },
  { ...PACKS[1], id: 'tide', name: 'Tide', author: 'ash' },
]

// Narrow width: the cards reflow rather than overflowing sideways.
//
// The grid is a 380px-tall scroll region (the same cap the trait grid and the
// Expressions pane use), so at phone width its rows genuinely do not all fit —
// and a card cut at the container's BOTTOM edge photographs as clipped even
// though it is only scrolled. So the frame is taken with the list scrolled to
// its end: the last card is whole, and the partial card at the TOP reads as
// what it is, content above the fold. Asserted, because a scroll that silently
// did nothing would photograph exactly like the clipped frame this replaces.
await page.setViewportSize({ width: 390, height: 1000 })
await page.goto(`${BASE}/capture/crew-pack-avatars.html?scene=library&theme=dark`)
await page.getByRole('button', { name: 'Library' }).click()
await page.getByTestId('avatar-library-pane').waitFor()
await page.getByTestId('avatar-pack-card-tide').waitFor()
const grid = page.getByRole('listbox', { name: 'Library' })

// Scroll so the top cut lands MID-THUMBNAIL rather than at a card boundary.
//
// Scrolled to the very end, the hidden region is whatever the row pitch happens
// to leave — here ~25px, which is the card's own padding and border and is no
// taller than the 24px fade. The frame then showed a card missing its top border
// and read as clipped, which is the opposite of what the fade is for. Cutting
// through a tile instead leaves a visibly half-faded drawing at the top edge,
// and content that continues cannot be mistaken for content that was cropped.
const geometry = await grid.evaluate(el => {
  const cards = [...el.querySelectorAll('[data-testid^="avatar-pack-card-"]')]
  const tops = [...new Set(cards.map(c => Math.round(c.offsetTop)))].sort((a, b) => a - b)
  const first = cards[0]
  const thumb = first.querySelector('img') ?? first
  // Relative to the SCROLLER, via rects rather than offsetTop: offsetTop is
  // measured from the nearest positioned ancestor, which here is the dialog, so
  // it reported a number the scroll offset could never reach.
  const gridRect = el.getBoundingClientRect()
  const thumbRect = thumb.getBoundingClientRect()
  return {
    rowPitch: tops.length > 1 ? tops[1] - tops[0] : 0,
    thumbTop: Math.round(thumbRect.top - gridRect.top + el.scrollTop),
    thumbHeight: Math.round(thumbRect.height),
    overflow: el.scrollHeight - el.clientHeight,
  }
})
console.log(`  narrow grid geometry: ${JSON.stringify(geometry)}`)
if (geometry.overflow < 80) {
  throw new Error(`only ${geometry.overflow}px is hidden — too little to photograph a scroll region`)
}
const target = Math.min(
  Math.round(geometry.thumbTop + geometry.thumbHeight / 2),
  geometry.overflow,
)
await grid.evaluate((el, top) => { el.scrollTop = top }, target)
// The fade is state, so wait for it rather than racing the scroll event: a frame
// taken before it lands photographs the hard cut this replaced.
await grid.evaluate(el => el.dispatchEvent(new Event('scroll')))
const settled = await grid.evaluate(el => el.scrollTop)
// The cut has to be clear of the card's own edge by more than the fade, or the
// fade is again covering nothing but padding.
const intoTile = settled - geometry.thumbTop
if (intoTile < 24) {
  throw new Error(
    `the cut is only ${intoTile}px into the first drawing — the fade would cover the card's padding, not its art`,
  )
}
// BOTH edges must be faded: content above AND below is what a scroll region is,
// and a frame showing one of them is half the evidence.
await page
  .locator('[data-testid="avatar-library-grid"][data-fade="topbottom"]')
  .waitFor({ timeout: 5000 })
await page.screenshot({ path: join(OUT, 'library-narrow-390.png') })
console.log(`captured library-narrow-390.png (cut ${intoTile}px into the first drawing)`)

await browser.close()
console.log(`done → ${OUT}`)
