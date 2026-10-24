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
