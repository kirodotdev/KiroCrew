import { useId, useRef, useState } from 'react'
import { Download, Loader2 } from 'lucide-react'
import ErrorNotice, { ErrorNoticeMenuItem, type ErrorNoticeMenuItemComponent } from './ErrorNotice'
import { downloadFileToDisk } from '../utils/fileReadUrl'
import { i18nT } from '../i18n/t'

/** Keep failures open for retry; close only when the browser receives the file.
 * Callers key this item by path so another file cannot inherit its failure. */
export default function FileDownloadMenuItem({ Item, filePath, pathLabel, onSuccess }: {
  Item: ErrorNoticeMenuItemComponent
  filePath: string
  pathLabel?: string
  onSuccess: () => void
}) {
  const errorId = useId()
  const pending = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const download = async () => {
    if (pending.current) return
    pending.current = true
    setBusy(true)
    setError(null)
    try {
      let failed = false
      await downloadFileToDisk(filePath, message => { failed = true; setError(message) })
      if (!failed) onSuccess()
    } finally {
      pending.current = false
      setBusy(false)
    }
  }
  return (
    <>
      <Item disabled={busy} title={i18nT('components.filePathMenu.download_current_file', { path: filePath })} onSelect={event => { event.preventDefault(); void download() }}>
        {busy
          ? <Loader2 size={14} className="lucide-inline shrink-0 animate-spin" aria-hidden />
          : <Download size={14} className="lucide-inline shrink-0" aria-hidden />}
        <span className="min-w-0 break-all">
          {busy
            ? <>{i18nT('components.markdownPanel.downloading')}{pathLabel && <> · {pathLabel}</>}</>
            : <>{i18nT('components.markdownPanel.download')}{pathLabel && <> · {pathLabel}</>}</>}
        </span>
      </Item>
      {error && (
        <>
          <div className="px-2 py-1.5 max-w-[260px]">
            <ErrorNotice id={errorId} variant="inline" className="whitespace-normal" message={error} onDismiss={() => setError(null)} />
          </div>
          <ErrorNoticeMenuItem Item={Item} message={error} describedBy={errorId} />
        </>
      )}
    </>
  )
}
