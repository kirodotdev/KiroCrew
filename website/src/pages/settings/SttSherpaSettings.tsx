/**
 * The `sherpa_onnx` provider's settings: which model, its languages, its licence,
 * and its download, or the owner's own archive for `custom`.
 *
 * The catalog, the sizes and the licences come from `GET /api/stt/status`
 * (`sherpa`), never from this file, so a model the gateway cannot fetch is never
 * offered. The licence is shown beside the picker because some weights carry
 * non-commercial terms the owner should read before the download
 * (`docs/system-specs/modules/stt-streaming.md#sherpa-onnx-provider`).
 */
import { useState } from 'react'
import { Download } from 'lucide-react'
import { SettingsInput, SettingsSelect } from '../../components/settings'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { fmtBytes, fmtLanguageName, fmtList } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { downloadLabel, downloadRatio } from '../../lib/sttProviders'

/** One catalog row, as served under `sherpa.models`. */
export interface SherpaModelRow {
  name: string
  /** ISO 639-1 codes of the languages the model recognises. */
  languages: string[]
  /** What the archive transfer costs. */
  download_bytes: number
  /** What the model occupies once installed. */
  size_bytes: number
  present: boolean
  /** SPDX identifier of the weights' licence; '' when the source states none. */
  license: string
  source_url: string
}

/** `GET /api/stt/status` → `sherpa`. */
export interface SherpaStatus {
  model: string
  present: boolean
  models: SherpaModelRow[]
  download: {
    step: string
    model: string
    downloaded_bytes: number
    total_bytes: number
    error: string
  }
  /** The owner's archive for `custom`: where it goes and how its last check went. */
  custom?: {
    dir: string
    archive_present: boolean
    check: { step: string; code: string; error: string }
  }
}

export const SHERPA_DOWNLOAD_RUNNING = 'downloading'
const SHERPA_DOWNLOAD_FAILED = 'failed'
/** `stt.sherpa_model` for the owner's own archive, mirroring `sherpa_custom.CUSTOM_MODEL`. */
export const SHERPA_CUSTOM_MODEL = 'custom'
/** Where k2-fsa publishes the offline transducer archives, linked from step 1. */
const SHERPA_RELEASE_URL = 'https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models'

interface Props {
  /** The stored `stt.sherpa_model`. */
  model: string
  status?: SherpaStatus
  /** The owner-only custom pair; undefined for a caller the config endpoint withheld it from. */
  customArchive?: string
  customSha256?: string
  disabled?: boolean
  downloadPending?: boolean
  onSave: (patch: { sherpa_model?: string; sherpa_custom_archive?: string; sherpa_custom_sha256?: string }) => void
  onDownload: (model: string) => void
}

function languagesLabel(codes: string[]): string {
  return codes.length
    ? fmtList(codes.map(code => fmtLanguageName(code)))
    : i18nT('pages.settings.sttSettings.sherpa_languages_model_decides')
}

