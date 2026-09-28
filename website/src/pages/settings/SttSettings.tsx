import { useState, useEffect, useRef, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Download, Sparkles } from 'lucide-react'
import { SettingsCard, SettingsToggle, SettingsSelect, SettingsInput, SettingsButtonGroup, SettingsSection, SettingsStepper } from '../../components/settings'
import { Badge, Btn, FormSkeleton } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import { api, ApiError } from '../../api/client'
import { RestartGatewayButton } from './AboutPanel'
import { listMicrophones, getPreferredMicId, setPreferredMicId, acquireMicStream, reportIfMicDenied } from '../../hooks/mic'
import { fmtBytes, fmtNumber, fmtUnit } from '../../i18n/format'
import {
  CATALOG_MODEL_PROVIDERS,
  decoderDownloadLabel,
  decoderRepairPrompt,
  downloadLabel,
  downloadRatio,
  FALLBACK_PROVIDERS,
  FALLBACK_STREAMING_PROVIDERS,
  PROVIDER_LOCAL,
  PROVIDER_TRANSCRIBE,
  providerLabel,
  unavailableMessage,
} from '../../lib/sttProviders'
import { sendErrorToChat } from '../../utils/errorReport'
import { PttTestStrip } from '../../components/PttTestStrip'
import AwsConsentGate from '../../components/AwsConsentGate'
import {
  BARE_CODE_LABEL_KEY,
  BARE_CODE_LABEL_KEY_OTHER,
  PTT_COPY_KEY,
  bindingLabel,
  clampHoldMs,
  defaultBinding,
  HOLD_MS_STEP,
  isBareModifier,
  IS_MAC,
  loadPttConfig,
  type PttMode,
  savePttConfig,
  SELECTABLE_BARE_CODES,
  toSeconds,
} from '../../lib/pushToTalk'

import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
import { errMessage } from '../../utils/thunkError'

interface SttConfig {
  enabled: boolean
  provider: string
  model: string
  available: boolean
  streaming?: boolean
  silence_ms?: number
  partial_interval_ms?: number
  endpointing?: boolean
  dictation_panel?: boolean
  transcribe_region?: string
  transcribe_profile?: string
  language_code?: string
  /** Whether a fast model tidies the finished transcript. Off unless turned on. */
  polish?: boolean
  providers?: string[]
  streaming_providers?: string[]
  language_codes?: string[]
  prereqs: string[]
  transcribe_unsupported?: boolean
  bundled_interpreter?: boolean
  ffmpeg_missing?: boolean
}

/** One model the recogniser can load, as served by `GET /api/stt/status`. */
interface SttModel {
  name: string
  /** Wire size of the weights. Rendered through `fmtBytes`, never preformatted
   *  server-side, so the size follows the dashboard's language. */
  size_bytes: number
  /** Whether the file is already on the gateway's disk. */
  present: boolean
}

/** Progress of the one model transfer a gateway runs at a time. */
interface SttDownload {
  /** `idle` | `downloading` | `ready` | `failed` | `skipped`. */
  step: string
  /** Which model this progress belongs to. The gateway runs one transfer at a
   *  time, so after a mid-download switch it can be a model nobody is looking at. */
  model: string
  downloaded_bytes: number
  total_bytes: number
  error: string
}

/** Progress of the one decoder fetch a gateway runs at a time. */
interface FfmpegDownload {
  /** `idle` | `downloading` | `ready` | `failed` | `unsupported`. */
  stage: string
  /** Which pinned executable this progress belongs to. */
  artifact: string
  downloaded_bytes: number
  total_bytes: number
  /** Machine-readable failure reason; '' unless `stage` is `failed`. */
  error_code: string
  /** The backend's own sentence for that failure, carried into the agent hand-off. */
  error_detail: string
}

/**
 * The decoder every compressed recording goes through, as served by
 * `GET /api/stt/status`.
 *
 * `os` / `arch` are the GATEWAY's, not the browser's: a dashboard open on a
 * laptop can be driving a gateway on another machine, and the hand-off prompt has
 * to name the host that actually needs fixing.
 */
interface SttFfmpeg {
  present: boolean
  /** `bundled` | `system` | `store`, or null when no decoder resolves. */
  source: string | null
  /** `available` | `unsupported` | `bundled` — whether a fetch can fix this host. */
  auto_fetch: string
  os: string
  arch: string
  download: FfmpegDownload
}

/**
 * What the installed speech build actually links, as read out of whisper.cpp's own
 * `whisper_print_system_info()` by `stt/capabilities.py`.
 *
 * This is the one surface that can contradict a hopeful setting. The native default
 * for a whisper context is `use_gpu = true` on every build including CPU-only ones,
 * so "GPU requested" says nothing about whether one is linked — only this does.
 */
interface SttBackend {
  /** The strongest linked backend, or `unknown` when the build could not be read. */
  name: string
  /** Whether anything faster than scalar CPU is linked. False when `unknown`. */
  accelerated: boolean
  /** True when only the encoder is accelerated, so the decoder still runs on CPU. */
  encoder_only: boolean
  /**
   * The ggml backend registries the build linked, in order -- the section labels of
   * `whisper_print_system_info()`. This is where `name` comes from: every backend
   * except CoreML/OpenVINO/VITISAI is named ONLY by its label, which is why an
   * reading the flags alone reports every Mac as CPU-only.
   */
  sections?: string[]
  /** Decode threads in effect. */
  threads: number
}

/** One stage-timed decode. Durations only; see `stt/telemetry.py`. */
interface SttDecodeSample {
  kind: string
  audio_ms: number
  wall_ms: number
  /** Wall time over audio duration. Above 1.0 the recogniser is slower than speech. */
  rtf: number
}

/** Stage timings for the most recent load and decodes. */
interface SttTimings {
  last_load?: { model: string; hash_ms: number; load_ms: number; first_decode_ms: number } | null
  last_final?: SttDecodeSample | null
}

