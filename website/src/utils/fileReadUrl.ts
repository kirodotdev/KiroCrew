import { i18nT } from '../i18n/t'

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

/** Fetch a file's raw bytes through /api/file-download and hand them to the
 * browser as a save-to-disk. THE ONE transport for retrieving a project file
 * onto the machine running the dashboard — the markdown panel's Download and
 * the file-tree row's Download both call it, so the credential gate the
 * endpoint applies (a positive scan aborts with 400) sits in front of both
 * callers, and the browser-download dance has one owner.
 *
 * The failure copy lives HERE, not in each caller: both passed the identical
 * string, so the helper owns it. A credential-scan refusal is told apart from
 * every other failure by the endpoint's machine-readable `code` field
 * (`content_redacted`, the same discriminator its file-stream and upload
 * siblings emit) -- NOT by the bare 400, which the endpoint also returns for an
 * invalid or out-of-project path. So a flagged file reads "blocked by the
 * credential scan" while a rejected path reads the generic "Download failed"
 * the user may retry. Either way the refusal is surfaced through `onError`,
 * never defeated -- the bytes are not reached another way.
 */
export async function downloadFileToDisk(
  filePath: string,
  onError: (message: string) => void,
): Promise<void> {
  try {
    const res = await fetch(fileDownloadUrl(filePath))
    if (!res.ok) {
      // eslint-disable-next-line no-console -- surface download failures for diagnostics
      console.error('downloadFileToDisk failed', res.status, res.statusText)
      // The credential gate aborts a flagged file with `code: content_redacted`
      // (see api_file_download's redact() check). Key the credential message on
      // that body code, not the status: the endpoint returns 400 for
      // invalid/out-of-project paths too, and those must not read as a
      // credential accusation. A body that will not parse falls back to generic.
      let code = ''
      try { code = (await res.clone().json())?.code ?? '' } catch { /* non-JSON body */ }
      onError(i18nT(code === 'content_redacted'
        ? 'components.markdownPanel.download_blocked_credentials'
        : 'components.markdownPanel.download_failed'))
      return
    }
    const blob = await res.blob()
    const a = document.createElement('a')
    const url = URL.createObjectURL(blob)
    a.href = url
    a.download = filePath.split('/').pop() || 'download'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 2_000)
  } catch (err) {
    // eslint-disable-next-line no-console -- surface download failures for diagnostics
    console.error('downloadFileToDisk failed', err)
    onError(i18nT('components.markdownPanel.download_failed'))
  }
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
