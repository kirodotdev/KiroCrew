import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsSelect, SettingsInput } from '../../components/settings'
import { FormSkeleton } from '../../components/ui'
import { api } from '../../api/client'
import SttSettings from './SttSettings'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
type VoiceConfig = {
  enabled: boolean; provider: string; voice: string; engine: string; rate: string
  autoSpeak: boolean; aws_profile: string; region: string
  piper_binary: string; piper_model: string; piper_model_config: string; piper_length_scale: number
}

const PROVIDER_OPTIONS = ['piper', 'polly']
/**
 * Catalog KEY per provider — not the label itself. This table is evaluated at
 * module load, so an `i18nT()` call here would freeze the boot language and
 * never re-resolve on a language switch; the lookup happens per render below.
 *
 * Keyed by the provider VALUE rather than by array position, so the pairing
 * with PROVIDER_OPTIONS cannot silently drift, and indexed inline at the
 * `i18nT()` call because that is the only shape `scripts/check-i18n-keys.mjs`
 * can resolve statically.
 */
const PROVIDER_LABEL_KEY: Record<string, string> = {
  piper: 'pages.settings.voicePanel.piper_local_offline',
  polly: 'pages.settings.voicePanel.amazon_polly_cloud',
}

// Piper speed is controlled by length_scale (lower = faster). Map friendly
// labels to length_scale values; the backend consumes piper_length_scale (rate
// is a Polly-only knob and is ignored by Piper synthesis).
const PIPER_SPEED_OPTIONS = ['0.7', '0.85', '1.0', '1.15', '1.3', '1.5']
/** Catalog KEY per length_scale value; resolved per render (see PROVIDER_LABEL_KEY). */
const PIPER_SPEED_LABEL_KEY: Record<string, string> = {
  '0.7': 'pages.settings.voicePanel.fastest',
  '0.85': 'pages.settings.voicePanel.faster',
  '1.0': 'pages.settings.voicePanel.normal',
  '1.15': 'pages.settings.voicePanel.slower',
  '1.3': 'pages.settings.voicePanel.slow',
  '1.5': 'pages.settings.voicePanel.slowest',
}

/**
 * Offline stand-in for `aws polly describe-voices` (Piper users have no AWS
 * credentials, so the catalogue is never fetched for them).
 *
 * DO NOT TRANSLATE any of these. `value` is the Polly VoiceId sent verbatim to
 * the TTS API, and `label` reproduces the exact shape built from the live
 * catalogue (`${name} (${languageCode} ${gender})`) — provider-supplied data
 * that no catalog can localise. Translating the fallback would make it disagree
 * with the online list the moment Polly is selected.
 */
const VOICE_OPTIONS_FALLBACK = [
  { value: 'Ruth', label: 'Ruth (US F)' },
  { value: 'Matthew', label: 'Matthew (US M)' },
  { value: 'Joanna', label: 'Joanna (US F)' },
  { value: 'Amy', label: 'Amy (UK F)' },
]
const ENGINE_OPTIONS = ['generative', 'neural', 'long-form', 'standard']
const SPEED_OPTIONS = ['80%', '90%', '95%', '100%', '110%', '120%', '130%', '150%']

/**
 * Voice settings — the single home for all voice config:
 *  - Text-to-Speech: spoken replies. Provider is Piper (local, offline, the
 *    default) or Amazon Polly (cloud). The field set switches with the provider.
 *  - Speech-to-Text (Whisper / MLX / Transcribe): dictation + install flow.
 */
