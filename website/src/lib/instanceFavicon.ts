/** A labelled instance's tab icon: its first character on the theme accent. */
export function instanceFavicon(label: string, accent: string, ink: string): string {
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    + `<rect width="32" height="32" rx="7" fill="${accent || '#888'}"/>`
    + `<text x="16" y="23" text-anchor="middle" font-family="system-ui,sans-serif" font-size="20" font-weight="700" fill="${ink || '#fff'}">${[...label][0]?.toUpperCase() ?? ''}</text>`
    + '</svg>'
  return `data:image/svg+xml,${encodeURIComponent(svg)}`
}
