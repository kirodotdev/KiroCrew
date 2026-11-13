import { useRef, useState } from 'react'
import { Download } from 'lucide-react'
import { IconButton } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useConfirm } from '../../components/ConfirmDialog'
import { saveBlob } from '../../utils/download'
import { i18nT } from '../../i18n/t'
import {
  buildDashboardDownload,
  DashboardDownloadError,
  downloadFileName,
  type DashboardDownloadInput,
} from './dashboardDownloadFile'

/** The Dashboard tab's Download button, and the notice it shows when a save fails.
 *
 * The confirm comes first, every time: the file carries whatever the bot put on its
 * page, and once it is saved it can be sent anywhere. */
export default function CrewDashboardDownload({ name, input, ready, className }: {
  /** The bot's display name, for the confirm and the file name. */
  name: string
  /** What to save, read when the save is confirmed rather than when the button was
   *  drawn: values pushed into the page since then are in it, the button is not. */
  input: () => DashboardDownloadInput | null
  /** False while the visible document does not match the current export input. */
  ready: boolean
  /** Classes for the button alone, so a host can float or hide it without hiding
   *  the failure notice beside it. */
  className?: string
}) {
  const { confirm, confirmDialog } = useConfirm()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The confirm is awaited, so the click handler outlives its render: read the
  // host's newest getter, not the one this click closed over.
  const inputRef = useRef(input)
  inputRef.current = input
  const readyRef = useRef(ready)
  readyRef.current = ready

  const onClick = async () => {
    setError(null)
    const ok = await confirm({
      title: i18nT('pages.membersPage.dashboard_download_confirm_title'),
      body: i18nT('pages.membersPage.dashboard_download_confirm_body', { name }),
      confirmLabel: i18nT('pages.membersPage.dashboard_download_confirm'),
      danger: false,
      primary: true,
    })
    if (!ok || !readyRef.current) return
    setBusy(true)
    try {
      const current = inputRef.current()
      if (!current) return
      const blob = await buildDashboardDownload(current)
      await saveBlob(blob, downloadFileName(name))
    } catch (e) {
      setError(
        e instanceof DashboardDownloadError && e.reason === 'too_large'
          ? i18nT('pages.membersPage.dashboard_download_too_large')
          : i18nT('pages.membersPage.dashboard_download_failed'),
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      {error && (
        <>
          {/* No hand-off: this tab sits on the Members page, which holds unsaved
              Profile and crew-editor drafts; the hand-off navigates to /chat and
              unmounts them, as at CrewDynamicDashboard's isError branch. Dismissible,
              because it floats over the page rather than taking a row. */}
          <ErrorNotice
            message={error}
            className="flex-1 min-w-0"
            onDismiss={() => setError(null)}
            testId="crew-dashboard-download-error"
          />
        </>
      )}
      <IconButton
        onClick={() => void onClick()}
        disabled={busy || !ready}
        // Stays shown while a save runs, so the button does not vanish mid-save.
        className={busy ? `${className ?? ''} opacity-100` : className}
        aria-label={i18nT('pages.membersPage.dashboard_download')}
        data-testid="crew-dashboard-download"
      >
        <Download size={14} aria-hidden="true" />
      </IconButton>
      {confirmDialog}
    </>
  )
}
