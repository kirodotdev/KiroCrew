import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsSelect, SettingsInput } from '../../components/settings'
import { FormSkeleton } from '../../components/ui'
import { api } from '../../api/client'
import SttSettings from './SttSettings'

type VoiceConfig = {
  enabled: boolean; provider: string; voice: string; engine: string; rate: string
  autoSpeak: boolean; aws_profile: string; region: string
  piper_binary: string; piper_model: string; piper_model_config: string; piper_length_scale: number
}

const PROVIDER_OPTIONS = ['piper', 'polly']
const PROVIDER_LABELS = ['Piper (local, offline)', 'AWS Polly (cloud)']

// Piper speed is controlled by length_scale (lower = faster). Map friendly
// labels to length_scale values; the backend consumes piper_length_scale (rate
// is a Polly-only knob and is ignored by Piper synthesis).
const PIPER_SPEED_OPTIONS = ['0.7', '0.85', '1.0', '1.15', '1.3', '1.5']
const PIPER_SPEED_LABELS = ['Fastest', 'Faster', 'Normal', 'Slower', 'Slow', 'Slowest']

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
 *    default) or AWS Polly (cloud). The field set switches with the provider.
 *  - Speech-to-Text (Whisper / MLX / Transcribe): dictation + install flow.
 * Previously split between the Chat tab (TTS + some STT) and the Slack tab
 * (STT install/model). Consolidated here so there is one place to look.
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
      setSaveError('Failed to save voice config')
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
      {saveError && (
        <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-center justify-between animate-rise">
          <span className="text-[13px] text-danger">{saveError}</span>
          <button className="text-[13px] text-danger hover:text-text cursor-pointer bg-transparent border-none" onClick={() => setSaveError('')}>Dismiss</button>
        </div>
      )}

      <SettingsSection title="Text-to-Speech">
        <SettingsCard>
          {voiceQ.isError ? (
            <div className="text-[13px] text-danger mb-2">Failed to load voice config. <button className="underline cursor-pointer bg-transparent border-none text-danger" onClick={() => voiceQ.refetch()}>Retry</button></div>
          ) : !voiceQ.isSuccess ? (
            <FormSkeleton rows={['toggle', 'field', 'field', 'field', 'field', 'field']} />
          ) : (
            <>
              <SettingsToggle label="Auto-speak Responses" description="Speak every assistant reply automatically" checked={voiceCfg.autoSpeak} onChange={v => setVoice({ autoSpeak: v, ...(v ? { enabled: true } : {}) })} disabled={voiceDisabled} />
              <SettingsSelect label="Provider" description="Piper runs locally and offline; Polly uses AWS credentials + network" value={voiceCfg.provider} options={PROVIDER_OPTIONS} optionLabels={PROVIDER_LABELS} onChange={v => setVoice({ provider: v })} disabled={voiceDisabled} />
              {isPolly ? (
                <>
                  <SettingsSelect label="Voice" description="AWS Polly voice for TTS" value={voiceCfg.voice} options={voiceOptions.map(o => o.value)} optionLabels={voiceOptions.map(o => o.label)} onChange={v => { const engines = voiceOptions.find(o => o.value === v)?.engines ?? ENGINE_OPTIONS; const patch: Partial<VoiceConfig> = { voice: v }; if (!engines.includes(voiceCfg.engine)) patch.engine = engines[0]; setVoice(patch) }} disabled={voiceDisabled} />
                  <SettingsSelect label="Engine" description="Polly engine type" value={voiceCfg.engine} options={selectedVoiceEngines} onChange={v => setVoice({ engine: v })} disabled={voiceDisabled} />
                  <SettingsSelect label="Speed" description="Speech rate" value={voiceCfg.rate} options={SPEED_OPTIONS} onChange={v => setVoice({ rate: v })} disabled={voiceDisabled} />
                  <SettingsInput label="AWS Profile (Polly)" description="AWS credentials profile for Polly" value={localProfile} onChange={setLocalProfile} onBlur={() => setVoice({ aws_profile: localProfile.trim() })} placeholder="default" disabled={voiceDisabled} />
                  <SettingsInput label="AWS Region (Polly)" description="AWS region for Polly API" value={localRegion} onChange={setLocalRegion} onBlur={() => setVoice({ region: localRegion.trim() })} placeholder="us-east-1" disabled={voiceDisabled} />
                </>
              ) : (
                <>
                  <SettingsInput label="Piper Model" description="Path to the Piper voice model (.onnx). Required — download from github.com/rhasspy/piper" value={localPiperModel} onChange={setLocalPiperModel} onBlur={() => setVoice({ piper_model: localPiperModel.trim() })} placeholder="~/piper/en_US-lessac-medium.onnx" disabled={voiceDisabled} />
                  <SettingsInput label="Piper Binary" description="Path to the piper executable. Leave blank to auto-detect on PATH or ~/piper-venv/bin/piper" value={localPiperBinary} onChange={setLocalPiperBinary} onBlur={() => setVoice({ piper_binary: localPiperBinary.trim() })} placeholder="(auto-detect)" disabled={voiceDisabled} />
                  <SettingsSelect label="Speed" description="Piper speech speed (length scale)" value={String(voiceCfg.piper_length_scale)} options={PIPER_SPEED_OPTIONS} optionLabels={PIPER_SPEED_LABELS} onChange={v => setVoice({ piper_length_scale: Number(v) })} disabled={voiceDisabled} />
                </>
              )}
            </>
          )}
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title="Speech-to-Text">
        <SttSettings />
      </SettingsSection>
    </>
  )
}
