import { useState, useEffect, useRef, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Hourglass, Package } from 'lucide-react'
import { SettingsCard, SettingsToggle, SettingsSelect, SettingsInput } from '../../components/settings'
import { Badge, Btn, FormSkeleton } from '../../components/ui'
import { api } from '../../api/client'
import { listMicrophones, getPreferredMicId, setPreferredMicId, micAudioConstraints, reportIfMicDenied } from '../../hooks/mic'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
interface SttConfig {
  enabled: boolean
  provider: string
  model: string
  mlx_model?: string
  available: boolean
  docker_mode: boolean
  streaming?: boolean
  endpointing?: boolean
  dictation_panel?: boolean
  transcribe_region?: string
  transcribe_profile?: string
  language_code?: string
  models: Record<string, string>
  mlx_models?: Record<string, string>
  providers?: string[]
  streaming_providers?: string[]
  language_codes?: string[]
  install_step: string
  install_detail: string
  install_error: string
  prereqs: string[]
}

/**
 * Catalog KEY for each install-progress step reported by the backend
 * (`SttConfig.install_step`).
 *
 * Keys, not strings: this table is evaluated at module load, so an `i18nT()` call
 * here would freeze the boot language and never re-resolve on a language switch.
 * The lookup happens in `stepLabel()`, which runs during render. Flat `Record` of
 * full literal keys indexed inline at the `i18nT()` call — the only shape
 * `scripts/check-i18n-keys.mjs` can resolve statically.
 */
const STEP_LABEL_KEY: Record<string, string> = {
  starting: 'pages.settings.sttSettings.step_starting',
  checking: 'pages.settings.sttSettings.step_checking',
  installing_xcode: 'pages.settings.sttSettings.step_installing_xcode',
  installing_brew: 'pages.settings.sttSettings.step_installing_brew',
  installing_python: 'pages.settings.sttSettings.step_installing_python',
  installing_ffmpeg: 'pages.settings.sttSettings.step_installing_ffmpeg',
  installing_whisper: 'pages.settings.sttSettings.step_installing_whisper',
  installing_mlx: 'pages.settings.sttSettings.step_installing_mlx',
  pulling: 'pages.settings.sttSettings.step_pulling',
  done: 'pages.settings.sttSettings.step_done',
  error: 'pages.settings.sttSettings.step_error',
}

/** Localised progress label for an install step, or '' for an unknown/idle step. */
function stepLabel(step: string): string {
  // `hasOwnProperty`, not `in`: `install_step` comes off the wire, so a backend
  // reporting `toString` would otherwise resolve to an inherited
  // Object.prototype member and hand a function to i18next.
  return Object.prototype.hasOwnProperty.call(STEP_LABEL_KEY, step)
    ? i18nT(STEP_LABEL_KEY[step])
    : ''
}

/**
 * Catalog KEY for each STT provider's dropdown label. Same keys-not-strings
 * reasoning as `STEP_LABEL_KEY`; resolved by `providerLabel()` during render.
 * The provider *names* (Whisper, MLX, Transcribe) are DNT — only the
 * parenthetical qualifier is copy.
 */
const PROVIDER_LABEL_KEY: Record<string, string> = {
  whisper: 'pages.settings.sttSettings.provider_whisper',
  mlx: 'pages.settings.sttSettings.provider_mlx',
  apple: 'pages.settings.sttSettings.provider_apple',
  transcribe: 'pages.settings.sttSettings.provider_transcribe',
}

/** Localised dropdown label for a provider id, falling back to the raw id. */
function providerLabel(provider: string): string {
  // `hasOwnProperty`, not `in`: the id list comes from `SttConfig.providers`.
  return Object.prototype.hasOwnProperty.call(PROVIDER_LABEL_KEY, provider)
    ? i18nT(PROVIDER_LABEL_KEY[provider])
    : provider
}

const TERMINAL_STEPS = ['idle', 'done', 'error']
const isInstalling = (s?: SttConfig) => !!s?.install_step && !TERMINAL_STEPS.includes(s.install_step)

/** A read-only info row that lines up with SettingsToggle / SettingsField rows. */
function InfoRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <div className="text-[13px] font-semibold text-text">{label}</div>
      {children}
    </div>
  )
}

/**
 * Speech-to-Text settings in the standard settings style, so the Voice page
 * reads consistently. Covers enable, status,
 * provider, model/MLX model, streaming, language, Transcribe AWS creds, runtime,
 * and the local-install flow (Whisper / MLX / Docker image).
 */