interface SttStatus {
  available: boolean
  /** Machine-readable refusal reason; '' when available. See `unavailableMessage`. */
  code: string
  /** The backend's own sentence, shown only for a code this build cannot name. */
  detail: string
  models: SttModel[]
  download: SttDownload
  ffmpeg?: SttFfmpeg
  backend?: SttBackend
  timings?: SttTimings
}

const DOWNLOAD_STEP_RUNNING = 'downloading'
const DOWNLOAD_STEP_FAILED = 'failed'

/**
 * Decoder stages, which are the backend's `stage` values verbatim.
 *
 * `unsupported` is not a failure and must not be rendered as one: no pinned
 * upstream executable exists for the platform, so a retry cannot change the
 * answer and the manual system-decoder route is the only one left.
 */
const FFMPEG_STAGE_RUNNING = 'downloading'
const FFMPEG_STAGE_FAILED = 'failed'
const FFMPEG_AUTO_FETCH_AVAILABLE = 'available'

/**
 * How often the status endpoint is re-read while a model transfer runs.
 *
 * The bar is the only evidence a multi-hundred-megabyte transfer is progressing,
 * so it has to advance at a rate a person reads as motion. The poll is armed only
 * while `step` is `downloading`, so it costs nothing at rest.
 */
const DOWNLOAD_POLL_MS = 1000

/**
 * The models whose decode is slow enough that a CPU-only build is worth warning
 * about before the download, not after the first dictation.
 *
 * `large-v3-turbo` measured RTF 1.238 on a 32-core aarch64 CPU build — 11 s of
 * speech costs 13.6 s, so live partials cannot keep up and the wait after the user
 * stops is longer than what they said. It is still the right choice for multilingual
 * and code-switched speech (best of the catalog on both), which is why this warns
 * rather than hides it.
 */
const SLOW_WITHOUT_ACCELERATION = ['large-v3-turbo']

/** `Capabilities.backend` when the build could not be interrogated at all. */
const BACKEND_UNKNOWN = 'unknown'

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
 * Byte progress for the decoder fetch.
 *
 * Its own component rather than a second caller of `ModelDownloadProgress`: the
 * two transfers report through different status blocks with different field
 * names, and the caption has to say WHICH one is running — a bar labelled
 * "downloading the speech model" while the decoder is being fetched is worse than
 * no caption. The bar itself reuses `downloadRatio`, so the percentage, the byte
 * figures and the divide-by-zero guard have one owner.
 */
