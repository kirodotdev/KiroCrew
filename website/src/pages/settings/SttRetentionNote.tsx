/**
 * "Is my audio kept?" for the selected speech provider: a one-line answer under
 * the provider picker, with the detail behind an info tip.
 *
 * Every provider streams: `transcribe` calls Amazon Transcribe Streaming, which
 * creates no transcription job or file in the user's account. The gateway never
 * writes live audio to disk and deletes an uploaded clip once it is transcribed;
 * the only audio copy is the browser tab's in-memory hold of a failed capture
 * (`hooks/voiceTranscriptInbox.ts`). The facts are in
 * `docs/system-specs/modules/stt-streaming.md#retention`.
 */
import InfoTip from '../../components/InfoTip'
import { i18nT } from '../../i18n/t'
import { PROVIDER_APPLE, PROVIDER_CODEX, PROVIDER_LOCAL, PROVIDER_TRANSCRIBE } from '../../lib/sttProviders'

export const AI_OPT_OUT_POLICY_URL =
  'https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_ai-opt-out.html'

export default function SttRetentionNote({ provider }: { provider: string }) {
  const onDevice = provider === PROVIDER_LOCAL || provider === PROVIDER_APPLE
  const transcribe = provider === PROVIDER_TRANSCRIBE
  const codex = provider === PROVIDER_CODEX
  if (!onDevice && !transcribe && !codex) return null
  return (
    <p className="-mt-1 mb-3 text-[13px] text-muted" data-testid="stt-retention-note">
      <span className="font-medium text-text">{i18nT('pages.settings.sttSettings.retention_title')}</span>{' '}
      <span className="inline-flex align-middle">
        <InfoTip
          placement="top"
          text={codex
            ? i18nT('pages.settings.sttSettings.retention_codex_detail')
            : onDevice
            ? i18nT('pages.settings.sttSettings.retention_on_device_detail')
            : i18nT('pages.settings.sttSettings.retention_transcribe_detail')}
        />
      </span>{' '}
      {codex
        ? i18nT('pages.settings.sttSettings.retention_codex')
        : onDevice
        ? i18nT('pages.settings.sttSettings.retention_on_device')
        : i18nT('pages.settings.sttSettings.retention_transcribe')}
      {transcribe && (
        <>
          {' '}
          <a href={AI_OPT_OUT_POLICY_URL} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
            {i18nT('pages.settings.sttSettings.retention_transcribe_opt_out')}
          </a>
        </>
      )}
    </p>
  )
}
