import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const OUT = process.argv[2] || '/tmp/spec-hooks-shots'
const CHROMIUM_PATH = process.env.CHROMIUM_PATH
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: 5177, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
console.log('vite up on 5177')

const browser = await chromium.launch({ executablePath: CHROMIUM_PATH, args: ['--no-sandbox'] })

for (const [scene, name] of [['run-nowhere', '01-run-nowhere-dark'], ['withheld', '02-withheld-dark'],
                               ['withheld-none', '03-withheld-none-dark'], ['run-nowhere', '04-run-nowhere-light'],
                               ['withheld', '05-withheld-light'], ['withheld-none', '06-withheld-none-light']]) {
  const theme = name.includes('light') ? 'light' : 'dark'
  const ctx = await browser.newContext({ viewport: { width: 800, height: 400 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await page.goto(`http://127.0.0.1:5177/capture/spec-hooks-notice.html?scene=${scene}&theme=${theme}`)
  await page.waitForSelector('.p-4', { timeout: 15000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `${name}.png`) })
  console.log('shot', name)
  await ctx.close()
}
await browser.close()
await server.close()
console.log('done')