export function VoicePanel() {
  const qc = useQueryClient()
  const [saveError, setSaveError] = useState('')
  const [localProfile, setLocalProfile] = useState('')
  const [localRegion, setLocalRegion] = useState('')
  const [localPiperBinary, setLocalPiperBinary] = useState('')
  const [localPiperModel, setLocalPiperModel] = useState('')

  // ── Text-to-Speech config (server-side) ──
  const voiceQ = useQuery<VoiceConfig>({ queryKey: ['voiceConfig'], queryFn: () => api.voiceConfig() })
  type PollyVoice = { id: string; name: string; language: string; languageCode: string; gender: string; engines: string[] }
  // Only fetch the Polly voice catalogue (aws polly describe-voices) when Polly
  // is the active provider — Piper users have no AWS CLI/credentials.
  const voicesQ = useQuery<{ voices: PollyVoice[] }>({ queryKey: ['voiceVoices'], queryFn: () => api.voiceVoices(), staleTime: 3600_000, enabled: voiceQ.data?.provider === 'polly' })

  const initializedRef = useRef(false)
  useEffect(() => {
    if (voiceQ.data && !initializedRef.current) {
      initializedRef.current = true
      setLocalProfile(voiceQ.data.aws_profile || '')
      setLocalRegion(voiceQ.data.region || '')
      setLocalPiperBinary(voiceQ.data.piper_binary || '')
      setLocalPiperModel(voiceQ.data.piper_model || '')
    }
  }, [voiceQ.data])

  const voiceCfg = voiceQ.data ?? { enabled: false, provider: 'piper', voice: 'Ruth', engine: 'generative', rate: '100%', autoSpeak: false, aws_profile: '', region: '', piper_binary: '', piper_model: '', piper_model_config: '', piper_length_scale: 1.0 }
  const isPolly = voiceCfg.provider === 'polly'
  const voiceOptions = voicesQ.data?.voices
    ? voicesQ.data.voices.map(v => ({ value: v.id, label: `${v.name} (${v.languageCode} ${v.gender[0]})`, engines: v.engines }))
    : VOICE_OPTIONS_FALLBACK.map(v => ({ ...v, engines: ENGINE_OPTIONS }))
  const selectedVoiceEngines = voiceOptions.find(v => v.value === voiceCfg.voice)?.engines ?? ENGINE_OPTIONS

  const voiceMut = useMutation({
    mutationFn: (patch: Partial<VoiceConfig>) => api.updateVoiceConfig(patch),
    onMutate: async (patch) => {
      await qc.cancelQueries({ queryKey: ['voiceConfig'] })
      const prev = qc.getQueryData<VoiceConfig>(['voiceConfig'])
      if (prev) {
        const next = { ...prev, ...patch }
        qc.setQueryData(['voiceConfig'], next)
        window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: next }))
      }
      return { prev }
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev) {
        qc.setQueryData(['voiceConfig'], ctx.prev)
        setLocalProfile(ctx.prev.aws_profile || '')
        setLocalRegion(ctx.prev.region || '')
        setLocalPiperBinary(ctx.prev.piper_binary || '')
        setLocalPiperModel(ctx.prev.piper_model || '')
        window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: ctx.prev }))
      }
      setSaveError(i18nT('pages.settings.voicePanel.failed_to_save_voice_config'))
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ['voiceConfig'] }),
  })

  // Controls only render in the voiceQ.isSuccess branch, so gate on the save
  // mutation instead — disables the fields briefly during a save to avoid
  // double-submits.
  const voiceDisabled = voiceMut.isPending
  const setVoice = (patch: Partial<VoiceConfig>) => voiceMut.mutate(patch)

  return (
    <>
      <ErrorNotice message={saveError} onDismiss={() => setSaveError('')} className="mb-4 animate-rise" />

      <SettingsSection title={i18nT('pages.settings.voicePanel.text_to_speech')}>
        <SettingsCard>
          {voiceQ.isError ? (
            <div className="text-[13px] text-danger mb-2">{i18nT('pages.settings.voicePanel.failed_to_load_voice_config')} <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => voiceQ.refetch()}>{i18nT('pages.settings.voicePanel.retry')}</button></div>
          ) : !voiceQ.isSuccess ? (
            <FormSkeleton rows={['toggle', 'field', 'field', 'field', 'field', 'field']} />
          ) : (
            <>
              <SettingsToggle label={i18nT('pages.settings.voicePanel.auto_speak_responses')} description={i18nT('pages.settings.voicePanel.speak_every_assistant_reply_automatically')} checked={voiceCfg.autoSpeak} onChange={v => setVoice({ autoSpeak: v, ...(v ? { enabled: true } : {}) })} disabled={voiceDisabled} />
              <SettingsSelect label={i18nT('pages.settings.voicePanel.provider')} description={i18nT('pages.settings.voicePanel.piper_runs_locally_and_offline_polly_uses_aws_cr')} value={voiceCfg.provider} options={PROVIDER_OPTIONS} optionLabels={PROVIDER_OPTIONS.map(p => i18nT(PROVIDER_LABEL_KEY[p]))} onChange={v => setVoice({ provider: v })} disabled={voiceDisabled} />
              {isPolly ? (
                <>
                  <SettingsSelect label={i18nT('pages.settings.voicePanel.voice')} description={i18nT('pages.settings.voicePanel.amazon_polly_voice_for_tts')} value={voiceCfg.voice} options={voiceOptions.map(o => o.value)} optionLabels={voiceOptions.map(o => o.label)} onChange={v => { const engines = voiceOptions.find(o => o.value === v)?.engines ?? ENGINE_OPTIONS; const patch: Partial<VoiceConfig> = { voice: v }; if (!engines.includes(voiceCfg.engine)) patch.engine = engines[0]; setVoice(patch) }} disabled={voiceDisabled} />
                  <SettingsSelect label={i18nT('pages.settings.voicePanel.engine')} description={i18nT('pages.settings.voicePanel.polly_engine_type')} value={voiceCfg.engine} options={selectedVoiceEngines} onChange={v => setVoice({ engine: v })} disabled={voiceDisabled} />
                  <SettingsSelect label={i18nT('pages.settings.voicePanel.speed')} description={i18nT('pages.settings.voicePanel.speech_rate')} value={voiceCfg.rate} options={SPEED_OPTIONS} onChange={v => setVoice({ rate: v })} disabled={voiceDisabled} />
                  <SettingsInput label={i18nT('pages.settings.voicePanel.aws_profile_polly')} description={i18nT('pages.settings.voicePanel.aws_credentials_profile_for_polly')} value={localProfile} onChange={setLocalProfile} onBlur={() => setVoice({ aws_profile: localProfile.trim() })} placeholder={i18nT('pages.settings.voicePanel.default')} disabled={voiceDisabled} />
                  <SettingsInput label={i18nT('pages.settings.voicePanel.aws_region_polly')} description={i18nT('pages.settings.voicePanel.aws_region_for_polly_api')} value={localRegion} onChange={setLocalRegion} onBlur={() => setVoice({ region: localRegion.trim() })} placeholder={i18nT('pages.settings.voicePanel.us_east_1')} disabled={voiceDisabled} />
                </>
              ) : (
                <>
                  <SettingsInput label={i18nT('pages.settings.voicePanel.piper_model')} description={i18nT('pages.settings.voicePanel.path_to_the_piper_voice_model_onnx_required_down')} value={localPiperModel} onChange={setLocalPiperModel} onBlur={() => setVoice({ piper_model: localPiperModel.trim() })} placeholder={i18nT('pages.settings.voicePanel.piper_en_us_lessac_medium_onnx')} disabled={voiceDisabled} />
                  <SettingsInput label={i18nT('pages.settings.voicePanel.piper_binary')} description={i18nT('pages.settings.voicePanel.path_to_the_piper_executable_leave_blank_to_auto')} value={localPiperBinary} onChange={setLocalPiperBinary} onBlur={() => setVoice({ piper_binary: localPiperBinary.trim() })} placeholder={i18nT('pages.settings.voicePanel.auto_detect')} disabled={voiceDisabled} />
                  <SettingsSelect label={i18nT('pages.settings.voicePanel.speed')} description={i18nT('pages.settings.voicePanel.piper_speech_speed_length_scale')} value={String(voiceCfg.piper_length_scale)} options={PIPER_SPEED_OPTIONS} optionLabels={PIPER_SPEED_OPTIONS.map(v => i18nT(PIPER_SPEED_LABEL_KEY[v]))} onChange={v => setVoice({ piper_length_scale: Number(v) })} disabled={voiceDisabled} />
                </>
              )}
            </>
          )}
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.voicePanel.speech_to_text')}>
        <SttSettings />
      </SettingsSection>
    </>
  )
}
