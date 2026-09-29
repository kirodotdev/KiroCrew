import { test, expect } from '@playwright/test'
import { buildSync } from 'esbuild'
import { fileURLToPath } from 'node:url'

// Exercise the production builder in Chromium: selector engines in DOM mocks
// do not reproduce SVG's namespaced attributes. No gateway or credentials needed.
test.use({ storageState: { cookies: [], origins: [] } })
const bundle = buildSync({
  entryPoints: [fileURLToPath(new URL('../src/pages/chat/command-center/dashboardDocument.ts', import.meta.url))],
  bundle: true, write: false, format: 'iife', globalName: 'taskDashboard',
}).outputFiles[0].text

const fakeTail = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234'
const quoteCases = [
  ['block', `<style>q{quotes:"ghp_" "${fakeTail}"}</style><q></q>`],
  ['inline', `<q style='quotes:"ghp_" "${fakeTail}"'></q>`],
  ['escaped property', String.raw`<style>q{qu\6f tes:"ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234"}</style><q></q>`],
  ['inherited variable', `<style>body{--pair:"ghp_" "${fakeTail}";quotes:var(--pair)}</style><q></q>`],
  ['nested', `<style>@media(min-width:1px){@supports(display:grid){q{quotes:"ghp_" "${fakeTail}"}}}</style><q></q>`],
  ['keyframe', `<style>@keyframes quotation{to{quotes:"ghp_" "${fakeTail}"}}q{animation:quotation 0s forwards}</style><q></q>`],
] as const

for (const [name, html] of quoteCases) {
  test(`automatic card cannot supply native quotation text: ${name}`, async ({ page }) => {
    const attempted: string[] = []
    await page.route('**/*', route => { attempted.push(route.request().url()); return route.abort() })
    await page.addScriptTag({ content: bundle })
    const documents = await page.evaluate(html => {
      const builder = (window as unknown as { taskDashboard: { dashboardDocument: (html: string, vars: object, mode: string, data?: Record<string, string>) => string } }).taskDashboard
      return [builder.dashboardDocument(html, {}, 'light'), builder.dashboardDocument(html, {}, 'light', {})]
    }, html)
    const cdp = await page.context().newCDPSession(page)
    try {
      const text = async () => (await cdp.send('Accessibility.getFullAXTree')).nodes
        .filter(node => node.role?.value === 'StaticText').map(node => node.name?.value || '').join('')
      await page.setContent(documents[0])
      await expect.poll(text).toBe('ghp_' + fakeTail)
      await page.setContent(documents[1])
      expect(await text()).not.toContain('ghp_' + fakeTail)
      expect(await page.locator('q').evaluate(el => getComputedStyle(el).quotes)).toBe('auto')
      await page.locator('q').evaluate(el => { el.textContent = 'Ordinary quotation' })
      await expect.poll(text).toContain('Ordinary quotation')
      expect(await text()).not.toContain('ghp_')
      expect(attempted).toEqual([])
    } finally { await cdp.detach() }
  })
}

const cssTextCases = [
  ['escaped', String.raw`body::before{content:"ghp\5f ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234"}`],
  ['concatenated', `body::before{content:"ghp_" "${fakeTail}"}`],
  ['escaped property', String.raw`body::before{c\6f ntent:"ghp_" "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234"}`],
  ['variable', `body{--first:"ghp_";--text:var(--first) "${fakeTail}"}body::before{content:var(--text)}`],
  ['attribute', 'body::before{content:attr(data-first) attr(data-last)}'],
  ['after', `body::after{content:"ghp_" "${fakeTail}"}`],
  ['marker', `li::marker{content:"ghp_" "${fakeTail}"}`],
  ['nested', `@media(min-width:1px){@supports(display:grid){body::before{content:"ghp_" "${fakeTail}"}}}`],
  ['nested selector', `body{&::before{content:"ghp_" "${fakeTail}"}}`],
  ['keyframe', `@keyframes words{to{content:"ghp_" "${fakeTail}"}}body::before{content:"";animation:words 0s forwards}`],
] as const

