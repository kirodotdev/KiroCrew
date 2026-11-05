/**
 * Settings -> Browser -> More -> Engine: one row with the Browser Harness and
 * Kiro's Chromium versions and a Redownload (or Download) button.
 *
 * Installing is not a step here: the gateway installs on its own and the main
 * card reports it. This row is the repair: Redownload REPLACES a Chromium already
 * on disk (`{components: ['chromium'], refresh: true}`), for a damaged copy. A
 * job that fails while the engine is installed is reported here, because the
 * main card keeps showing a browser that still works.
 */
import { Download, Loader2 } from 'lucide-react'

import type { BrowserStatusData } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import InfoTip from '../../components/InfoTip'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'

export function BrowserEngineRow({ status, installing, installError, onInstall, onRedownload }: {
  status: BrowserStatusData
  installing: boolean
  installError: string | null
  /** Install both parts; a healthy Chromium already on disk is kept. */
  onInstall: () => void
  /** Replace Kiro's Chromium even when the copy on disk looks whole (a damaged executable). */
  onRedownload: () => void
}) {
  const job = status.install_job
  const jobRunning = installing || job?.status === 'running'
  // Only while installed: an install that never got that far is the main card's.
  const jobFailed = status.installed && !jobRunning && (job?.status === 'failed' || job?.status === 'interrupted')
  const chromium = status.chromium ?? null
  const label = i18nT('pages.settings.browserPanel.engine_label')
  const versions = i18nT('pages.settings.browserPanel.engine_versions', {
    harness: status.version ?? '—',
    chromium: chromium?.version ?? '—',
  })
  return (
    <div className="py-1.5" data-setting-label={label}>
      <div className="flex items-center gap-3">
        <div className="flex-1 min-w-0 flex flex-col gap-0.5">
          <div className="flex items-center gap-1.5">
            <span className="text-[13px] font-semibold text-text">{label}</span>
            <InfoTip text={i18nT('pages.settings.browserPanel.engine_hint')} placement="top" />
          </div>
          <span className="font-mono text-[12px] text-muted break-words" data-testid="browser-engine-versions">{versions}</span>
        </div>
        <Btn
          className="shrink-0"
          onClick={chromium?.downloaded ? onRedownload : onInstall}
          disabled={jobRunning}
          aria-busy={jobRunning || undefined}
          data-testid="browser-redownload"
        >
          {jobRunning
            ? <Loader2 size={13} className="lucide-inline animate-spin" aria-hidden />
            : <Download size={13} className="lucide-inline" aria-hidden />}
          {chromium?.downloaded
            ? i18nT('pages.settings.browserPanel.redownload')
            : i18nT('pages.settings.browserPanel.download')}
        </Btn>
      </div>
      {/* The gateway's own words for a failed job, never translated; the generic
          line only when it gave none. Hand-off on: no draft in this row. */}
      {(installError || jobFailed) && (
        <ErrorNotice
          variant="inline"
          className="mt-2"
          message={installError ?? job?.error_detail ?? i18nT('pages.settings.browserPanel.install_failed')}
          askAgent
          testId="browser-engine-install-error"
        />
      )}
    </div>
  )
}
