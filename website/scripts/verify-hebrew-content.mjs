import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import { chromium, firefox, webkit, expect } from '@playwright/test'

const base = process.argv[2]
if (!base) throw new Error('Usage: node scripts/verify-hebrew-content.mjs <loopback-vite-url> [output-directory]')
const out = process.argv[3] || '../temp-screenshots/hebrew-content'
await mkdir(out, { recursive: true })
const engine = process.env.RTL_BROWSER || 'chromium'
const browserType = { chromium, firefox, webkit }[engine]
if (!browserType) throw new Error(`Unknown browser: ${engine}`)
const browser = await browserType.launch()
const lexical = process.env.RTL_LEXICAL === '1'

async function verifyLexical(page, label) {
  const input = page.locator('[data-lexical-composer]')
  await expect(input).toBeVisible()
  await page.evaluate(() => document.fonts.ready)
  const observations = []
  async function verifyDraft(value, phase) {
    await expect.poll(() => input.evaluate(root => {
      const text = node => node.nodeName === 'BR' ? '\n' : node.nodeType === Node.TEXT_NODE ? node.textContent : Array.from(node.childNodes, text).join('')
      return text(root)
    })).toBe(value)
    await input.scrollIntoViewIfNeeded()
    const layout = await input.evaluate(root => {
      const p = root.querySelector('p')
      const box = p.getBoundingClientRect()
      const walker = document.createTreeWalker(p, NodeFilter.SHOW_TEXT)
      const lines = []
      while (walker.nextNode()) {
        const node = walker.currentNode
        if (!node.textContent) continue
        const range = document.createRange()
        range.selectNodeContents(node)
        const rect = range.getBoundingClientRect()
        range.setStart(node, 0); range.setEnd(node, 1)
        const first = range.getBoundingClientRect().left
        range.setStart(node, node.textContent.length - 1); range.setEnd(node, node.textContent.length)
        lines.push({ text: node.textContent, left: rect.left, right: rect.right, first, last: range.getBoundingClientRect().left })
      }
      const style = getComputedStyle(p)
      return { left: box.left, right: box.right, lines, dir: p.dir, direction: style.direction, unicodeBidi: style.unicodeBidi, textAlign: style.textAlign, shellDirection: getComputedStyle(document.documentElement).direction }
    })
    observations.push({ phase, value, ...layout })
    await page.screenshot({ path: `${out}/${label}-lexical-${phase}.png` })
    await writeFile(`${out}/${label}-lexical.json`, JSON.stringify(observations, null, 2))
    const english = layout.lines.find(line => line.text.startsWith('English'))
    assert(english, 'English paragraph must be present')
    assert(Math.abs(english.left - layout.left) < 2, 'English paragraph must align to the left content edge')
    assert(english.first < english.last, 'English terminal punctuation must follow its text visually')
    for (const line of layout.lines.filter(line => !line.text.startsWith('English'))) {
      assert(Math.abs(line.right - layout.right) < 2, 'Hebrew-first and neutral-prefix Hebrew paragraphs must align right')
    }
    assert.equal(layout.unicodeBidi, 'plaintext')
    assert.equal(layout.textAlign, 'start')
    assert.equal(layout.dir, 'auto')
    assert.equal(layout.shellDirection, 'ltr')
    const selection = await input.evaluate(root => {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
      while (walker.nextNode()) {
        const node = walker.currentNode, start = node.textContent.indexOf('AWS')
        if (start < 0) continue
        const range = document.createRange()
        range.setStart(node, start); range.setEnd(node, start + 3)
        const selection = window.getSelection()
        selection.removeAllRanges(); selection.addRange(range)
        return String(selection)
      }
    })
    assert.equal(selection, 'AWS')
    await page.getByRole('button', { name: 'Send', exact: true }).click()
    await expect.poll(() => page.getByTestId('submitted').textContent()).toBe(value)
  }
  await verifyDraft('שלום AWS (123).\nEnglish paragraph.\n123 — עברית', 'restored')
  await input.fill('')
  await expect(input).toHaveText('')
  await input.focus()
  await page.keyboard.insertText('שלום עולם (123).')
  await expect(input).toHaveText('שלום עולם (123).')
  for (const line of ['English paragraph (456).', '123 — עברית נוספת AWS (789).']) {
    await page.keyboard.press('Shift+Enter')
    await page.keyboard.press('Shift+Enter')
    await page.keyboard.insertText(line)
  }
  await verifyDraft('שלום עולם (123).\n\nEnglish paragraph (456).\n\n123 — עברית נוספת AWS (789).', 'typed-blanklines')
}

