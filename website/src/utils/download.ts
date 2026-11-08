/** In the Electron shell, announce a download so the main process's
 *  will-download handler auto-saves ONLY downloads the app explicitly asked
 *  for (short-lived, single-use). Outside Electron the bridge is absent and
 *  this is a no-op; the browser handles the download itself. */
function announceDownload(filename: string): void {
  try {
    ;(window as unknown as { kirocrew?: { expectDownload?: (name: string) => void } })
      .kirocrew?.expectDownload?.(filename)
  } catch {
    // A missing/throwing bridge must never block the download itself.
  }
}

function clickDownload(href: string, filename: string): void {
  const a = document.createElement('a')
  a.href = href
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
}

/** Object-URL download; revoke deferred a tick so the click can consume it. */
export function downloadBlob(blob: Blob, filename: string): void {
  announceDownload(filename)
  const url = URL.createObjectURL(blob)
  clickDownload(url, filename)
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/** Download a same-origin URL straight to disk, so the browser streams the
 *  response instead of holding it in page memory the way `downloadBlob` must. */
export function downloadUrl(url: string, filename: string): void {
  announceDownload(filename)
  clickDownload(url, filename)
}

/** Save a file the page built, in any browser. Outside an Android in-app
 *  browser this is `downloadBlob`. Inside one a blob: click is dropped (see
 *  `isAndroidWebView`), so the bytes go up to the gateway and come back down
 *  from a real URL. Rejects when that hand-off fails, so the caller shows its
 *  own error rather than a silent no-op; a blob is no fallback there. Only the
 *  gateway's accepted types (text, html, json, svg, png) can be handed off. */
export async function saveBlob(blob: Blob, filename: string): Promise<void> {
  if (!isAndroidWebView()) {
    downloadBlob(blob, filename)
    return
  }
  const type = (blob.type || '').split(';')[0].trim().toLowerCase()
  // Base64, never blob.text(): the bytes may not be UTF-8 (a legacy svg the
  // lightbox fetched), and decoding them would corrupt the saved file.
  const content = await blobToBase64(blob)
  // Loaded on use: only this rare path needs the API client, and a static
  // import would pull it into every module that merely saves a blob.
  const { api } = await import('../api/client')
  const { url } = await api.artifactDownloadUrl(filename, type, content, 'base64')
  downloadUrl(url, filename)
}

async function blobToBase64(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer())
  let binary = ''
  // Chunked: one String.fromCharCode call per byte array would overflow the stack.
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  }
  return btoa(binary)
}

/** True inside an Android app that embeds a WebView (an in-app browser).
 *  Android marks a WebView's user agent with `; wv)`. Such a browser saves
 *  nothing for an `<a download>` click itself: it hands the URL to the host
 *  app, which cannot fetch a `blob:` URL, so a blob download there is silently
 *  dropped and the caller must give it an http(s) URL instead. Android Chrome
 *  has no `wv` mark and keeps the blob path. */
export function isAndroidWebView(ua: string = typeof navigator === 'undefined' ? '' : navigator.userAgent): boolean {
  return /\bAndroid\b/.test(ua) && /;\s*wv\)/.test(ua)
}
