/**
 * Screenshots of the side panel's oversize-question refusal (issue #3306).
 *
 * Drives the ISOLATED capture entry (website/capture/side-chat-oversize.html),
 * which mounts the REAL SideChat. Each scene TYPES an actually-oversize question
 * (over 32,768 UTF-8 bytes) and presses Enter, so the refusal in every shot is
 * the shipped byte guard firing on real input — not a posed string.
 *
 * Scenes cover the two scripts the fix exists for:
 *   emoji (en)  — 8,193 emoji = 32,772 bytes; message reports 8,193 characters
 *   CJK (zh-CN) — 11,000 han chars = 33,000 bytes; localized message, 11,000 chars
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort   # in another shell
 *   node scripts/capture-side-chat-oversize.mjs http://127.0.0.1:6813 ../temp-screenshots/side-panel-oversize-chars-3306
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/side-panel-oversize-chars-3306'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })

async function shoot(lang, oversizeText, mustSee, name) {
  await page.goto(`${BASE}/capture/side-chat-oversize.html?lang=${lang}&theme=dark`)
  const box = page.locator('textarea')
  await box.waitFor({ timeout: 15000 })
  // fill() sets the value in one commit — typing 8k+ characters key-by-key would
  // take minutes and exercise nothing extra: the guard only reads the final draft.
  await box.fill(oversizeText)
  await box.press('Enter')
  await page.waitForSelector(`text=${mustSee}`, { timeout: 15000 })
  await page.screenshot({ path: `${OUT}/${name}`, fullPage: false })
  console.log(`captured ${name}`)
}

// All-emoji question one code point past the safe floor: the message must say
// 8,193 characters, a number the user can act on, never 32,772 bytes.
await shoot('en', '😀'.repeat(8_193),
  'reduce to under ~8,192 characters (yours: 8,193)',
  '01-emoji-refusal-en-dark.png')

// CJK question at 3 bytes per character: 11,000 chars bust the byte budget while
// the old message's "32,768 bytes" would have suggested they still had room.
await shoot('zh-CN', '问'.repeat(11_000),
  '（当前：11,000）',
  '02-cjk-refusal-zh-dark.png')

await browser.close()