try {
  for (const [width, theme, streaming] of [[1000, 'dark', false], [390, 'dark', false], [320, 'dark', false], [1000, 'light', false], [1000, 'dark', true]]) {
    const page = await browser.newPage({ viewport: { width, height: 1100 } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    // Match backend endpoints only; Vite also serves source files under /src/api/.
    await page.route(url => url.pathname.startsWith('/api/'), route => route.fulfill({
      json: /commands|skills|agents|models|sessions|files|artifacts/.test(route.request().url()) ? [] : {},
    }))
    try {
      if (lexical) {
        await page.goto(`${base}/capture/hebrew-content.html?lexical=1&theme=${theme}${streaming ? '&stream=1' : ''}`)
        await verifyLexical(page, `${width}-${theme}${streaming ? '-streaming' : ''}`)
        assert.deepEqual(errors, [])
        console.log(`PASS ${engine} ${width}px ${theme}${streaming ? ' streaming' : ''}: Lexical restored/typed paragraphs, visual ordering, selection and exact submit`)
        continue
      }
      await page.goto(`${base}/capture/hebrew-content.html?theme=${theme}${streaming ? '&stream=1' : ''}`)
      const input = page.getByRole('textbox', { name: 'Message input', exact: true })
      await expect(input).toBeVisible()
      await page.evaluate(() => document.fonts.ready)
      await expect(page.getByRole('heading', { name: 'חדשות AWS בישראל' })).toHaveCSS('direction', 'rtl')
      const prose = page.locator('main p')
      await expect(prose.nth(0)).toHaveCSS('direction', 'rtl')
      await expect(prose.nth(1)).toHaveCSS('direction', 'ltr')
      for (const quote of await page.locator('blockquote').all()) {
        await expect(quote).toHaveCSS('direction', 'rtl')
        await expect(quote).toHaveCSS('border-right-width', '3px')
      }
      await expect(page.locator('p').filter({ hasText: 'קישור בעברית' }).first()).toHaveCSS('direction', 'rtl')
      await expect(page.locator('p').filter({ hasText: 'שלום מפורש' })).toHaveCSS('direction', 'rtl')
      await expect(page.locator('div[dir="auto"]').filter({ hasText: 'קישור מפורש בתיבה' })).toHaveCSS('direction', 'rtl')
      await expect(page.locator('.katex').first()).toHaveCSS('direction', 'ltr')
      await expect(page.locator('.katex-mathml').first()).toHaveCSS('width', '1px')
      const equation = await page.locator('.katex-html > .katex-base').evaluateAll(nodes => nodes.map(n => n.getBoundingClientRect().left))
      assert(equation.length > 1 && equation.every((left, index) => index === 0 || left >= equation[index - 1]), 'equation segments must remain in LTR order')
      await expect(page.locator('main li').first()).toHaveCSS('direction', 'rtl')
      await expect(page.locator('main code').first()).toHaveCSS('direction', 'ltr')
      await expect(page.locator('.code-block')).toHaveCSS('direction', 'ltr')
      await expect(page.locator('th').nth(0)).toHaveCSS('direction', 'rtl')
      await expect(page.locator('th').nth(1)).toHaveCSS('direction', 'ltr')
      const columns = await page.locator('th').evaluateAll(nodes => nodes.map(n => n.getBoundingClientRect().left))
      assert(columns[0] < columns[1], 'content direction must not reverse table columns')
      assert.equal(await page.locator('html').evaluate(n => getComputedStyle(n).direction), 'ltr')
      await expect(input).toHaveCSS('unicode-bidi', 'plaintext')
      const mirror = page.locator('[aria-hidden][data-composer-typo]')
      await expect(mirror).toHaveCSS('unicode-bidi', 'plaintext')
      assert.equal(await mirror.textContent(), await input.inputValue())
      const geometry = await input.evaluate(n => {
        const backdrop = n.parentElement.querySelector('[aria-hidden][data-composer-typo]')
        const a = n.getBoundingClientRect(), b = backdrop.getBoundingClientRect()
        return { input: a.width, mirror: b.width }
      })
      assert(Math.abs(geometry.input - geometry.mirror) < 1, 'paste mirror must match textarea width')
      const original = 'שלום AWS (123).\nEnglish paragraph.\n123 — עברית'
      await expect(input).toHaveValue(original)
      await input.evaluate(n => n.setSelectionRange(5, 8))
      assert.equal(await input.evaluate(n => n.value.slice(n.selectionStart, n.selectionEnd)), 'AWS')
      await page.getByRole('button', { name: 'Send', exact: true }).click()
      await expect(page.getByTestId('submitted')).toHaveText(original, { useInnerText: false })
      assert.equal(await page.getByTestId('submitted').textContent(), original)
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)
      assert(overflow <= 1, `content overflows viewport by ${overflow}px`)
      assert.deepEqual(errors, [])
      await page.screenshot({ path: `${out}/${width}-${theme}${streaming ? '-streaming' : ''}.png` })
      await input.fill('לפני ההדבקה\nEnglish line\n')
      await input.evaluate(node => {
        const data = new DataTransfer()
        data.setData('text/plain', 'שלום עולם\nEnglish pasted line\nשורה שלישית\nשורה רביעית')
        node.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }))
      })
      const chip = page.locator('[aria-hidden] [data-paste-seq]')
      await expect(chip).toHaveCount(1)
      await expect.poll(async () => input.evaluate(node => {
        const actual = node.parentElement.querySelector('[data-paste-seq]')
        const style = getComputedStyle(node)
        const rect = node.getBoundingClientRect()
        const probe = document.createElement('div')
        probe.dir = node.dir
        for (const key of ['font', 'lineHeight', 'letterSpacing', 'padding', 'border', 'boxSizing', 'wordBreak', 'overflowWrap', 'whiteSpace', 'unicodeBidi', 'textAlign', 'direction', 'scrollbarGutter']) probe.style[key] = style[key]
        Object.assign(probe.style, { position: 'fixed', left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: 'auto', visibility: 'hidden', overflow: 'hidden' })
        const token = actual.textContent
        const offset = node.value.indexOf(token)
        const mark = document.createElement('span')
        mark.textContent = token
        probe.append(node.value.slice(0, offset), mark, node.value.slice(offset + token.length))
        document.body.append(probe)
        const expected = mark.getBoundingClientRect(), painted = actual.getBoundingClientRect()
        probe.remove()
        return Math.max(Math.abs(painted.left - expected.left), Math.abs(painted.top - expected.top + node.scrollTop))
      })).toBeLessThan(1.5)
      console.log(`PASS ${engine} ${width}px ${theme}${streaming ? ' streaming' : ''}: prose, technical text, columns, composer, selection and send`)
    } catch (error) {
      await page.screenshot({ path: `${out}/${width}-${theme}-failure.png` })
      throw error
    } finally {
      await page.close()
    }
  }
} finally {
  await browser.close()
}