function DecoderDownloadProgress({ download }: { download: FfmpegDownload }) {
  const progress = { done: download.downloaded_bytes, total: download.total_bytes }
  return (
    <div className="animate-rise" aria-live="polite">
      <p className="text-[12px] text-muted mb-1.5">{decoderDownloadLabel(progress)}</p>
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
 * Catalog KEY for each push-to-talk mode's option label and its explainer.
 *
 * Keys, not strings, and module scope, not inside the component: an `i18nT()`
 * call evaluated at import would freeze the boot language, and a table rebuilt
 * on every render is pointless churn. Flat `Record`s indexed inline at the
 * `i18nT()` call — the shape `scripts/check-i18n-keys.mjs` resolves statically,
 * so these sites stay OUT of the dynamic-key population the gate cannot check.
 *
 * The explainer is per-mode rather than one sentence for the row because the
 * option names cannot carry the semantics: a first-run reader seeing "Both" cold
 * had no way to learn what it combined.
 */
const PTT_MODE_LABEL_KEY: Record<PttMode, string> = {
  toggle: 'pages.settings.sttSettings.ptt_mode_toggle',
  ptt: 'pages.settings.sttSettings.ptt_mode_hold',
  hybrid: 'pages.settings.sttSettings.ptt_mode_hybrid',
}
const PTT_MODE_DESC_KEY: Record<PttMode, string> = {
  toggle: 'pages.settings.sttSettings.ptt_mode_desc_toggle',
  ptt: 'pages.settings.sttSettings.ptt_mode_desc_hold',
  hybrid: 'pages.settings.sttSettings.ptt_mode_desc_hybrid',
}
const PTT_MODES: readonly PttMode[] = ['toggle', 'ptt', 'hybrid']

/**
 * Push-to-talk binding editor: which key, how the key behaves, the tap/hold
 * cutoff, and the live test strip.
 *
 * State is BROWSER-LOCAL (see `lib/pushToTalk`), so this block writes
 * localStorage and does not go through the STT config mutation — the right key
 * depends on the keyboard in front of you, and pushing one machine's choice to
 * every other device would be wrong.
 *
 * Row ORDER is deliberate and was corrected after a first-run review: key first,
 * then behaviour, then the cutoff, then the test. Asking "how should the key
 * behave" before "which key" is unanswerable, and the strip's own prompt
 * ("Press your shortcut key") already assumes the key has been chosen.
 *
 * The `custom` chord recorder is deliberately NOT here yet: the bare modifiers
 * cover the platform defaults and every key these apps converge on, and a
 * recorder is a separate surface with its own capture/cancel semantics (see
 * `SearchEverywhereConfig` in ShortcutsPanel for the pattern to follow). Until
 * then a chord binding is reachable only as the non-mac default, which is why
 * the dropdown surfaces it as a read-only entry rather than hiding it.
 */
function PushToTalkConfig() {
  const [cfg, setCfg] = useState(() => loadPttConfig())
  const patch = (next: Partial<typeof cfg>) => {
    const merged = { ...cfg, ...next }
    setCfg(merged)
    savePttConfig(merged)
  }

  const bare = isBareModifier(cfg.binding)
  // A chord binding (the Windows/Linux default) has no entry in the bare-key
  // list, so it is surfaced as its own option rather than silently displaying
  // as whatever happens to sort first.
  const options = bare ? [...SELECTABLE_BARE_CODES] : ['__chord__', ...SELECTABLE_BARE_CODES]
  const recommended = defaultBinding().code
  const optionLabels = options.map(code => {
    if (code === '__chord__') return bindingLabel(cfg.binding)
    // Indexed directly per branch so the key-reference gate can resolve both.
    const name = IS_MAC ? i18nT(BARE_CODE_LABEL_KEY[code]) : i18nT(BARE_CODE_LABEL_KEY_OTHER[code])
    return code === recommended ? i18nT('pages.settings.sttSettings.ptt_key_recommended', { name }) : name
  })
  const headingDescKey = IS_MAC ? PTT_COPY_KEY.headingDescMac : PTT_COPY_KEY.headingDescOther
  const keyDescKey = IS_MAC ? PTT_COPY_KEY.keyDescMac : PTT_COPY_KEY.keyDescOther
  const keyFieldLabel = i18nT('pages.settings.sttSettings.ptt_key')
  const modeLabel = i18nT(PTT_MODE_LABEL_KEY[cfg.mode])

  return (
    /* Its own disclosure, nested inside Fine-tuning. The heading still names the
       feature -- without a name the rows below are settings for something the page
       never identifies, which was the first-run review's biggest finding -- but a
       whole sub-feature nobody has to configure should not spend five rows of the
       surface saying so. Its own copy admits as much: the default combination works
       out of the box and the text tells you to leave it alone.

       The heading's explanation moves inside, where a reader who opened this has
       already said they want it. */
    <SettingsSection title={i18nT('pages.settings.sttSettings.ptt_heading')} collapsible>
      <p className="text-[12px] text-muted mb-1">{i18nT(headingDescKey)}</p>

      <SettingsSelect
        label={i18nT('pages.settings.sttSettings.ptt_key')}
        description={i18nT(keyDescKey)}
        value={bare ? cfg.binding.code : '__chord__'}
        options={options}
        optionLabels={optionLabels}
        onChange={code => { if (code !== '__chord__') patch({ binding: { code } }) }}
      />

      {/* Right Alt is AltGr on most non-mac layouts (reports ctrl+alt and
          composes characters) and a lone left Alt reveals the window menu, so
          those platforms cannot default to a bare Option. */}
      {!IS_MAC && (
        <p className="text-[12px] text-muted my-0.5">
          {i18nT('pages.settings.sttSettings.ptt_altgr_note')}
        </p>
      )}

      {/* The description below the picker changes with the selection, because
          the option NAMES cannot carry the semantics on their own — a reviewer
          reading "Both" cold had no way to learn what it combined. */}
      <SettingsButtonGroup
        label={i18nT('pages.settings.sttSettings.ptt_mode')}
        description={i18nT(PTT_MODE_DESC_KEY[cfg.mode])}
        value={cfg.mode}
        options={PTT_MODES.map(m => ({ value: m, label: i18nT(PTT_MODE_LABEL_KEY[m]) }))}
        onChange={v => patch({ mode: v as PttMode })}
      />

      {/* Hidden rather than disabled outside hybrid: the cutoff has no meaning
          at all there. Its description names the mode that uses it, so when it
          IS shown the dependency is explicit rather than inferred from the row
          appearing and disappearing. */}
      {cfg.mode === 'hybrid' && (
        <SettingsStepper
          label={i18nT('pages.settings.sttSettings.ptt_hold_threshold')}
          description={i18nT('pages.settings.sttSettings.ptt_hold_threshold_desc')}
          value={i18nT('pages.settings.sttSettings.ptt_hold_seconds', { secs: toSeconds(cfg.holdMs) })}
          onIncrement={() => patch({ holdMs: clampHoldMs(cfg.holdMs + HOLD_MS_STEP) })}
          onDecrement={() => patch({ holdMs: clampHoldMs(cfg.holdMs - HOLD_MS_STEP) })}
        />
      )}

      <div className="flex flex-col gap-1.5 py-1.5">
        <span className="text-[13px] font-semibold text-text">{i18nT('components.pttTestStrip.title')}</span>
        <span className="text-[12px] text-muted">{i18nT('pages.settings.sttSettings.ptt_try_desc')}</span>
        <PttTestStrip
          binding={cfg.binding}
          mode={cfg.mode}
          holdMs={cfg.holdMs}
          modeLabel={modeLabel}
          fieldLabel={keyFieldLabel}
        />
      </div>
    </SettingsSection>
  )
}

/**
 * Speech-to-Text settings in the standard settings style, so the Voice page
 * reads consistently. Covers enable, availability, provider, the local model and
 * its download, streaming and its two timing knobs, language, and Transcribe's
 * AWS credentials.
 */
export default function SttSettings({ cardIndex }: {
  /** Ordinal of this component's card in the hosting panel's stagger ladder. */
  cardIndex?: number
} = {}) {
  const qc = useQueryClient()
  const [err, setErr] = useState('')
  const [localProfile, setLocalProfile] = useState('')
  const [localRegion, setLocalRegion] = useState('')

  // Microphone input-device picker (browser-local; persisted in localStorage,
  // applied via getUserMedia constraints). Device labels are blank until the
  // page has been granted mic access at least once.
  const [mics, setMics] = useState<MediaDeviceInfo[]>([])
  // Before permission, an anonymous device can share the system-default value.
  // It cannot be selected separately, so offer it through System default only.
  const selectableMics = mics.filter(device => device.deviceId !== '')
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
      const s = await acquireMicStream()
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
  })

  // Availability, the model catalog and any transfer in flight. A second query
  // rather than more fields on the config: this one POLLS during a download, and
  // the config endpoint re-reads and re-probes configuration on every read.
  const statusQ = useQuery<SttStatus>({
    queryKey: ['sttStatus'],
    queryFn: () => api.sttStatus(),
    // Either transfer keeps the poll armed. The decoder fetch runs on its own
    // store, so gating only on the model's `step` left the decoder bar frozen at
    // whatever byte count the first response happened to carry.
    refetchInterval: q =>
      q.state.data?.download?.step === DOWNLOAD_STEP_RUNNING
      || q.state.data?.ffmpeg?.download?.stage === FFMPEG_STAGE_RUNNING
        ? DOWNLOAD_POLL_MS
        : false,
  })

  const initRef = useRef(false)
  useEffect(() => {
    if (sttQ.data && !initRef.current) {
      initRef.current = true
      setLocalProfile(sttQ.data.transcribe_profile || '')
      setLocalRegion(sttQ.data.transcribe_region || '')
    }
  }, [sttQ.data])

  const mut = useMutation({
    mutationFn: (patch: Partial<SttConfig>) => api.saveSttConfig(patch),
    onSuccess: (data, patch) => {
      qc.setQueryData(['sttConfig'], data)
      // Provider, model and enablement all change what the availability probe
      // answers, so the status card would otherwise keep describing the previous
      // selection until something else happened to refetch it.
      qc.invalidateQueries({ queryKey: ['sttStatus'] })
      // A new profile/region resolves a different account; without this the gate
      // keeps the old one and Confirm 409s as a stale confirmation.
      if ('transcribe_profile' in patch || 'transcribe_region' in patch) {
        qc.invalidateQueries({ queryKey: ['awsConsent', PROVIDER_TRANSCRIBE] })
      }
    },
    onError: (e: Error) => setErr(e.message || i18nT('pages.settings.sttSettings.failed_to_save_stt_config')),
  })
  const set = (patch: Partial<SttConfig>) => mut.mutate(patch)
  const saving = mut.isPending

  const prepareMut = useMutation({
    mutationFn: (model: string) => api.sttPrepare(model),
    onMutate: () => setErr(''),
    // The transfer outlives the request, so the response only says it started.
    // Progress arrives through the polled status query.
    onSettled: () => qc.invalidateQueries({ queryKey: ['sttStatus'] }),
    onError: (e: Error) => setErr(e.message || i18nT('pages.settings.sttSettings.download_failed')),
  })
  const [restarting, setRestarting] = useState(false)
  const decoderMut = useMutation({
    mutationFn: () => api.sttFfmpegDownload(),
    onMutate: () => setErr(''),
    // Same contract as the model transfer: the response only says it started, and
    // progress arrives through the polled status query.
    onSettled: () => qc.invalidateQueries({ queryKey: ['sttStatus'] }),
    onError: (e: Error) => setErr(e.message || i18nT('pages.settings.sttSettings.download_failed')),
  })
  const restartMut = useMutation({
    mutationFn: () => api.restartGateway(),
    onSuccess: () => setRestarting(true),
    onError: (e: unknown) => {
      // Restarting normally resets the connection before a response arrives.
      // Only a structured server rejection is a real failure.
      if (e instanceof ApiError) setErr(e.message || i18nT('pages.settings.aboutPanel.restart_failed'))
      else setRestarting(true)
    },
  })

  const stt = sttQ.data
  if (!stt) return (
    // Same ordinal as the loaded card below: the success render replaces this
    // skeleton at the same position, and a differing delay would hold the
    // already-loaded content blank for the delay after the swap.
    <SettingsCard index={cardIndex}>
      {sttQ.isError ? (
        // A skeleton that never resolves is indistinguishable from a slow load.
        // Nothing to lose: no control is mounted in this branch.
        <ErrorNotice
          message={errMessage(sttQ.error) || i18nT('pages.settings.sttSettings.config_load_failed')}
          askAgent
        />
      ) : (
        <FormSkeleton rows={['toggle', 'info', 'field', 'field', 'field', 'info']} />
      )}
    </SettingsCard>
  )

  const isTranscribe = stt.provider === PROVIDER_TRANSCRIBE
  const provider = stt.provider || PROVIDER_LOCAL
  const providerOptions = stt.providers?.length ? stt.providers : FALLBACK_PROVIDERS
  // Gate the streaming controls on the CAPABILITY, not on a provider name. The
  // backend owns the list (`stt_stream._STREAMING_PROVIDERS`) and serves it, so
  // adding a streaming provider cannot silently hide its own toggle — which is
  // exactly what happened when `apple` was added while this read `isTranscribe`.
  const streamingProviders = stt.streaming_providers?.length
    ? stt.streaming_providers
    : FALLBACK_STREAMING_PROVIDERS
  const canStream = streamingProviders.includes(provider)
  const defaultLanguage = provider === PROVIDER_LOCAL ? 'auto' : 'en-US'
  const languageOptions = stt.language_codes?.length ? stt.language_codes : [defaultLanguage]

  // The catalog and the availability verdict. Treated as absent rather than as
  // "nothing to download" while the status query is in flight, so a slow probe
  // shows no model rows instead of claiming a model is missing.
  const status = statusQ.data
  const models = status?.models ?? []
  const selectedModel = models.find(m => m.name === stt.model)
  // Progress is only shown for the model on screen. The gateway runs one transfer
  // at a time, so switching models mid-download leaves the store reporting the
  // PREVIOUS model, and attributing that byte count to the new selection would
  // claim a download that has not started.
  // Optional chaining on `download` is load-bearing, not defensive habit: a status
  // body without it threw during render, and the error boundary then replaced the
  // entire settings page rather than this one card.
  const download =
    status?.download?.model === stt.model ? status?.download : undefined
  const downloading = download?.step === DOWNLOAD_STEP_RUNNING
  // Availability comes from the status endpoint when it has answered, because it
  // carries the machine-readable reason. The config's plain boolean is the
  // fallback for the window before the first status response lands.
  const available = status ? status.available : stt.available
  const unavailableText = status ? unavailableMessage(status.code, status.detail) : ''
  const usesCatalogModel = CATALOG_MODEL_PROVIDERS.includes(provider)

  // What the native build actually links, and whether the configured request for it
  // took effect. Read from the status endpoint rather than inferred from the config:
  // a whisper context defaults to `use_gpu = true` on CPU-only builds, so the
  // setting cannot answer this and only the build's own system info can.
  const backend = status?.backend
  const isLocal = provider === PROVIDER_LOCAL
  // A named backend the build does not link. Surfaced because the setting's own help
  // text promises this panel will say so, and because the alternative — quietly
  // recognising on CPU while the config reads `cuda` — is the specific wrong answer
  // this whole change exists to stop.
  // The finished-transcript cost of the last dictation, which is what tells someone
  // whether a model is viable on THEIR machine rather than on a benchmark host.
  const lastFinal = status?.timings?.last_final
  const slowModel =
    isLocal
    && !!backend
    && !backend.accelerated
    && backend.name !== BACKEND_UNKNOWN
    && SLOW_WITHOUT_ACCELERATION.includes(stt.model)

  // The acceleration, as a badge beside Status. One glanceable fact, and the only
  // one of the engine readings that changes what a user would DO -- the threads and
  // the last decode's cost answer "why is it slow", which is a question you go
  // looking for, so they belong in the tip rather than on the surface.
  const engineBadge = !isLocal || !backend ? null
    : backend.name === BACKEND_UNKNOWN
      // Never reported as CPU. An unreadable build is a build we know nothing
      // about, and calling it CPU is the answer that is wrong for exactly the user
      // who has a GPU build and would then stop expecting it to be used.
      ? <Badge variant="warn">{i18nT('pages.settings.sttSettings.backend_unknown')}</Badge>
      : backend.encoder_only
        ? <Badge variant="ok">{i18nT('pages.settings.sttSettings.backend_encoder_only', { name: backend.name })}</Badge>
        : backend.accelerated
          ? <Badge variant="ok">{backend.name}</Badge>
          // `muted`: a CPU-only build is the normal published state, not a fault.
          : <Badge variant="muted">{i18nT('pages.settings.sttSettings.backend_cpu_only')}</Badge>

  // Everything else the engine knows, as one tip. Joined rather than stacked so it
  // stays a sentence a user can skim, and each part is omitted when it has nothing
  // to say instead of rendering an empty label.
  const engineDetail = !isLocal || !backend ? '' : [
    i18nT('pages.settings.sttSettings.decode_threads_value', { n: backend.threads }),
    // Absent until someone has dictated once: an invented number is worse than
    // none. Above 1.0 the recogniser is slower than the speech it transcribes.
    lastFinal && lastFinal.rtf > 0
      ? i18nT('pages.settings.sttSettings.last_recognition_value', {
        duration: fmtUnit(lastFinal.wall_ms / 1000, 'second', { maximumFractionDigits: 1 }),
        rtf: fmtNumber(lastFinal.rtf, { maximumFractionDigits: 2 }),
      })
      : '',
  ].filter(Boolean).join(' · ')

  // The decoder block. Treated as absent until the status query answers, for the
  // same reason the model catalog is: claiming a decoder is missing before the
  // probe has run would offer a download nobody asked for on a host that has one.
  const ffmpeg = status?.ffmpeg
  const decoderDownload = ffmpeg?.download
  const decoderDownloading = decoderDownload?.stage === FFMPEG_STAGE_RUNNING
  const decoderFailed = decoderDownload?.stage === FFMPEG_STAGE_FAILED
  // The code is the contract and the sentence is advisory, so the sentence is
  // preferred for a human reader and the code is the fallback when a failure
  // carried no prose.
  const decoderFailureText = decoderDownload?.error_detail || decoderDownload?.error_code || ''
  const askAgentToFixDecoder = () => {
    if (!ffmpeg) return
    // The one prefill mechanism: stage the prompt, navigate to chat, and let the
    // user read and send it. Never a parallel channel, and never an auto-send.
    sendErrorToChat(decoderRepairPrompt({
      code: ffmpeg.download.error_code,
      detail: ffmpeg.download.error_detail,
      os: ffmpeg.os,
      arch: ffmpeg.arch,
    }))
  }

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
      {/* Only mutation failures reach here, so dismissing simply clears it. There
          is no server-held error to re-read: a failed model download reports
          itself through the status query's own `download.error`.
          askAgent is gated on the drafts (the SecretsPanel shape): the AWS
          profile / region inputs commit `onBlur`, so a failed save leaves the
          typed-but-rejected text in them — kept on purpose, so it can be
          corrected rather than retyped. No hand-off while either differs from
          the stored value: `localProfile` / `localRegion`. */}
      <ErrorNotice
        message={err}
        onDismiss={() => setErr('')}
        className="mb-4 animate-rise"
        askAgent={
          localProfile.trim() === (stt.transcribe_profile || '')
          && localRegion.trim() === (stt.transcribe_region || '')
        }
      />
      <SettingsCard index={cardIndex}>
        <SettingsToggle label={i18nT('pages.settings.sttSettings.enabled')} hint={i18nT('pages.settings.sttSettings.transcribe_voice_into_the_message_box_when_you_c')} checked={stt.enabled} onChange={v => set({ enabled: v })} disabled={saving} />

        <InfoRow label={i18nT('pages.settings.sttSettings.status')}>
          <div className="flex items-center gap-1.5">
            {available
              ? <Badge variant="ok">{i18nT('pages.settings.sttSettings.ready')}</Badge>
              : status?.code === 'stt_provider_off'
                // Nothing is missing when the provider is Off: "not installed" would
                // send the user to an install for a state they chose (or that an
                // unknown stored value was degraded onto). Neutral, and the reason
                // line below names the control.
                ? <Badge variant="muted">{i18nT('pages.settings.sttSettings.status_provider_off')}</Badge>
                : <Badge variant="warn">{i18nT('pages.settings.sttSettings.not_installed')}</Badge>}
            {/* The engine truth rides on the row a user already reads to answer
                "is this working", rather than in a section of its own. Three
                read-only numbers each given a labelled row of their own was the
                panel's problem, not its fix: the acceleration is the only one that
                changes a decision, so it is the only one shown, and the rest live
                in the tip beside it. */}
            {engineBadge}
            {engineDetail && <InfoTip text={engineDetail} />}
          </div>
        </InfoRow>
        {/* The REASON, not just the badge. Every refusal has a different remedy
            (install an extra, install a compiler, upgrade macOS, fetch a model),
            so a bare "not installed" sends the user looking for the wrong thing.
            Rendered from the backend's machine-readable `code`; see
            `lib/sttProviders.unavailableMessage`. */}
        {!available && unavailableText && (
          <p className="text-[12px] text-muted -mt-1 mb-1">{unavailableText}</p>
        )}
        {/* The probe itself failed: the badge above is then showing the config's
            plain boolean, not a verdict, and the model catalog is absent. Status
            row, nothing editable → hand-off on. */}
        {statusQ.isError && (
          <ErrorNotice
            variant="inline"
            className="-mt-1 mb-1"
            message={errMessage(statusQ.error) || i18nT('pages.settings.sttSettings.status_unavailable')}
            askAgent
          />
        )}

        <SettingsSelect
          label={i18nT('pages.settings.sttSettings.microphone')}
          hint={i18nT('pages.settings.sttSettings.input_device_used_to_capture_your_voice')}
          value={micId}
          options={['', ...selectableMics.map(d => d.deviceId)]}
          optionLabels={[i18nT('pages.settings.sttSettings.system_default'), ...selectableMics.map((d, i) => d.label || i18nT('pages.settings.sttSettings.microphone_2', { n: i + 1 }))]}
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

        <SettingsSelect label={i18nT('pages.settings.sttSettings.provider')} hint={i18nT('pages.settings.sttSettings.provider_desc')} value={provider} options={providerOptions} optionLabels={providerOptions.map(providerLabel)} onChange={handleProvider} disabled={saving} configKey="stt.provider" />

        {/* Gated on a NON-EMPTY catalog, not just on the provider: the catalog is
            the status endpoint's to serve, and a picker with no options is worse
            than no picker at all: it reads as "this model list is empty" rather
            than "the list has not arrived". */}
        {usesCatalogModel && models.length > 0 && (
          <>
            {/* Options come from the served catalog, never a list in this file:
                the sizes and the set of models are the backend's to change, and a
                hardcoded copy here would offer a model the gateway cannot load.
                The size rides in the option label so the download cost is visible
                BEFORE the click that commits to it. */}
            <SettingsSelect
              label={i18nT('pages.settings.sttSettings.model')}
              hint={i18nT('pages.settings.sttSettings.larger_models_are_more_accurate_but_slower_to_ru')}
              value={stt.model}
              options={models.map(m => m.name)}
              optionLabels={models.map(m => i18nT('pages.settings.sttSettings.model_option', { name: m.name, size: fmtBytes(m.size_bytes) }))}
              onChange={v => set({ model: v })}
              disabled={saving}
              configKey="stt.model"
            />
            {/* The cost of THIS choice on THIS machine, before the download rather
                than after the first dictation. Shown only when the build links no
                acceleration and the build was readable, so it is a measured warning
                and never a guess: on a CPU-only build `large-v3-turbo` decodes slower
                than real time, which no amount of waiting improves. Deliberately not
                a block — it is the most accurate model in the catalog for
                multilingual and code-switched speech, so someone who needs that
                accuracy should be able to accept the wait knowingly. */}
            {slowModel && (
              <p className="text-[12px] text-warn -mt-1 mb-1">
                {i18nT('pages.settings.sttSettings.model_slow_on_cpu')}
              </p>
            )}
            {downloading && download ? (
              <ModelDownloadProgress download={download} />
            ) : selectedModel?.present ? (
              // Nothing. A model already on disk needs no line of its own: the
              // absence of a download prompt IS the message, and a row that says
              // "this is fine" for the ordinary case is the kind of reassurance
              // that crowds out the warnings worth reading.
              null
            ) : selectedModel ? (
              // Offered BEFORE the first dictation on purpose. The alternative is
              // that the download starts when the user is already talking, where a
              // multi-hundred-megabyte transfer is indistinguishable from a hang.
              <div className="-mt-1 mb-1 flex flex-col gap-1.5 items-start">
                <p className="text-[12px] text-muted">
                  {i18nT('pages.settings.sttSettings.model_download_prompt', { size: fmtBytes(selectedModel.size_bytes) })}
                </p>
                <Btn onClick={() => prepareMut.mutate(selectedModel.name)} disabled={prepareMut.isPending}>
                  <Download className="lucide-inline" /> {i18nT('pages.settings.sttSettings.download_model')}
                </Btn>
              </div>
            ) : null}
            {/* A finished-and-failed transfer (the job's own `error`). Status
                panel with nothing editable in this block → hand-off on. */}
            {download?.step === DOWNLOAD_STEP_FAILED && download.error && (
              <ErrorNotice
                variant="inline"
                className="-mt-1 mb-1"
                message={i18nT('pages.settings.sttSettings.download_failed_reason', { error: download.error })}
                askAgent
              />
            )}
          </>
        )}

        <SettingsSelect configKey="stt.language_code" label={i18nT('pages.settings.sttSettings.language')} hint={i18nT('pages.settings.sttSettings.bcp_47_language_code_for_speech_recognition')} value={stt.language_code || defaultLanguage} options={languageOptions} optionLabels={languageOptions.map(code => code === 'auto' ? i18nT('pages.settings.sttSettings.language_auto') : code)} onChange={v => set({ language_code: v })} disabled={saving} />

        {/* Kept on the surface, not in Advanced, because it is the one setting here
            that changes where the words GO: the transcript (never the audio) leaves
            this machine. The description stays inline for the same reason -- consent
            a user has to hover to read is not consent. */}
        <SettingsToggle
          label={i18nT('pages.settings.sttSettings.polish')}
          description={i18nT('pages.settings.sttSettings.polish_desc')}
          checked={!!stt.polish}
          onChange={v => set({ polish: v })}
          disabled={saving}
          configKey="stt.polish"
        />

        {/* A DISCLOSURE, not a heading. Every row below is real and adjustable and
            every one has a default that works, so on the surface they only compete
            for attention with the six decisions above them. A heading was the first
            attempt and it did not help: the rows still rendered, still carried their
            own explanations, and the panel was as tall as before.

            Their explanations moved into `hint` tips at the same time. A sentence
            that tells you what a control IS belongs behind a "?"; only a sentence you
            need in order to CHOOSE earns permanent space. */}
        <SettingsSection title={i18nT('pages.settings.sttSettings.section_fine_tuning')} collapsible>
          {canStream && (
            <SettingsToggle label={i18nT('pages.settings.sttSettings.streaming')} hint={i18nT('pages.settings.sttSettings.streaming_desc')} checked={!!stt.streaming} onChange={v => set({ streaming: v })} disabled={saving} configKey="stt.streaming" />
          )}

          {canStream && stt.streaming && (
            <>
              <SettingsToggle label={i18nT('pages.settings.sttSettings.endpointing')} hint={i18nT('pages.settings.sttSettings.endpointing_desc')} checked={!!stt.endpointing} onChange={v => set({ endpointing: v })} disabled={saving} configKey="stt.endpointing" />

              {/* No duration steppers here at all, and that is the point of this
                  block rather than an omission.

                  The phrase-commit pause (`stt.silence_ms`) went the same way as the
                  live-refresh cadence below it: both asked a user to reason about a
                  raw millisecond number against speech they cannot time, and nobody
                  can tell 700 ms from 750 ms by feel. What a user actually wants when
                  dictation cuts them off is the Auto-submit toggle above, which is
                  the behaviour those milliseconds were tuning.

                  Both keys are still honoured from config.json, so an operator who
                  has measured their own pauses loses nothing; only the pickers are
                  gone, because a dial nobody can aim is worse than no dial. */}
              {/* No refresh-interval stepper. Measurement is why: whisper.cpp pads
                  every decode into a fixed analysis window, so a decode costs a large
                  constant plus a small term in the audio length -- about 0.78 s fixed
                  plus 0.08 s per audio-second for `base` on a 32-core aarch64 CPU
                  build. The cadence a user could dial in was unreachable on any CPU
                  build, so the control changed nothing they could perceive.
                  `stt.partial_interval_ms` is still honoured from config.json; only
                  the picker is gone, because a dial that does nothing is worse than
                  no dial. */}
            </>
          )}

          <SettingsToggle label={i18nT('pages.settings.sttSettings.dictation_panel')} hint={i18nT('pages.settings.sttSettings.show_an_animated_panel_while_recording_instead_of')} checked={stt.dictation_panel !== false} onChange={v => set({ dictation_panel: v })} disabled={saving} />

          {stt.enabled && <PushToTalkConfig />}
        </SettingsSection>

        {isTranscribe && (
          <>
            <AwsConsentGate service={PROVIDER_TRANSCRIBE} />
            <SettingsInput label={i18nT('pages.settings.sttSettings.aws_profile_transcribe')} description={i18nT('pages.settings.sttSettings.aws_credentials_profile_for_transcribe_blank_def')} value={localProfile} onChange={setLocalProfile} onBlur={() => set({ transcribe_profile: localProfile.trim() })} placeholder={i18nT('pages.settings.sttSettings.default')} disabled={saving} />
            <SettingsInput label={i18nT('pages.settings.sttSettings.aws_region_transcribe')} description={i18nT('pages.settings.sttSettings.aws_region_for_transcribe')} value={localRegion} onChange={setLocalRegion} onBlur={() => set({ transcribe_region: localRegion.trim() })} placeholder={i18nT('pages.settings.sttSettings.us_east_1')} disabled={saving} />
          </>
        )}

        {/* No Runtime row: the `/api/config/stt` response has no `docker_mode`
            field, so STT has no Docker runtime and nothing to display.
            No install button either: the recogniser ships in the `voice` extra
            and its weights are fetched by the model download above, so the only
            thing left that a terminal can fix is listed as a command. */}
        {!available && (
          <div className="mt-2">
            {isTranscribe && stt.transcribe_unsupported && (
              // No install channel can make the `voice` extra importable in
              // this gateway's interpreter — say so instead of showing an
              // empty panel or a command that errors. The desktop app gets
              // its own copy: "run the gateway from a different Python
              // environment" is not actionable for an app bundle, so it names
              // the real remedy (a pip-installed gateway) instead.
              <div className="mb-3 bg-warn-subtle border border-border rounded-lg p-3 animate-rise">
                <p className="text-sm text-text">
                  {stt.bundled_interpreter
                    ? i18nT('pages.settings.sttSettings.the_desktop_app_can_t_add_transcribe_support_ins')
                    : i18nT('pages.settings.sttSettings.this_gateway_s_python_can_t_install_extra_packag')}
                </p>
              </div>
            )}
            {stt.prereqs?.length > 0 && (
              <div className="mb-3 bg-accent/10 border border-accent/20 rounded-lg p-3 animate-rise">
                <p className="text-sm text-text font-medium mb-2">{i18nT('pages.settings.sttSettings.run_these_commands_in_your_terminal_first')}</p>
                {stt.prereqs.map((cmd, i) => (
                  <code key={i} className="block bg-bg-elevated rounded px-3 py-1.5 text-[13px] font-mono text-accent mb-1 select-all">{cmd}</code>
                ))}
                {/* The restart hint is tied to the pip command -- matched on
                    `-m pip install`, since the command names the extra's real
                    distributions and no longer contains an extra name -- not to
                    the provider: a package only becomes importable in a fresh
                    process, while an ffmpeg-only list needs no restart because
                    the PATH probe re-runs on every settings read. The button is
                    now the ONLY next step, because the in-dashboard installer it
                    used to sit beside is gone; whichever provider asked for the
                    extra, restarting is what makes it importable. */}
                {stt.prereqs.some(c => c.includes('-m pip install')) && (
                  <div className="flex flex-wrap items-center gap-2 mt-2">
                    <p className="text-muted text-[13px]">{i18nT('pages.settings.sttSettings.then_restart_the_gateway_so_it_can_import_the_ne')}</p>
                    <RestartGatewayButton
                      pending={restartMut.isPending}
                      restarting={restarting}
                      onConfirm={() => restartMut.mutate()}
                      testId="stt-restart-gateway"
                    />
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* A packaged desktop install owns its decoder. If its authenticated
            payload is absent or damaged, reinstalling the app is the only
            supported recovery; the user must not install FFmpeg separately. */}
        {!!stt.ffmpeg_missing && !!stt.bundled_interpreter && (
          <div className="mt-2 bg-warn-subtle border border-border rounded-lg p-3 animate-rise">
            <p className="text-sm text-text font-medium">
              {i18nT('pages.settings.sttSettings.the_bundled_audio_decoder_is_missing_or_damaged')}
            </p>
          </div>
        )}

        {/* A source install's decoder, and the one thing on this page that used
            to be a dead end: the panel printed a shell command, and on a
            distribution with no ffmpeg package that command was an `echo` of a
            URL. The gateway can fetch the same digest-verified upstream bytes a
            desktop release carries, so the states below are a fetch, its
            progress, and — when it fails — a hand-off to an agent session,
            leaving the manual commands only for a platform nothing is pinned for.
            Rendered even when Status reads "ready": availability deliberately
            treats the decoder as optional, so a provider can be usable while an
            uploaded WebM cannot be decoded. */}
        {!stt.bundled_interpreter && ffmpeg && !ffmpeg.present && (
          <div className="mt-2 bg-warn-subtle border border-border rounded-lg p-3 animate-rise">
            <p className="text-sm text-text font-medium mb-2">
              {i18nT('pages.settings.sttSettings.ffmpeg_is_missing_voice_recordings_from_the_brow')}
            </p>
            {decoderDownloading ? (
              <DecoderDownloadProgress download={ffmpeg.download} />
            ) : (
              <>
                {ffmpeg.auto_fetch === FFMPEG_AUTO_FETCH_AVAILABLE && (
                  <div className="flex flex-col gap-1.5 items-start">
                    <p className="text-[12px] text-muted">
                      {i18nT('pages.settings.sttSettings.decoder_fetch_prompt')}
                    </p>
                    <Btn onClick={() => decoderMut.mutate()} disabled={decoderMut.isPending}>
                      <Download className="lucide-inline" /> {i18nT('pages.settings.sttSettings.download_decoder')}
                    </Btn>
                  </div>
                )}
                {/* The manual route, for a platform with no pinned executable
                    (and therefore nothing a fetch could install). An empty
                    command list is the honest answer on a host where no package
                    manager can supply one, so the block renders nothing rather
                    than an instruction that only prints a sentence. */}
                {ffmpeg.auto_fetch !== FFMPEG_AUTO_FETCH_AVAILABLE && stt.prereqs?.length > 0 &&
                  stt.prereqs.map((cmd, i) => (
                    <code key={i} className="block bg-bg-elevated rounded px-3 py-1.5 text-[13px] font-mono text-accent mb-1 select-all">{cmd}</code>
                  ))
                }
                {decoderFailed && (
                  // The agent gets the failure, not the user: a digest mismatch
                  // or an unreachable index is not something a settings page can
                  // talk anyone through, and this IS an agent product. The button
                  // PRE-FILLS a composer and stops — nothing is sent on the
                  // user's behalf — through the same hand-off every other error
                  // surface uses.
                  <div className="flex flex-col gap-1.5 items-start mt-1.5">
                    {/* `askAgent` is off because the hand-off is REPLACED here, not
                        withheld: `AskAgentButton` carries the error journal's
                        context (route, endpoint, status), which cannot express the
                        four facts this repair needs — the failure code, the
                        GATEWAY's OS and arch, the trusted decoder locations, and
                        that ~/.local/bin is deliberately not one of them. Two
                        buttons offering the agent different payloads is worse than
                        one carrying the right payload, so the button below is the
                        panel's own hand-off.
                        On the draft this navigation could cost, the same trade
                        BrowserPanel's install error takes and a safer version of
                        it: the only editable values in this subtree are the AWS
                        profile and region inputs above, which commit `onBlur` — so
                        moving focus to this button is itself what saves them, and
                        being stranded on a digest mismatch is not recoverable from
                        this screen. */}
                    <ErrorNotice
                      message={i18nT('pages.settings.sttSettings.decoder_download_failed_reason', {
                        error: decoderFailureText,
                      })}
                      variant="inline"
                      askAgent={false}
                    />
                    <Btn onClick={askAgentToFixDecoder}>
                      <Sparkles className="lucide-inline" /> {i18nT('pages.settings.sttSettings.let_the_agent_fix_it')}
                    </Btn>
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </SettingsCard>
    </>
  )
}
