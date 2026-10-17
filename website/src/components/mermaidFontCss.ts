/** Keep the live Mermaid label fonts inside the PNG's foreignObject image.
 * html-to-image 1.11.13's font scanner uses style.fontFamily, which Firefox's
 * CSSFontFaceDescriptors lacks. Use the standard property accessor instead.
 * No diagram data is sent: these are GETs for already referenced font assets.
 */
export async function mermaidFontCss(node: HTMLElement): Promise<string> {
  const normalize = (family: string) => family.trim().replace(/^["']|["']$/g, '').toLowerCase()
  const used = new Set([node, ...node.querySelectorAll('*')].flatMap(element =>
    getComputedStyle(element).fontFamily.split(',').map(normalize)))
  const embedded: string[] = []
  for (const sheet of Array.from(node.ownerDocument.styleSheets)) {
    let rules: CSSRuleList
    try {
      rules = sheet.cssRules
    } catch {
      if (!sheet.href) continue
      try {
        const response = await fetch(sheet.href)
        if (!response.ok) continue
        const readable = new CSSStyleSheet()
        readable.replaceSync(await response.text())
        rules = readable.cssRules
      } catch {
        continue // Unavailable stylesheets must not prevent a PNG download.
      }
    }
    for (const rule of Array.from(rules)) {
      if (rule.type !== CSSRule.FONT_FACE_RULE) continue
      const face = rule as CSSFontFaceRule
      if (!used.has(normalize(face.style.getPropertyValue('font-family')))) continue
      try {
        let css = face.cssText
        for (const match of css.matchAll(/url\(["']?([^"')]+)["']?\)/g)) {
          if (match[1].startsWith('data:')) continue
          const response = await fetch(new URL(match[1], sheet.href || document.baseURI).href)
          if (!response.ok) throw new Error('Could not load diagram font')
          const blob = await response.blob()
          const data = await new Promise<string>((resolve, reject) => {
            const reader = new FileReader()
            reader.onload = () => resolve(String(reader.result))
            reader.onerror = () => reject(reader.error)
            reader.readAsDataURL(blob)
          })
          css = css.replace(match[0], match[0].replace(match[1], data))
        }
        embedded.push(css)
      } catch {
        // Omit incomplete faces so the rasterizer never retries remote font URLs.
        continue
      }
    }
  }
  return embedded.join('\n')
}
