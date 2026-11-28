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

/** The name Chromium saves a download under when the link or the server names
 *  it *filename*, so the announced name is the one the desktop app's
 *  will-download handler sees; any other name misses the match, and the
 *  download opens a Save dialog instead of saving. These are Chromium's
 *  download-name rules (`net::GenerateFileName`, Chromium 152 in Electron 44),
 *  in its order, the Windows ones only on Windows.
 *  shortcut: two kinds of blob download still open the Save dialog. A name
 *  left with no extension gets one from the blob's type (`Makefile` as
 *  `text/plain` saves as `Makefile.txt`), which the OS's type table decides;
 *  the gateway serves a file of unknown type as `application/octet-stream`,
 *  which gets none. And the link hands this name back to Chromium, which
 *  rewrites it again when it is only `_` and `-` (`?` or `~`, saved as
 *  `download`) or, on Windows, when a replaced character left an 8.3-shaped
 *  name with a `~` (` a~1.txt`). */
export function chromiumSavedName(
  filename: string,
  windows: boolean = typeof navigator !== 'undefined' && /^Win/.test(navigator.platform),
): string {
  let name = filename
  if (windows) {
    // Windows drops trailing dots and spaces; Chromium writes a `_` for each.
    const kept = name.replace(/[ .]+$/, '').replace(/\p{White_Space}+$/u, '')
    name = kept && kept.padEnd(name.length, '_')
  }
  name = name.replace(/^\.+|\.+$/g, '').replace(/[/\\]/g, '_')
  if (!/[^-_]/.test(name)) return 'download'
  // Windows may give an 8.3-shaped name a `~1` short name, so its `~` goes.
  const shortName = windows && /^[^.]{1,8}(\.[^.]{0,3})?$/.test(name)
    && !/[\p{White_Space}"\\/\[\]:+|<>=;?,*]/u.test(name)
  name = name
    .replace(/[\u0000-\u001f\u007f-\u009f"*/:<>?\\|\p{Cf}\p{Noncharacter_Code_Point}]/gu, '_')
    .replace(/^[\p{White_Space}.~]|[\p{White_Space}.~]$/gu, '_')
  if (shortName) name = name.replace(/~/g, '_')
  if (!windows) return name
  name = name.replace(/\.(?:local|lnk|scf|url|\{[^.]*\})$/i, '.download')
  return /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9]|clock\$)(?:\.|$)|^(?:desktop\.ini|thumbs\.db)$/i.test(name)
    ? `_${name}`
    : name
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
  const name = chromiumSavedName(filename)
  announceDownload(name)
  const url = URL.createObjectURL(blob)
  clickDownload(url, name)
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/** Download a same-origin URL straight to disk, so the browser streams the
 *  response instead of holding it in page memory the way `downloadBlob` must. */
export function downloadUrl(url: string, filename: string): void {
  const name = chromiumSavedName(filename)
  announceDownload(name)
  clickDownload(url, name)
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
