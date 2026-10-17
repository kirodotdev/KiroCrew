import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import assert from 'node:assert/strict'

const out = resolve(process.argv[2] ?? '../temp-screenshots/chat-core-attach')
mkdirSync(out, { recursive: true })
const server = await createServer({ server: { host: '127.0.0.1', port: 0 } })
let browser
try {
  await server.listen()
  const address = server.httpServer.address()
  assert.equal(address.address, '127.0.0.1')
  const base = `http://127.0.0.1:${address.port}`
  browser = await chromium.launch()
  const errors = []
  for (const scene of [
    { name: 'staged-dark', mode: 'dark', width: 600 },
    { name: 'staged-light-phone', mode: 'light', width: 390 },
    { name: 'refused-dark', mode: 'dark', width: 600, query: '&refuse=1' },
    { name: 'denied-light-phone', mode: 'light', width: 390, query: '&deny=1' },
    { name: 'queue-dark-phone', mode: 'dark', width: 390, query: '&running=1' },
  ]) {
    const page = await browser.newPage({ viewport: { width: scene.width, height: 480 } })
    page.on('pageerror', error => errors.push(String(error)))
    await page.goto(`${base}/capture/chat-core-attach.html?mode=${scene.mode}${scene.query ?? ''}`)
    const box = page.getByRole('textbox', { name: 'Chat message' })
    await box.waitFor()
    if (scene.query === '&deny=1') {
      assert.equal(await page.getByTestId('chat-embed-file-input').count(), 0)
      await box.evaluate(element => {
        const data = new DataTransfer()
        data.items.add(new File(['fake'], 'example.pdf', { type: 'application/pdf' }))
        element.dispatchEvent(new DragEvent('drop', { dataTransfer: data, bubbles: true, cancelable: true }))
      })
      await page.getByTestId('chat-embed-attach-notice').waitFor()
    } else {
      await page.getByTestId('chat-embed-file-input').setInputFiles({ name: 'example.pdf', mimeType: 'application/pdf', buffer: Buffer.from('Fake sample only') })
      await page.getByRole('group', { name: '/sample/example.pdf', exact: true }).waitFor()
      const send = page.getByRole('button', { name: scene.query === '&running=1' ? 'Queue message' : 'Send message', exact: true })
      assert.equal(await send.isEnabled(), true)
      if (scene.query === '&refuse=1') {
        await box.fill('Read this sample')
        await send.click()
        await page.getByText("Couldn't send this message", { exact: false }).first().waitFor()
        await page.getByRole('group', { name: '/sample/example.pdf', exact: true }).waitFor()
        assert.equal(await box.inputValue(), 'Read this sample')
      }
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
    await page.evaluate(() => Promise.allSettled(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished)))
    await page.screenshot({ path: `${out}/${scene.name}.png` })
    console.log(`${scene.name}: passed`)
    await page.close()
  }
  assert.deepEqual(errors, [])
} finally {
  await browser?.close()
  await server.close()
}