for (const inline of [false, true]) {
  test(`automatic card cannot supply hyphenation text: inline=${inline}`, async ({ page }) => {
    const attempted: string[] = []
    await page.route('**/*', route => { attempted.push(route.request().url()); return route.abort() })
    await page.addScriptTag({ content: bundle })
    const documents = await page.evaluate(inline => {
      const builder = (window as unknown as { taskDashboard: { dashboardDocument: (html: string, vars: object, mode: string, data?: Record<string, string>) => string } }).taskDashboard
      const style = String.raw`width:20px;font:16px monospace;hyphens:manual;hyphenate-character:"ghp\5f ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef1234"`
      const html = inline ? `<p style='${style}'>ab&shy;cd</p>` : `<style>p{${style}}</style><p>ab&shy;cd</p>`
      return [builder.dashboardDocument(html, {}, 'light'), builder.dashboardDocument(html, {}, 'light', {})]
    }, inline)
    const cdp = await page.context().newCDPSession(page)
    try {
      const text = async () => (await cdp.send('Accessibility.getFullAXTree')).nodes
        .filter(node => node.role?.value === 'InlineTextBox').map(node => node.name?.value || '').join('')
      await page.setContent(documents[0])
      await expect.poll(text).toContain('ghp_' + fakeTail)
      await page.setContent(documents[1])
      expect(await text()).not.toContain('ghp_' + fakeTail)
      expect(await text()).toContain('‐') // ordinary browser hyphen still renders
      expect(await page.locator('p').evaluate(el => ({ width: getComputedStyle(el).width, hyphens: getComputedStyle(el).hyphens, character: getComputedStyle(el).hyphenateCharacter }))).toEqual({ width: '20px', hyphens: 'manual', character: 'auto' })
      expect(attempted).toEqual([])
    } finally { await cdp.detach() }
  })
}

