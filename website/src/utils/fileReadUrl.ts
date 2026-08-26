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
