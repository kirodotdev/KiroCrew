import { useState, useEffect, useRef, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, X, Hourglass, Package } from 'lucide-react'
import { SettingsCard, SettingsToggle, SettingsSelect, SettingsInput } from '../../components/settings'
import { Badge, Btn, FormSkeleton } from '../../components/ui'
import { api } from '../../api/client'
import { listMicrophones, getPreferredMicId, setPreferredMicId, micAudioConstraints } from '../../hooks/mic'

import { i18nT } from '../../i18n/t'
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
  language_codes?: string[]
  install_step: string
  install_detail: string
  install_error: string
  prereqs: string[]
}

const STEP_LABELS: Record<string, string> = {
  starting: 'Starting…',
  checking: 'Finding Python…',
  installing_xcode: 'Installing Xcode CLI Tools…',
  installing_brew: 'Installing Homebrew…',
  installing_python: 'Installing Python…',
  installing_ffmpeg: 'Installing ffmpeg…',
  installing_whisper: 'Installing whisper (~1.6 GB)…',
  installing_mlx: 'Installing mlx-whisper…',
  pulling: 'Pulling Docker image…',
  done: 'Done!',
  error: 'Failed',
}

const PROVIDER_LABELS: Record<string, string> = {
  whisper: 'Whisper (local)',
  mlx: 'Whisper MLX (local — Apple Silicon only)',
  transcribe: 'Transcribe (AWS)',
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
 * Speech-to-Text settings in the standard settings style. Replaces the older
 * raw-card SlackTab so the Voice page reads consistently. Covers enable, status,
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
    } catch {
      /* permission denied — device names stay hidden */
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
  const languageOptions = stt.language_codes?.length ? stt.language_codes : ['en-US']
  const stepLabel = STEP_LABELS[stt.install_step] || ''

  // Switching to Transcribe turns on streaming by default (one click to undo).
  const handleProvider = (v: string) => set(v === 'transcribe' && !stt.streaming ? { provider: v, streaming: true } : { provider: v })

  return (
    <>
      {err && (
        <div className="mb-4 bg-danger/10 border border-danger/20 rounded-lg p-3 flex items-start gap-3 animate-rise">
          <AlertTriangle className="lucide-inline shrink-0 text-danger" />
          <span className="text-sm text-danger flex-1">{err}</span>
          <button className="text-muted hover:text-text cursor-pointer bg-transparent border-none" aria-label={i18nT('pages.settings.sttSettings.dismiss_error')} onClick={() => { dismissedErrorRef.current = err; setErr(''); sttQ.refetch() }}><X className="lucide-inline" /></button>
        </div>
      )}
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
          optionLabels={['System default', ...mics.map((d, i) => d.label || `Microphone ${i + 1}`)]}
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

        <SettingsSelect label={i18nT('pages.settings.sttSettings.provider')} description={i18nT('pages.settings.sttSettings.whisper_and_mlx_run_locally_transcribe_calls_aws')} value={provider} options={providerOptions} optionLabels={providerOptions.map(p => PROVIDER_LABELS[p] || p)} onChange={handleProvider} disabled={saving} />

        {provider === 'whisper' && (
          <SettingsSelect label={i18nT('pages.settings.sttSettings.model')} description={i18nT('pages.settings.sttSettings.larger_models_are_more_accurate_but_slower_to_ru')} value={stt.model} options={Object.keys(stt.models)} optionLabels={Object.entries(stt.models).map(([n, s]) => `${n} (${s})`)} onChange={v => set({ model: v })} disabled={saving} />
        )}

        {provider === 'mlx' && (
          <SettingsSelect label={i18nT('pages.settings.sttSettings.mlx_model')} hint={i18nT('pages.settings.sttSettings.whisper_model_running_on_apple_mlx_metal_gpu_dow')} value={stt.mlx_model || ''} options={Object.keys(stt.mlx_models || {})} optionLabels={Object.entries(stt.mlx_models || {}).map(([n, s]) => `${n.replace('mlx-community/', '')} (${s})`)} onChange={v => set({ mlx_model: v })} disabled={saving} />
        )}

        {isTranscribe && (
          <SettingsToggle label={i18nT('pages.settings.sttSettings.streaming')} description={i18nT('pages.settings.sttSettings.stream_live_partial_transcripts_into_the_input_b')} checked={!!stt.streaming} onChange={v => set({ streaming: v })} disabled={saving} />
        )}

        {isTranscribe && stt.streaming && (
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
                  <span className="text-sm text-text font-medium">{stepLabel}</span>
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