for (const [name, css] of cssTextCases) {
  test(`automatic card blocks CSS generated text: ${name}`, async ({ page }) => {
    await page.route('**/*', route => route.abort())
    await page.addScriptTag({ content: bundle })
    const documents = await page.evaluate(({ css, tail }) => {
      const builder = (window as unknown as { taskDashboard: { dashboardDocument: (html: string, vars: object, mode: string, data?: Record<string, string>) => string } }).taskDashboard
      const html = `<style>${css}</style><body data-first="ghp_" data-last="${tail}"><ul><li>Evidence</li></ul></body>`
      return [builder.dashboardDocument(html, {}, 'light'), builder.dashboardDocument(html, {}, 'light', {})]
    }, { css, tail: fakeTail })
    for (const [index, document] of documents.entries()) {
      await page.setContent(document)
      const generatedText = () => page.evaluate(() => [
        getComputedStyle(document.body, '::before').content,
        getComputedStyle(document.body, '::after').content,
        getComputedStyle(document.querySelector('li')!, '::marker').content,
      ].map(value => [...value.matchAll(/"([^"\\]*(?:\\.[^"\\]*)*)"/g)].map(match => JSON.parse(match[0])).join('')).join(''))
      if (index === 0) await expect.poll(generatedText).toContain('ghp_' + fakeTail)
      else expect(await generatedText()).not.toContain('ghp_' + fakeTail)
    }
  })
}

test('automatic cards remove inline and list-marker text but keep safe layout and literal bindings', async ({ page }) => {
  const attempted: string[] = []
  await page.route('**/*', route => { attempted.push(route.request().url()); return route.abort() })
  await page.addScriptTag({ content: bundle })
  const result = await page.evaluate(tail => {
    const builder = (window as unknown as { taskDashboard: { dashboardDocument: (html: string, vars: object, mode: string, data?: Record<string, string>) => string } }).taskDashboard
    const html = `<style>@import url("https://outside.invalid/sheet");
      @counter-style secret{system:cyclic;symbols:"ghp_${tail}";suffix:""}
      .map{display:grid;--gap:13px;gap:var(--gap)}
      @media(min-width:1px){.map{display:flex}}
      #custom{list-style-type:secret}#string{list-style-type:"ghp_${tail}"}
      #variable{--marker:"ghp_${tail}";list-style:var(--marker)}
      </style><style data-dashboard-field="css" style='content:"hidden"'>p{margin:0}</style>
      <article class="map" style='content:"ghp_" "${tail}";padding:7px'>
      <details><summary>Evidence</summary>Passed</details><svg><path d="M0 0L10 10" /></svg>
      <p data-dashboard-field="result">Old</p><ul><li id="custom">Custom</li><li id="string">String</li><li id="variable">Variable</li><li id="inline" style='list-style:"hidden";display:list-item'>Inline</li></ul></article>`
    const automatic = builder.dashboardDocument(html, { '--bg': '#111' }, 'light', { result: '&lt;literal&gt;', css: 'p{display:none}' })
    const updated = builder.dashboardDocument(html, {}, 'light', { result: 'Updated' })
    const parsed = new DOMParser().parseFromString(automatic, 'text/html')
    return { automatic, updated, saved: builder.dashboardDocument(html, {}, 'light'),
      hostDisplay: getComputedStyle(document.body).display,
      hostChildren: document.body.children.length,
      inlineContent: parsed.querySelector('article')?.getAttribute('style'),
      styleContent: parsed.querySelector('style[style]')?.getAttribute('style') }
  }, fakeTail)
  expect(result.saved).toContain('@counter-style')
  expect(result.saved).toContain('list-style-type:secret')
  expect(result.automatic).not.toContain('@counter-style')
  expect(result.inlineContent).not.toContain('content')
  expect(result.styleContent).not.toContain('content')
  expect(result.hostDisplay).toBe('block')
  expect(result.hostChildren).toBe(0) // no model nodes joined the host
  await page.setContent(result.automatic)
  expect(await page.locator('article').evaluate(el => ({ display: getComputedStyle(el).display, gap: getComputedStyle(el).gap, padding: getComputedStyle(el).padding }))).toEqual({ display: 'flex', gap: '13px', padding: '7px' })
  for (const id of ['custom', 'string', 'variable', 'inline']) {
    expect(await page.locator(`#${id}`).evaluate(el => getComputedStyle(el).listStyleType)).toBe('disc')
  }
  await expect(page.locator('p[data-dashboard-field]')).toHaveText('&lt;literal&gt;')
  expect(await page.locator('style[data-dashboard-field]').textContent()).toContain('margin: 0px')
  expect(await page.locator('p').evaluate(el => getComputedStyle(el).display)).toBe('block')
  await expect(page.locator('svg path')).toHaveAttribute('d', 'M0 0L10 10')
  await page.locator('summary').click()
  await expect(page.locator('details')).toHaveAttribute('open', '')
  await page.setContent(result.updated)
  await expect(page.locator('p[data-dashboard-field]')).toHaveText('Updated')
  expect(attempted).toEqual([])
})

test('automatic card CSS serialization cannot introduce an HTML closing boundary', async ({ page }) => {
  await page.addScriptTag({ content: bundle })
  const html = await page.evaluate(() => {
    const builder = (window as unknown as { taskDashboard: { dashboardDocument: (html: string, vars: object, mode: string, data: object) => string } }).taskDashboard
    return builder.dashboardDocument(String.raw`<style>body{font-family:"\3c /style>\3c img id=escaped src=https://outside.invalid>"}</style><p>Safe</p>`, {}, 'light', {})
  })
  await page.setContent(html)
  await expect(page.locator('#escaped')).toHaveCount(0)
  expect(await page.locator('style').count()).toBe(1)
  await expect(page.locator('p')).toHaveText('Safe')
})

for (const failure of ['unavailable', 'parse', 'inspect'] as const) {
  test(`automatic card CSS fails closed when CSSOM is ${failure}`, async ({ page }) => {
    await page.addScriptTag({ content: bundle })
    const result = await page.evaluate(failure => {
      const builder = (window as unknown as { taskDashboard: { dashboardDocument: (html: string, vars: object, mode: string, data?: object) => string } }).taskDashboard
      if (failure === 'unavailable') Object.defineProperty(window, 'CSSStyleSheet', { value: undefined })
      else if (failure === 'parse') CSSStyleSheet.prototype.replaceSync = () => { throw new Error('synthetic parser failure') }
      else Object.defineProperty(CSSStyleSheet.prototype, 'cssRules', { get() { throw new Error('synthetic unreadable rules') } })
      const html = '<style>body::before{content:"hidden"}</style><p style="content:attr(title);display:grid" title="hidden">Visible</p>'
      return { automatic: builder.dashboardDocument(html, {}, 'light', {}), saved: builder.dashboardDocument(html, {}, 'light') }
    }, failure)
    await page.setContent(result.automatic)
    expect(await page.locator('style').count()).toBe(1)
    expect(await page.locator('p').getAttribute('style') || '').not.toContain('content')
    await expect(page.locator('p')).toHaveText('Visible')
    expect(result.saved).toContain('body::before')
  })
}

for (const kind of ['html', 'svg', 'xlink'] as const) {
  for (const href of ['https://outside.invalid/synthetic', '/relative', '']) {
    test(`published view blocks ${kind} navigation to ${JSON.stringify(href)}`, async ({ page }) => {
      let document = ''
      const attempted: string[] = []
      await page.route('**/*', async route => {
        const url = route.request().url()
        if (url === 'https://dashboard.invalid/host') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><body></body>' })
        if (url === 'https://dashboard.invalid/published' && !document) return route.abort()
        if (url === 'https://dashboard.invalid/published') return route.fulfill({ contentType: 'text/html', body: document })
        attempted.push(url)
        return route.abort()
      })
      await page.goto('https://dashboard.invalid/host')
      await page.addScriptTag({ content: bundle })
      const link = kind === 'html' ? `<a id="go" href="${href}">Open</a>`
        : `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="200" height="60"><a id="go" ${kind === 'xlink' ? 'xlink:href' : 'href'}="${href}"><text x="0" y="30">Open</text></a></svg>`
      document = await page.evaluate(html => (window as unknown as { taskDashboard: { dashboardDocument: (html: string, vars: object, mode: string) => string } }).taskDashboard.dashboardDocument(html, {}, 'light'), `${link}<p id="keep">Synthetic task</p>`)
      await page.evaluate(() => {
        const iframe = document.createElement('iframe')
        iframe.setAttribute('sandbox', '')
        iframe.src = 'https://dashboard.invalid/published'
        document.body.append(iframe)
      })
      const frame = page.frameLocator('iframe')
      await expect(frame.locator('#keep')).toBeVisible()
      // Start observing before the click. Abort every synthetic outbound request;
      // the browser can navigate its own opaque iframe despite an empty sandbox.
      const navigation = page.waitForRequest(r => r.isNavigationRequest(), { timeout: 1000 }).then(r => r.url(), () => null)
      await frame.locator('#go').click()
      expect(await navigation).toBeNull()
      expect(attempted).toEqual([])
      expect(await frame.locator('#go').evaluate(el => Array.from(el.attributes).filter(a => a.localName === 'href').map(a => a.value))).toEqual([])
      await expect(frame.locator('#keep')).toBeVisible()
    })
  }
}

test('published view preserves supported SVG fragments, CSS and disclosures', async ({ page }) => {
  await page.addScriptTag({ content: bundle })
  const result = await page.evaluate(() => {
    const builder = (window as unknown as { taskDashboard: { dashboardDocument: (html: string, vars: object, mode: string) => string } }).taskDashboard
    const html = builder.dashboardDocument('<style>.map{display:grid}</style><article class="map"><details><summary>Evidence</summary>Passed</details><a href="#target">Jump</a><svg xmlns:xlink="http://www.w3.org/1999/xlink"><defs><path id="shape" d="M0 0L10 10" /></defs><use xlink:href="#shape"/><a href="#target"><text>Jump</text></a><a xlink:href="#target"><text>Linked fragment</text></a></svg><p id="target">Target</p><script>bad()</script><form><input></form><svg><animate attributeName="href" to="https://outside.invalid"/><set attributeName="href" to="https://outside.invalid"/></svg></article>', {}, 'light')
    const parsed = new DOMParser().parseFromString(html, 'text/html')
    return {
      links: Array.from(parsed.querySelectorAll('*')).flatMap(el => Array.from(el.attributes).filter(a => a.localName === 'href').map(a => a.value)),
      path: parsed.querySelector('defs path')?.getAttribute('d'),
      disclosure: parsed.querySelector('details summary')?.textContent,
      css: html.includes('.map{display:grid}'),
      forbidden: parsed.querySelectorAll('script,form,input,animate,set').length,
      csp: parsed.head.firstElementChild?.getAttribute('content'),
    }
  })
  // The existing sanitizer strips <use>; this repair does not widen its allowlist.
  expect(result.links).toEqual(['#target', '#target', '#target'])
  expect(result.path).toBe('M0 0L10 10')
  expect(result.disclosure).toBe('Evidence')
  expect(result.css).toBe(true)
  expect(result.forbidden).toBe(0)
  expect(result.csp).toContain("script-src 'none'")
})
