/**
 * Does this drag carry files from outside the page (OS file manager, another
 * app), as opposed to the app's own in-page drag payloads (session rows,
 * folder moves, board cards)?
 *
 * During `dragenter` / `dragover` the browser hides the file list for
 * security, so `files` is empty until the drop; `types` includes `'Files'`
 * and `items[].kind === 'file'` are the signals available mid-drag. All
 * three are checked so a drop (where `files` is populated) matches too.
 */
export function carriesFiles(dataTransfer: DataTransfer | null | undefined): boolean {
  if (!dataTransfer) return false
  return dataTransfer.types?.includes('Files')
    || Array.from(dataTransfer.items ?? []).some((item) => item.kind === 'file')
    || (dataTransfer.files?.length ?? 0) > 0
}
