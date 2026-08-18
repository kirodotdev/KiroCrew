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