export default function SttSherpaSettings({
  model,
  status,
  customArchive,
  customSha256,
  disabled,
  downloadPending,
  onSave,
  onDownload,
}: Props) {
  const [archiveDraft, setArchiveDraft] = useState(customArchive ?? '')
  const [shaDraft, setShaDraft] = useState(customSha256 ?? '')
  const rows = status?.models ?? []
  if (!rows.length) return null
  // Offered only to the owner: the config endpoint serves the custom pair to the
  // owner alone, and only the owner may write it.
  const canCustom = customArchive !== undefined
  const isCustom = model === SHERPA_CUSTOM_MODEL && canCustom
  const selected = isCustom
    ? undefined
    : rows.find(r => r.name === model) ?? rows.find(r => r.name === status?.model) ?? rows[0]
  const download = status?.download
  const running = !!selected && download?.step === SHERPA_DOWNLOAD_RUNNING && download.model === selected.name
  const failed = !!selected && download?.step === SHERPA_DOWNLOAD_FAILED && download.model === selected.name && !!download.error
  const options = [...rows.map(r => r.name), ...(canCustom ? [SHERPA_CUSTOM_MODEL] : [])]
  const optionLabels = [
    ...rows.map(r => i18nT('pages.settings.sttSettings.sherpa_model_option', {
      name: r.name,
      languages: languagesLabel(r.languages),
      size: fmtBytes(r.download_bytes),
    })),
    ...(canCustom ? [i18nT('pages.settings.sttSettings.sherpa_model_custom')] : []),
  ]
  const check = status?.custom?.check
  return (
    <>
      <SettingsSelect
        configKey="stt.sherpa_model"
        label={i18nT('pages.settings.sttSettings.sherpa_model')}
        hint={i18nT('pages.settings.sttSettings.sherpa_model_desc', {
          languages: selected ? languagesLabel(selected.languages) : languagesLabel([]),
        })}
        value={isCustom ? SHERPA_CUSTOM_MODEL : selected?.name ?? ''}
        options={options}
        optionLabels={optionLabels}
        onChange={value => onSave({ sherpa_model: value })}
        disabled={disabled}
      />
      {selected && (
        <p className="-mt-1 mb-2 text-[12px] text-muted" data-testid="stt-sherpa-license">
          {selected.license
            ? i18nT('pages.settings.sttSettings.sherpa_license_named', { license: selected.license })
            : i18nT('pages.settings.sttSettings.sherpa_license_unstated')}{' '}
          <a href={selected.source_url} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
            {i18nT('pages.settings.sttSettings.sherpa_license_link')}
          </a>
        </p>
      )}
      {isCustom ? (
        <div className="mb-2" data-testid="stt-sherpa-custom">
          <ol className="mb-2 ml-4 list-decimal text-[12px] text-muted space-y-0.5">
            <li>
              {i18nT('pages.settings.sttSettings.sherpa_custom_step_download')}{' '}
              <a href={SHERPA_RELEASE_URL} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
                {i18nT('pages.settings.sttSettings.sherpa_custom_release_link')}
              </a>
            </li>
            <li>{i18nT('pages.settings.sttSettings.sherpa_custom_step_place', { dir: status?.custom?.dir ?? '' })}</li>
            <li>{i18nT('pages.settings.sttSettings.sherpa_custom_step_fill')}</li>
          </ol>
          <SettingsInput
            configKey="stt.sherpa_custom_archive"
            label={i18nT('pages.settings.sttSettings.sherpa_custom_archive')}
            value={archiveDraft}
            onChange={setArchiveDraft}
            onBlur={() => { if (archiveDraft.trim() !== (customArchive ?? '')) onSave({ sherpa_custom_archive: archiveDraft.trim() }) }}
            placeholder="sherpa-onnx-zipformer-korean-2024-06-24.tar.bz2"
            disabled={disabled}
          />
          <SettingsInput
            configKey="stt.sherpa_custom_sha256"
            label={i18nT('pages.settings.sttSettings.sherpa_custom_sha256')}
            value={shaDraft}
            onChange={setShaDraft}
            onBlur={() => { if (shaDraft.trim() !== (customSha256 ?? '')) onSave({ sherpa_custom_sha256: shaDraft.trim() }) }}
            placeholder="shasum -a 256 <file>"
            disabled={disabled}
          />
          {status?.custom && !status.custom.archive_present ? (
            <p className="text-[12px] text-muted" data-testid="stt-sherpa-custom-missing">
              {i18nT('pages.settings.sttSettings.sherpa_custom_not_found', { dir: status.custom.dir })}
            </p>
          ) : !status?.present ? (
            <Btn onClick={() => onDownload(SHERPA_CUSTOM_MODEL)} disabled={disabled || downloadPending || check?.step === 'unpacking'}>
              {i18nT('pages.settings.sttSettings.sherpa_custom_prepare')}
            </Btn>
          ) : null}
          {check?.step === 'failed' && check.error && (
            <ErrorNotice
              variant="inline"
              className="mt-1 mb-1"
              actionPlacement="below-on-phone"
              message={check.error}
              askAgent
              testId="stt-sherpa-custom-error"
            />
          )}
        </div>
      ) : running && download ? (
        <div className="-mt-1 mb-1 animate-rise" aria-live="polite" data-testid="stt-sherpa-progress">
          <p className="text-[12px] text-muted mb-1.5">
            {downloadLabel({ done: download.downloaded_bytes, total: download.total_bytes })}
          </p>
          <div className="h-1.5 bg-border rounded-full overflow-hidden">
            <div
              className="h-full bg-accent rounded-full transition-all duration-500"
              style={{ width: `${Math.round(downloadRatio({ done: download.downloaded_bytes, total: download.total_bytes }) * 100)}%` }}
            />
          </div>
        </div>
      ) : selected && !selected.present ? (
        <div className="-mt-1 mb-1 flex flex-col gap-1.5 items-start">
          <p className="text-[12px] text-muted">
            {i18nT('pages.settings.sttSettings.model_download_prompt', { size: fmtBytes(selected.download_bytes) })}
          </p>
          <Btn onClick={() => onDownload(selected.name)} disabled={disabled || downloadPending}>
            <Download className="lucide-inline" /> {i18nT('pages.settings.sttSettings.download_model')}
          </Btn>
        </div>
      ) : null}
      {failed && download && (
        <ErrorNotice
          variant="inline"
          className="-mt-1 mb-1"
          actionPlacement="below-on-phone"
          message={i18nT('pages.settings.sttSettings.download_failed_reason', { error: download.error })}
          askAgent
          testId="stt-sherpa-download-error"
        />
      )}
    </>
  )
}
