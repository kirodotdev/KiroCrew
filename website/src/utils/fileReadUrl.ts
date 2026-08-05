/** Build the /api/file-read URL, appending resolve=1 for relative paths. */
export function fileReadUrl(filePath: string): string {
  const resolve = !filePath.startsWith('/') && !filePath.startsWith('~')
  return '/api/file-read?path=' + encodeURIComponent(filePath) + (resolve ? '&resolve=1' : '')
}

/** Build the /api/file-download URL — streams raw bytes for binary downloads.
 *
 * Use this instead of fileReadUrl when saving a file to disk. fileReadUrl
 * decodes content as UTF-8 with errors='replace', which corrupts binary
 * files (.docx, .pdf, images) by replacing non-text bytes with U+FFFD. */
export function fileDownloadUrl(filePath: string): string {
  const resolve = !filePath.startsWith('/') && !filePath.startsWith('~')
  return '/api/file-download?path=' + encodeURIComponent(filePath) + (resolve ? '&resolve=1' : '')
}
