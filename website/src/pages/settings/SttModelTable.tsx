/**
 * The speech-model table in Voice settings, and the types and wire constants it
 * shares with the rest of the panel. Its own module so `SttSettings.tsx` stays
 * under the file line cap; `SttSettings.tsx` re-exports `SttModelTable`, so that
 * path keeps working for callers.
 */
import { Fragment } from 'react'
import { Download, Trash2 } from 'lucide-react'
import { SettingsField } from '../../components/settings'
import { Badge, Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { fmtBytes } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { downloadLabel, downloadRatio } from '../../lib/sttProviders'
import { reportForError, type ErrorReport } from '../../utils/errorReport'

/** One model the recogniser can load, as served by `GET /api/stt/status`. */
export interface SttModel {
  name: string
  /** Wire size of the weights. Rendered through `fmtBytes`, never preformatted
   *  server-side, so the size follows the dashboard's language. */
  size_bytes: number
  /** Whether the file is already on the gateway's disk. */
  present: boolean
  /** Custom model only: why its file cannot be used, '' when it can. */
  error?: string
}

/** Progress of the one model transfer a gateway runs at a time. */
export interface SttDownload {
  /** `idle` | `downloading` | `ready` | `failed` | `skipped`. */
  step: string
  /** Which model this progress belongs to. The gateway runs one transfer at a
   *  time, so after a mid-download switch it can be a model nobody is looking at. */
  model: string
  downloaded_bytes: number
  total_bytes: number
  error: string
  /** A `failed` step that was a load refusing a file already on disk (a second
   *  hard link or a link on its path), not a transfer. `error` names the remedy. */
  refused?: boolean
}

export const DOWNLOAD_STEP_RUNNING = 'downloading'
export const DOWNLOAD_STEP_FAILED = 'failed'

/**
 * The `stt.model` value that selects the user's own weights instead of a catalog
 * row, mirroring `stt.models.CUSTOM_MODEL`.
 *
 * A literal here rather than a served field, unlike the catalog itself: the
 * catalog's CONTENTS are the backend's to change, but this sentinel is a wire
 * constant on the same footing as `DOWNLOAD_STEP_RUNNING` above. The option it
 * adds is safe to hardcode for the same reason — it names no artifact, so it
 * cannot go stale against what the gateway can fetch.
 */
export const CUSTOM_MODEL = 'custom'

/**
 * Byte progress for a model transfer in flight.
 *
 * A real determinate bar, not a pulse: the transfer is between 78 MB and 1.6 GB
 * and the whole reason this surface exists is that a silent download of that size
 * is indistinguishable from a hang. Percent AND absolute bytes, because percent
 * alone hides how much is left on a slow link.
 *
 * A zero `total_bytes` means the transfer has been announced but its size has not
 * been reported yet, so the bar stays at zero rather than dividing by it.
 */
function ModelDownloadProgress({ download }: { download: SttDownload }) {
  const progress = { done: download.downloaded_bytes, total: download.total_bytes }
  return (
    <div className="-mt-1 mb-1 animate-rise" aria-live="polite">
      {/* The same sentence the recording chrome shows, from the same helper: two
          copies of "downloading N of M" would be two keys a translator renders
          differently for one event. */}
      <p className="text-[12px] text-muted mb-1.5">{downloadLabel(progress)}</p>
      <div className="h-1.5 bg-border rounded-full overflow-hidden">
        <div
          className="h-full bg-accent rounded-full transition-all duration-500"
          style={{ width: `${Math.round(downloadRatio(progress) * 100)}%` }}
        />
      </div>
    </div>
  )
}

/**
 * One row per catalog model: what it is, what it costs, whether it is on disk,
 * and the actions that state allows.
 *
 * A table rather than the old dropdown because the dropdown showed the state of
 * the selected model only: a user could not see what else was installed, could
 * not fetch a model without first selecting it, and had no way to delete one.
 *
 * At most two buttons per row (Use plus Download or Remove). The selected model
 * offers no Remove: the gateway refuses it, and asking the user to pick another
 * model first keeps the selection explicit rather than letting a delete change it.
 * Only one transfer runs per gateway, so every Download waits while one is going.
 */
export function SttModelTable({ label, hint, configKey, models, selected, download, saving, preparing, removing, removeError, onDismissRemoveError, onUse, onDownload, onRemove, selectedNotice, handoffSafe }: {
  /** The settings row's caption. A prop, not a constant, so the settings
   *  registry extractor indexes this row like any other `Settings*` control. */
  label: string
  hint?: string
  configKey?: string
  models: SttModel[]
  selected: string
  download: SttDownload | undefined
  saving: boolean
  preparing: boolean
  removing: string
  /** A refused or failed Remove, shown on a row under the model it was for. */
  removeError: ModelRemoveError | null
  onDismissRemoveError: () => void
  onUse: (name: string) => void
  onDownload: (name: string) => void
  onRemove: (model: SttModel) => void
  /** A line about the selected model, shown on a full-width row under it. */
  selectedNotice?: string
  /** False while the panel holds an unsaved draft (the AWS profile or region, or
   *  the custom model's file name or digest). The agent hand-off navigates away
   *  and would discard it, so the table's error rows offer it only when true. */
  handoffSafe: boolean
}) {
  const transferRunning = download?.step === DOWNLOAD_STEP_RUNNING
  return (
    <SettingsField label={label} hint={hint} configKey={configKey}>
      <table className="table-striped w-full text-[13px] border-collapse" data-testid="stt-model-table" aria-label={label}>
        <thead>
          <tr className="hidden sm:table-row text-left text-[12px] text-muted">
            <th scope="col" className="font-medium py-1.5 pr-2">{i18nT('pages.settings.sttSettings.model_table_model')}</th>
            <th scope="col" className="hidden sm:table-cell font-medium py-1.5 pr-2">{i18nT('pages.settings.sttSettings.model_table_size')}</th>
            <th scope="col" className="font-medium py-1.5 pr-2">{i18nT('pages.settings.sttSettings.model_table_status')}</th>
            <th scope="col" className="py-1.5"><span className="sr-only">{i18nT('pages.settings.sttSettings.model_table_actions')}</span></th>
          </tr>
        </thead>
        <tbody>
          {models.map((m, index) => {
            const isSelected = m.name === selected
            // The user's own file: configuration, not a published artifact. It has
            // no size to show and nothing to download, and it is never offered for
            // removal -- the delete route is catalog-only (it 404s `custom`), and
            // deleting a file the user placed is not this panel's call.
            const isCustom = m.name === CUSTOM_MODEL
            const displayName = isCustom ? i18nT('pages.settings.sttSettings.model_option_custom') : m.name
            const sizeText = m.size_bytes > 0 ? fmtBytes(m.size_bytes) : ''
            const thisDownload = download?.model === m.name ? download : undefined
            const downloadingThis = thisDownload?.step === DOWNLOAD_STEP_RUNNING
            // `.table-striped` counts <tr>s, and a failure or refusal row is its own
            // <tr>: it would take the next stripe and shift every model below it.
            // Each model's rows therefore carry that model's stripe inline, which
            // overrides the positional rule and keeps a notice on its model's band.
            const band = { background: index % 2 ? 'var(--card-hl)' : 'transparent' }
            return (
              <Fragment key={m.name}>
                <tr style={band} className="block sm:table-row border-t border-border align-top" data-testid={`stt-model-${m.name}`} aria-current={isSelected ? 'true' : undefined}>
                  <td className="block sm:table-cell pt-2 sm:pb-2 sm:pr-2">
                    {/* No wrap: the name is what a reader scans for, and just above
                        `sm` the auto-width column broke "large-v3-turbo" at each hyphen. */}
                    <div className="font-medium text-text whitespace-nowrap" data-testid={`stt-model-${m.name}-name`}>{displayName}</div>
                    {/* Phone-first: below `sm` every row stacks its cells (name and
                        size, then status, then actions) instead of laying them side
                        by side. At 320px the pane is 288px, and the German "Nicht
                        heruntergeladen" badge plus a Download button do not fit
                        beside the name. The column header is hidden while stacked. */}
                    {sizeText && <div className="sm:hidden text-[12px] text-muted">{sizeText}</div>}
                  </td>
                  <td className="hidden sm:table-cell py-2 pr-2 text-muted whitespace-nowrap">{sizeText}</td>
                  <td className="block sm:table-cell pt-1 sm:py-2 sm:pr-2 min-w-0">
                    <div className="flex flex-wrap items-center gap-1.5">
                      {isSelected && <Badge variant="ok">{i18nT('pages.settings.sttSettings.model_state_selected')}</Badge>}
                      {downloadingThis
                        ? null
                        : m.present
                          ? <Badge variant="muted">{i18nT('pages.settings.sttSettings.model_state_installed')}</Badge>
                          // "Not downloaded" would be untrue for a file the user
                          // places themselves; an unusable one is explained under
                          // the file row below the table.
                          : isCustom
                            ? null
                            : <Badge variant="muted">{i18nT('pages.settings.sttSettings.model_state_not_downloaded')}</Badge>}
                    </div>
                    {downloadingThis && thisDownload && <div className="mt-1"><ModelDownloadProgress download={thisDownload} /></div>}
                  </td>
                  <td className="block sm:table-cell pt-1.5 pb-2 sm:py-2">
                    {/* The buttons may wrap onto their own lines, but each label stays on
                        one: "Jetzt herunterladen" split inside its button at mid widths. */}
                    <div className="flex flex-wrap justify-start sm:justify-end gap-1.5">
                      {!isSelected && (
                        <Btn className="whitespace-nowrap" onClick={() => onUse(m.name)} disabled={saving} aria-label={i18nT('pages.settings.sttSettings.model_use_named', { name: displayName })}>
                          {i18nT('pages.settings.sttSettings.model_use')}
                        </Btn>
                      )}
                      {!isCustom && !m.present && !downloadingThis && (
                        <Btn
                          className="whitespace-nowrap"
                          onClick={() => onDownload(m.name)}
                          disabled={preparing || transferRunning}
                          aria-label={i18nT('pages.settings.sttSettings.model_download_named', { name: m.name })}
                        >
                          <Download className="lucide-inline" /> {i18nT('pages.settings.sttSettings.download_model')}
                        </Btn>
                      )}
                      {!isCustom && m.present && !isSelected && (
                        <Btn
                          danger
                          className="whitespace-nowrap"
                          onClick={() => onRemove(m)}
                          disabled={removing === m.name}
                          aria-label={i18nT('pages.settings.sttSettings.model_remove_named', { name: m.name })}
                        >
                          <Trash2 className="lucide-inline" /> {i18nT('pages.settings.sttSettings.model_remove')}
                        </Btn>
                      )}
                    </div>
                  </td>
                </tr>
                {isSelected && selectedNotice && (
                  <tr style={band} className="block sm:table-row" data-testid={`stt-model-${m.name}-notice`}>
                    <td colSpan={4} className="block sm:table-cell pb-2 text-[12px] text-muted">{selectedNotice}</td>
                  </tr>
                )}
                {/* The job's own failure, in a full-width row under the model it
                    belongs to: inside the status cell it wrapped one word per line
                    on a phone. Status text with nothing editable → hand-off on. */}
                {thisDownload?.step === DOWNLOAD_STEP_FAILED && thisDownload.error && (
                  <tr style={band} className="block sm:table-row" data-testid={`stt-model-${m.name}-failed`}>
                    <td colSpan={4} className="block sm:table-cell pb-2">
                      <ErrorNotice
                        variant="inline"
                        // A custom file is checked, never downloaded, and a refused
                        // load is of a file already installed, so "choose Download
                        // now" would point at a button the row lacks; the gateway's
                        // own reason (wrong digest, missing file, a second link to
                        // remove) says what to fix instead.
                        message={isCustom || thisDownload.refused
                          ? thisDownload.error
                          : i18nT('pages.settings.sttSettings.model_download_failed_row')}
                        report={downloadFailureReport(m.name, thisDownload.error)}
                        // No hand-off while the panel's AWS or custom-model drafts are unsaved: see `handoffSafe`.
                        askAgent={handoffSafe}
                      />
                    </td>
                  </tr>
                )}
                {/* A refused or failed Remove, under the row it was for: below the
                    table, "this model" pointed at nothing. Every Remove error offers
                    the hand-off, known refusals included, per
                    `errors-use-error-notice`, except while the panel holds an unsaved
                    draft: the hand-off navigates away and would discard it.
                    No hand-off while the panel's AWS or custom-model drafts are
                    unsaved: see `handoffSafe`. A known refusal's sentence still says
                    what to do first. */}
                {removeError?.model === m.name && (
                  <tr style={band} className="block sm:table-row" data-testid={`stt-model-${m.name}-remove-error`}>
                    <td colSpan={4} className="block sm:table-cell pb-2">
                      <ErrorNotice
                        variant="inline"
                        testId="stt-model-remove-error"
                        message={removeError.message}
                        report={reportForError(removeError.error)}
                        onDismiss={onDismissRemoveError}
                        askAgent={handoffSafe}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </SettingsField>
  )
}

/** A refused or failed Remove, and the model it was for. */
export interface ModelRemoveError {
  model: string
  message: string
  error: unknown
}

/**
 * The hand-off report for a failed download. The row says in plain words that
 * the download stopped and how to retry; the gateway's own reason (byte counts, a
 * digest mismatch) is what an agent needs, so it rides here as the detail.
 * Built per render rather than journaled: it is a projection of polled status,
 * not a new failure.
 */
function downloadFailureReport(model: string, reason: string): ErrorReport {
  return {
    id: `stt-model-download-${model}`,
    at: 0,
    source: 'system',
    message: i18nT('pages.settings.sttSettings.model_download_failed_row'),
    endpoint: '/api/stt/status',
    route: window.location.pathname,
    detail: `${model}: ${reason}`,
  }
}
