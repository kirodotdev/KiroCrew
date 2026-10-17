/** Object-URL download; revoke deferred a tick so the click can consume it. */
export function downloadBlob(blob: Blob, filename: string): void {
  // In the Electron shell, announce this download so the main process's
  // will-download handler auto-saves ONLY downloads the app explicitly asked
  // for (short-lived, single-use). Outside Electron the bridge is absent and
  // this is a no-op; the browser handles the download itself.
  try {
    ;(window as unknown as { kirocrew?: { expectDownload?: (name: string) => void } })
      .kirocrew?.expectDownload?.(filename)
  } catch {
    // A missing/throwing bridge must never block the download itself.
  }
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
