/** A mock app screenshot at an exact pixel size: dark status bar, light content
 *  rows. Rendered by the browser the caller already launched and captured at
 *  deviceScaleFactor 1, so the bytes are a real PNG at exactly w x h with no
 *  encoder of our own. The aspect ratio is the whole point of the fixture —
 *  and generating it in-browser keeps a harness self-contained: it never reads
 *  another feature's committed screenshots, which the temp-screenshots cleanup
 *  workflow prunes on a schedule. */
export async function mockShot(browser, w, h) {
  const page = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 1 })
  await page.setContent(`<!doctype html><style>
    html,body{margin:0;height:100%;background:#fafafc}
    .bar{height:5%;background:#1c2638}
    .row{height:7.5%;margin:3.5% 6% 0;background:#e2e6ee}
  </style><div class="bar"></div>${'<div class="row"></div>'.repeat(6)}`)
  const buf = await page.screenshot()
  await page.close()
  return buf
}
