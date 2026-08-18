/** Append resolve=1 for relative paths. The backend resolves such paths
 * against KIROCREW_PROJECT_DIR; absolute and ~-paths pass through unchanged. */
function withResolve(url: string, filePath: string): string {
  const relative = !filePath.startsWith('/') && !filePath.startsWith('~')
  return relative ? url + '&resolve=1' : url
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