export default function SttSettings() {
  const qc = useQueryClient()
  const [err, setErr] = useState('')
  const [localProfile, setLocalProfile] = useState('')
  const [localRegion, setLocalRegion] = useState('')

  // Microphone input-device picker (browser-local; persisted in localStorage,
  // applied via getUserMedia constraints). Device labels are blank until the
  // page has been granted mic access at least once.
  const [mics, setMics] = useState<MediaDeviceInfo[]>([])
  const [micId, setMicId] = useState(getPreferredMicId())
  const refreshMics = useCallback(async () => { setMics(await listMicrophones()) }, [])
  useEffect(() => {
    refreshMics()
    const md = navigator.mediaDevices
    md?.addEventListener?.('devicechange', refreshMics)
    return () => md?.removeEventListener?.('devicechange', refreshMics)
  }, [refreshMics])
  const micsNeedGrant = mics.length > 0 && mics.every(d => !d.label)
  const grantMicAccess = async () => {
    try {
      const s = await navigator.mediaDevices.getUserMedia(micAudioConstraints())
      s.getTracks().forEach(t => t.stop())
      refreshMics()
    } catch (e) {
      // Device names stay hidden, and this button is the user's ONLY affordance
      // for fixing that — so a denial must still reach the shell's recovery
      // route. Otherwise clicking "Allow microphone access" appears to do
      // nothing at all, forever (macOS never re-prompts after a denial).
      reportIfMicDenied(e)
    }
  }
  const changeMic = (id: string) => { setMicId(id); setPreferredMicId(id) }

  const sttQ = useQuery<SttConfig>({
    queryKey: ['sttConfig'],
    queryFn: () => api.sttConfig(),
    // Poll while an install is in progress so the progress bar advances.
    refetchInterval: q => (isInstalling(q.state.data) ? 2000 : false),
  })

  const initRef = useRef(false)
  // Tracks the last error the user dismissed so a refetch (which re-runs this
  // effect) doesn't immediately re-surface the same install_error — otherwise
  // the banner is impossible to dismiss while install_error persists.
  const dismissedErrorRef = useRef('')
  useEffect(() => {
    if (sttQ.data && !initRef.current) {
      initRef.current = true
      setLocalProfile(sttQ.data.transcribe_profile || '')
      setLocalRegion(sttQ.data.transcribe_region || '')
    }
    if (sttQ.data?.install_error && sttQ.data.install_error !== dismissedErrorRef.current) {
      setErr(sttQ.data.install_error)
    }
  }, [sttQ.data])

  const mut = useMutation({
    mutationFn: (patch: Partial<SttConfig>) => api.saveSttConfig(patch),
    onSuccess: data => qc.setQueryData(['sttConfig'], data),
    onError: (e: Error) => setErr(e.message || i18nT('pages.settings.sttSettings.failed_to_save_stt_config')),
  })
  const set = (patch: Partial<SttConfig>) => mut.mutate(patch)
  const saving = mut.isPending

  const installMut = useMutation({
    mutationFn: () => api.sttInstall(),
    onMutate: () => {
      setErr('')
      // Flip to a non-terminal step immediately so polling kicks in.
      qc.setQueryData<SttConfig>(['sttConfig'], prev => (prev ? { ...prev, install_step: 'starting' } : prev))
    },
    onSuccess: (res: { ok?: boolean; ffmpeg?: boolean }) => {
      if (res?.ok && res.ffmpeg === false) setErr(i18nT('pages.settings.sttSettings.whisper_installed_but_ffmpeg_is_missing_voice_tr'))
      qc.invalidateQueries({ queryKey: ['sttConfig'] })
    },
    onError: (e: Error) => setErr(e.message || i18nT('pages.settings.sttSettings.install_failed')),
  })

  const stt = sttQ.data
  if (!stt) return (
    <SettingsCard>
      <FormSkeleton rows={['toggle', 'info', 'field', 'field', 'field', 'info']} />
    </SettingsCard>
  )

  const installing = isInstalling(stt)
  const isTranscribe = stt.provider === 'transcribe'
  const provider = stt.provider || 'whisper'
  const providerOptions = stt.providers?.length ? stt.providers : ['whisper', 'transcribe']
  // Gate the streaming controls on the CAPABILITY, not on a provider name. The
  // backend owns the list (`stt_stream._STREAMING_PROVIDERS`) and serves it, so
  // adding a streaming provider cannot silently hide its own toggle — which is
  // exactly what happened when `apple` was added while this read `isTranscribe`.
  const streamingProviders = stt.streaming_providers?.length
    ? stt.streaming_providers
    : ['transcribe']
  const canStream = streamingProviders.includes(provider)
  const languageOptions = stt.language_codes?.length ? stt.language_codes : ['en-US']
  const installStepLabel = stepLabel(stt.install_step)

  // Moving to a streaming-capable provider turns streaming on by default (one click
  // to undo) — a provider chosen FOR its live partials should not need a second
  // step to produce any. Leaving it on a non-streaming provider would be a lie, so
  // it is also turned off when moving to one that cannot stream.
  const handleProvider = (v: string) => {
    const streams = streamingProviders.includes(v)
    if (streams && !stt.streaming) return set({ provider: v, streaming: true })
    if (!streams && stt.streaming) return set({ provider: v, streaming: false })
    return set({ provider: v })
  }

  return (
    <>
      <ErrorNotice
        message={err}
        onDismiss={() => { dismissedErrorRef.current = err; setErr(''); sttQ.refetch() }}
        className="mb-4 animate-rise"
      />
      <SettingsCard>
        <SettingsToggle label={i18nT('pages.settings.sttSettings.enabled')} description={i18nT('pages.settings.sttSettings.transcribe_voice_into_the_message_box_when_you_c')} checked={stt.enabled} onChange={v => set({ enabled: v })} disabled={saving} />

        <InfoRow label={i18nT('pages.settings.sttSettings.status')}>
          {stt.available ? <Badge variant="ok">{i18nT('pages.settings.sttSettings.ready')}</Badge> : <Badge variant="warn">{i18nT('pages.settings.sttSettings.not_installed')}</Badge>}
        </InfoRow>

        <SettingsSelect
          label={i18nT('pages.settings.sttSettings.microphone')}
          description={i18nT('pages.settings.sttSettings.input_device_used_to_capture_your_voice')}
          value={micId}
          options={['', ...mics.map(d => d.deviceId)]}
          optionLabels={[i18nT('pages.settings.sttSettings.system_default'), ...mics.map((d, i) => d.label || i18nT('pages.settings.sttSettings.microphone_2', { n: i + 1 }))]}
          onChange={changeMic}
          disabled={saving}
        />
        {micsNeedGrant && (
          <button
            type="button"
            onClick={grantMicAccess}
            className="-mt-1 mb-1 text-[12px] text-accent hover:underline cursor-pointer bg-transparent border-none p-0 self-start"
          >
            {i18nT('pages.settings.sttSettings.allow_microphone_access_to_show_device_names')}
          </button>
        )}

        <SettingsSelect label={i18nT('pages.settings.sttSettings.provider')} description={i18nT('pages.settings.sttSettings.whisper_and_mlx_run_locally_transcribe_calls_aws')} value={provider} options={providerOptions} optionLabels={providerOptions.map(providerLabel)} onChange={handleProvider} disabled={saving} />

        {provider === 'whisper' && (
          <SettingsSelect label={i18nT('pages.settings.sttSettings.model')} description={i18nT('pages.settings.sttSettings.larger_models_are_more_accurate_but_slower_to_ru')} value={stt.model} options={Object.keys(stt.models)} optionLabels={Object.entries(stt.models).map(([n, s]) => `${n} (${s})`)} onChange={v => set({ model: v })} disabled={saving} />
        )}

        {provider === 'mlx' && (
          <SettingsSelect label={i18nT('pages.settings.sttSettings.mlx_model')} hint={i18nT('pages.settings.sttSettings.whisper_model_running_on_apple_mlx_metal_gpu_dow')} value={stt.mlx_model || ''} options={Object.keys(stt.mlx_models || {})} optionLabels={Object.entries(stt.mlx_models || {}).map(([n, s]) => `${n.replace('mlx-community/', '')} (${s})`)} onChange={v => set({ mlx_model: v })} disabled={saving} />
        )}

        {canStream && (
          <SettingsToggle label={i18nT('pages.settings.sttSettings.streaming')} description={i18nT('pages.settings.sttSettings.stream_live_partial_transcripts_into_the_input_b')} checked={!!stt.streaming} onChange={v => set({ streaming: v })} disabled={saving} />
        )}

        {canStream && stt.streaming && (
          <SettingsToggle label={i18nT('pages.settings.sttSettings.endpointing')} description={i18nT('pages.settings.sttSettings.endpointing_desc')} checked={!!stt.endpointing} onChange={v => set({ endpointing: v })} disabled={saving} />
        )}

        <SettingsToggle label={i18nT('pages.settings.sttSettings.dictation_panel')} description={i18nT('pages.settings.sttSettings.show_an_animated_panel_while_recording_instead_of')} checked={stt.dictation_panel !== false} onChange={v => set({ dictation_panel: v })} disabled={saving} />

        <SettingsSelect label={i18nT('pages.settings.sttSettings.language')} hint={i18nT('pages.settings.sttSettings.bcp_47_language_code_for_speech_recognition')} value={stt.language_code || 'en-US'} options={languageOptions} onChange={v => set({ language_code: v })} disabled={saving} />

        {isTranscribe && (
          <>
            <SettingsInput label={i18nT('pages.settings.sttSettings.aws_profile_transcribe')} description={i18nT('pages.settings.sttSettings.aws_credentials_profile_for_transcribe_blank_def')} value={localProfile} onChange={setLocalProfile} onBlur={() => set({ transcribe_profile: localProfile.trim() })} placeholder={i18nT('pages.settings.sttSettings.default')} disabled={saving} />
            <SettingsInput label={i18nT('pages.settings.sttSettings.aws_region_transcribe')} description={i18nT('pages.settings.sttSettings.aws_region_for_transcribe')} value={localRegion} onChange={setLocalRegion} onBlur={() => set({ transcribe_region: localRegion.trim() })} placeholder={i18nT('pages.settings.sttSettings.us_east_1')} disabled={saving} />
          </>
        )}

        <InfoRow label={i18nT('pages.settings.sttSettings.runtime')}>
          <span className="text-[13px] font-mono text-muted">{stt.docker_mode ? i18nT('pages.settings.sttSettings.docker') : i18nT('pages.settings.sttSettings.native')}</span>
        </InfoRow>

        {!stt.available && (
          <div className="mt-2">
            {stt.prereqs?.length > 0 && !installing && (
              <div className="mb-3 bg-accent/10 border border-accent/20 rounded-lg p-3 animate-rise">
                <p className="text-sm text-text font-medium mb-2">{i18nT('pages.settings.sttSettings.run_these_commands_in_your_terminal_first')}</p>
                {stt.prereqs.map((cmd, i) => (
                  <code key={i} className="block bg-bg-elevated rounded px-3 py-1.5 text-[13px] font-mono text-accent mb-1 select-all">{cmd}</code>
                ))}
                <p className="text-muted text-[13px] mt-2">{i18nT('pages.settings.sttSettings.then_click_install_below')}</p>
              </div>
            )}
            {installing ? (
              <div className="animate-rise">
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-[13px] animate-pulse"><Hourglass className="lucide-inline" /></span>
                  <span className="text-sm text-text font-medium">{installStepLabel}</span>
                </div>
                {stt.install_detail && <p className="text-muted text-[13px] font-mono truncate">{stt.install_detail}</p>}
                <div className="mt-2 h-1.5 bg-border rounded-full overflow-hidden">
                  <div className="h-full bg-accent rounded-full transition-all duration-500 animate-pulse"
                    style={{ width: stt.install_step === 'checking' ? '10%' : stt.install_step === 'installing_xcode' ? '15%' : stt.install_step === 'installing_brew' ? '25%' : stt.install_step === 'installing_python' ? '35%' : stt.install_step === 'installing_ffmpeg' ? '50%' : stt.install_step === 'installing_whisper' || stt.install_step === 'pulling' ? '70%' : '5%' }} />
                </div>
              </div>
            ) : (
              <>
                <Btn onClick={() => installMut.mutate()}>
                  {stt.docker_mode
                    ? <><Package className="lucide-inline" /> {i18nT('pages.settings.sttSettings.pull_docker_image')}</>
                    : provider === 'mlx'
                      ? <><Package className="lucide-inline" /> {i18nT('pages.settings.sttSettings.install_mlx_whisper')}</>
                      : <><Package className="lucide-inline" /> {i18nT('pages.settings.sttSettings.install_whisper')}</>}
                </Btn>
                <p className="text-muted text-[13px] mt-2">
                  {stt.docker_mode
                    ? i18nT('pages.settings.sttSettings.pulls_python_3_11_slim_for_docker_based_transcri')
                    : provider === 'mlx'
                      ? i18nT('pages.settings.sttSettings.installs_mlx_whisper_via_pipx_ffmpeg_apple_silic')
                      : i18nT('pages.settings.sttSettings.installs_openai_whisper_ffmpeg_uses_system_pytho')}
                </p>
              </>
            )}
          </div>
        )}
      </SettingsCard>
    </>
  )
}
