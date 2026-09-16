import { afterEach, expect, it, vi } from 'vitest'
import { mermaidFontCss } from '../components/mermaidFontCss'

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('embeds used webfonts through CSS properties Firefox exposes', async () => {
  const node = document.createElement('div')
  const face = {
    type: CSSRule.FONT_FACE_RULE,
    // Firefox CSSFontFaceDescriptors lacks the fontFamily convenience property.
    style: { getPropertyValue: () => '"Diagram Font"' },
    cssText: '@font-face { font-family: "Diagram Font"; src: url("diagram.woff2"); }',
  }
  vi.spyOn(document, 'styleSheets', 'get').mockReturnValue([
    { href: 'https://example.test/fonts/style.css', cssRules: [face] },
  ] as unknown as StyleSheetList)
  vi.spyOn(window, 'getComputedStyle').mockReturnValue({ fontFamily: '"Diagram Font", sans-serif' } as CSSStyleDeclaration)
  const fetchFont = vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(['font bytes'], { type: 'font/woff2' }) })
  vi.stubGlobal('fetch', fetchFont)
  const css = await mermaidFontCss(node)
  expect(fetchFont).toHaveBeenCalledWith('https://example.test/fonts/diagram.woff2')
  expect(css).toContain('data:font/woff2;base64,')
  expect(css).not.toContain('url("diagram.woff2")')
})

it('embeds fonts from a cross-origin stylesheet when CSSOM access is denied', async () => {
  const node = document.createElement('div')
  const sheet = { href: 'https://fonts.example.test/style.css', get cssRules(): CSSRuleList { throw new DOMException('Cross-origin', 'SecurityError') } }
  vi.spyOn(document, 'styleSheets', 'get').mockReturnValue([sheet] as unknown as StyleSheetList)
  vi.spyOn(window, 'getComputedStyle').mockReturnValue({ fontFamily: 'DiagramFont' } as CSSStyleDeclaration)
  const fetchAsset = vi.fn()
    .mockResolvedValueOnce({ ok: true, text: async () => '@font-face { font-family: DiagramFont; src: url("font.woff2"); }' })
    .mockResolvedValueOnce({ ok: true, blob: async () => new Blob(['font'], { type: 'font/woff2' }) })
  vi.stubGlobal('fetch', fetchAsset)
  expect(await mermaidFontCss(node)).toContain('data:font/woff2;base64,')
  expect(fetchAsset.mock.calls.map(call => call[0])).toEqual([
    'https://fonts.example.test/style.css', 'https://fonts.example.test/font.woff2',
  ])
})
