// Shared phone-profile boot for the scroll rigs: throttled CPU + 3G-ish
// network, 390x844 touch viewport, main-frame navigation logging. Rigs that
// need a different profile keep their own preamble on purpose.
import { chromium } from 'playwright'

export async function bootPhoneRig(url) {
  const context = process.env.RIGPROFILE
    ? await chromium.launchPersistentContext(process.env.RIGPROFILE, { headless: true, viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })
    : await (await chromium.launch({ headless: true })).newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })
  const page = await context.newPage()
  const cdp = await context.newCDPSession(page)
  await cdp.send('Emulation.setCPUThrottlingRate', { rate: 4 })
  await cdp.send('Network.enable')
  await cdp.send('Network.emulateNetworkConditions', {
    offline: false, latency: 150, downloadThroughput: 1.5 * 1024 * 1024 / 8, uploadThroughput: 750 * 1024 / 8,
  })
  page.on('framenavigated', (f) => { if (f === page.mainFrame()) console.error('NAV ->', f.url().slice(0, 90)) })
  await page.goto(url, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(12000)
  return { context, page, cdp }
}
