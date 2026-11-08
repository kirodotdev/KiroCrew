/**
 * Hand the active installed theme's boot-splash art (`branding/splash.*`) to
 * the desktop shell, which shows it on the next loading.html paint, before any
 * gateway is up to serve the pack (website/electron/theme-splash.js). A null
 * `url` (the theme ships no splash) clears the shell's copy so the stock
 * splash returns. A download that fails (an expired session mid-refresh, a
 * network blip) hands over nothing and keeps the stored art; the next branding
 * pass reports it again. `superseded` drops the answer of a fetch a later theme
 * switch overtook. A no-op in a plain browser.
 */
// The types theme-splash.js stores; any other body (a proxy's HTML page) is a
// failed download, not a theme without art.
const SPLASH_TYPES = ['image/png', 'image/svg+xml']

export function reportThemeSplash(url: string | null, superseded: () => boolean): void {
  const setThemeSplash = window.electronAPI?.setThemeSplash
  if (!setThemeSplash) return
  if (!url) {
    setThemeSplash(null)
    return
  }
  fetch(url)
    .then((res) => (res.ok ? res.blob() : null))
    .then(async (blob) =>
      blob && SPLASH_TYPES.includes(blob.type)
        ? { type: blob.type, bytes: new Uint8Array(await blob.arrayBuffer()) }
        : null,
    )
    .catch(() => null)
    .then((image) => {
      if (image && !superseded()) setThemeSplash(image)
    })
}
