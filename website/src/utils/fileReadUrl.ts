/** Append resolve=1 for relative paths. The backend resolves such paths
 * against KIROCREW_PROJECT_DIR; absolute and ~-paths pass through unchanged. */
function withResolve(url: string, filePath: string): string {
  return isAbsolute(filePath) ? url : url + '&resolve=1'
}

/** Is this path already absolute, i.e. NOT to be resolved against the project dir?
 *
 * Covers the Windows shapes as well as the POSIX ones: a drive-qualified path
 * (`C:\x`, `C:/x`) and a UNC path (`\\host\share\x`) are absolute, and marking
 * them `resolve=1` mislabels them. The backend currently passes drive and UNC
 * shapes through its resolver untouched, so the flag is inert today — but the
 * classification is what the caller is asserting, so it should be true. */
function isAbsolute(filePath: string): boolean {
  return /^([~/]|[A-Za-z]:[\\/]|\\\\)/.test(filePath)
}

/** Build the /api/file-read URL, appending resolve=1 for relative paths. */
export function fileReadUrl(filePath: string): string {
  return withResolve('/api/file-read?path=' + encodeURIComponent(filePath), filePath)
}

/** Build the /api/file-download URL — streams raw bytes for binary downloads.
 *
 * Use this instead of fileReadUrl when saving a file to disk. fileReadUrl
 * decodes content as UTF-8 with errors='replace', which corrupts binary
 * files (.docx, .pdf, images) by replacing non-text bytes with U+FFFD. */
export function fileDownloadUrl(filePath: string): string {
  return withResolve('/api/file-download?path=' + encodeURIComponent(filePath), filePath)
}

/** Build the /api/file-stream URL — Range-capable audio/video serving.
 *
 * Media elements need 206 Partial Content for seeking; file-read and
 * file-download cannot serve that. Only audio/video paths belong here. */
export function fileStreamUrl(filePath: string): string {
  return withResolve('/api/file-stream?path=' + encodeURIComponent(filePath), filePath)
}

/** Build the /api/file-office-preview URL — extracts plaintext from a
 * .docx / .pptx for inline preview in the file viewer.
 *
 * The backend uses `kiro_crew.doc_parser.extract_text` (defusedxml-hardened
 * ZIP+XML parser, no python-docx / python-pptx dep). Returns 415 when the
 * extension isn't previewable (.xls/.xlsx/.doc/.ppt/ODF) so the caller can
 * fall back to the download card. See `api_file_office_preview` in
 * `src/kiro_crew/dashboard/handlers/files.py`.
 *
 * Derived from fileDownloadUrl rather than restated: the two endpoints take
 * the identical query shape (path + optional resolve=1), so swapping the
 * endpoint segment keeps one owner for the construction. The swap cannot
 * collide with the encoded path value — encodeURIComponent turns its
 * slashes into %2F, so the raw endpoint string appears exactly once. */
export function fileOfficePreviewUrl(filePath: string): string {
  return fileDownloadUrl(filePath).replace('/api/file-download', '/api/file-office-preview')
}

/** Build the /api/file-office-slides URL — the rendered-slides manifest for a
 * .pptx / .ppt (LibreOffice → PDF → PNG on the gateway host, cached by content).
 *
 * Same query shape as the other file endpoints, so it is derived from
 * fileDownloadUrl like fileOfficePreviewUrl above. The response is either
 * `{status: 'ready', count, slides}` or `{status: 'unavailable', hint}` when the
 * host has no LibreOffice; see `api_file_office_slides` in
 * `src/kiro_crew/dashboard/handlers/office_slides.py`. */
export function fileOfficeSlidesUrl(filePath: string): string {
  return fileDownloadUrl(filePath).replace('/api/file-download', '/api/file-office-slides')
}

/** Build the /api/file-office-slide URL — one rendered slide (PNG), 1-based.
 *
 * `n` and the deck's content `digest` (from the manifest) are appended AFTER
 * the encoded path (and after `resolve=1` for a relative path), so the path
 * value is never split by the extra parameters. The digest is what makes the
 * URL safe to cache: an edited deck has a new digest, hence a new URL, so the
 * browser can never answer a stale slide for the new file — and the server
 * refuses a digest that no longer matches the file (409). */
export function fileOfficeSlideUrl(filePath: string, n: number, digest?: string): string {
  const base = fileDownloadUrl(filePath).replace('/api/file-download', '/api/file-office-slide') + '&n=' + String(n)
  return digest ? base + '&digest=' + encodeURIComponent(digest) : base
}
