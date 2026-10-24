/** The question asked before a download the server will not scan.
 *
 * `/api/file-download` scans a file for credentials only up to a size limit; a
 * larger file answers 409 `large_file_unscanned` until the request confirms.
 * The download helper asks here, and the one mounted
 * `UnscannedDownloadConfirmHost` answers with a dialog. Kept free of React so
 * the helper in `fileReadUrl.ts` can ask without owning a component. */

export interface UnscannedDownloadInfo {
  /** The file's name as it will be saved. */
  name: string
  /** The file's size in bytes, as the server measured it. */
  size: number
  /** The largest size the server scans, in bytes. */
  limit: number
}

type Asker = (info: UnscannedDownloadInfo) => Promise<boolean>

let asker: Asker | null = null

/** Install the dialog that answers; returns the uninstall. */
export function setUnscannedDownloadAsker(next: Asker): () => void {
  asker = next
  return () => {
    if (asker === next) asker = null
  }
}

/** Whether the owner chose to download anyway. `false` when no dialog is mounted. */
export function confirmUnscannedDownload(info: UnscannedDownloadInfo): Promise<boolean> {
  return asker ? asker(info) : Promise.resolve(false)
}
