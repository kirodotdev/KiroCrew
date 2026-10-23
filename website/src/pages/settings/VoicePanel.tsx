import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsSelect, SettingsInput } from '../../components/settings'
import { Btn, FormSkeleton } from '../../components/ui'
import { api } from '../../api/client'
import SttSettings from './SttSettings'
import AwsConsentGate from '../../components/AwsConsentGate'

import { i18nT } from '../../i18n/t'
import { Info } from 'lucide-react'
import ErrorNotice from '../../components/ErrorNotice'
import SpeechProviderNotice from '../../components/SpeechProviderNotice'
import { parseSpeechProviders, speechProviderNotice, speechProviderOptions } from '../../lib/speechProviders'
import { languageOptionLabel } from '../../lib/sttProviders'
type VoiceConfig = {
  enabled: boolean; provider: string; voice: string; engine: string; rate: string
  autoSpeak: boolean; aws_profile: string; region: string
  system_voice: string
  /** BCP-47 tag for spoken replies; `''` is the provider's own default. Absent on an older gateway. */
  language_code?: string
  /** The Language picker's codes, `''` first; absent on an older gateway. */
  language_codes?: string[]
  /** Served provider entries (see lib/speechProviders); absent on an older gateway. */
  providers?: unknown
}

/** The options when the gateway serves no provider list. */
const PROVIDER_OPTIONS = ['system', 'polly']

// Consent-gate service key for Polly; shared so the gate and the cache
// invalidation below cannot drift apart.
const PROVIDER_POLLY = 'polly'
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
  system: 'pages.settings.voicePanel.system_built_in',
  polly: 'pages.settings.voicePanel.amazon_polly_cloud',
}

/** Sentinel for "let the OS pick", which is what an empty `system_voice` means. */
const SYSTEM_VOICE_DEFAULT = ''

/** Sentinel for "the provider's own default language", what an empty `language_code` means. */
const LANGUAGE_PROVIDER_DEFAULT = ''

/**
 * Offline stand-in for `aws polly describe-voices` (non-Polly users have no AWS
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
 *  - Text-to-Speech: spoken replies. Provider is the host's built-in engine
 *    (the default, nothing to install) or Amazon Polly (cloud). The field set switches with the provider.
 *  - Speech-to-Text (Whisper / MLX / Transcribe): dictation + install flow.
 */
export function VoicePanel() {
  const qc = useQueryClient()
  const [saveError, setSaveError] = useState('')
  const [localProfile, setLocalProfile] = useState('')
  const [localRegion, setLocalRegion] = useState('')

  // ── Text-to-Speech config (server-side) ──
  const voiceQ = useQuery<VoiceConfig>({ queryKey: ['voiceConfig'], queryFn: () => api.voiceConfig() })
  type PollyVoice = { id: string; name: string; language: string; languageCode: string; gender: string; engines: string[] }
  // Only fetch the Polly voice catalogue (aws polly describe-voices) when Polly
  // is the active provider — other users have no AWS CLI/credentials.
  const voicesQ = useQuery<{ voices: PollyVoice[] }>({ queryKey: ['voiceVoices'], queryFn: () => api.voiceVoices(), staleTime: 3600_000, enabled: voiceQ.data?.provider === 'polly' })
  type SystemVoice = { id: string; name: string; language: string }
  // Enumerating the host engine's voices costs a subprocess, so it is fetched
  // only while that provider is selected — the same rule the Polly catalogue
  // follows above.
  const systemVoicesQ = useQuery<{ available: boolean; voices: SystemVoice[] }>({ queryKey: ['voiceSystemVoices'], queryFn: () => api.voiceSystemVoices(), staleTime: 3600_000, enabled: voiceQ.data?.provider === 'system' })

  const initializedRef = useRef(false)
  useEffect(() => {
    if (voiceQ.data && !initializedRef.current) {
      initializedRef.current = true
      setLocalProfile(voiceQ.data.aws_profile || '')
      setLocalRegion(voiceQ.data.region || '')
    }
  }, [voiceQ.data])

  // `voice` takes the first fallback entry rather than respelling the VoiceId:
  // one source for the same value, and the id is provider-supplied data that no
  // catalog can localise.
  const voiceCfg = voiceQ.data ?? { enabled: false, provider: 'system', voice: VOICE_OPTIONS_FALLBACK[0].value, engine: 'generative', rate: '100%', autoSpeak: false, aws_profile: '', region: '', system_voice: '' }
  const isPolly = voiceCfg.provider === 'polly'
  const isSystem = voiceCfg.provider === 'system'
  const servedProviders = parseSpeechProviders(voiceCfg.providers)
  const providerChoices = speechProviderOptions(servedProviders, PROVIDER_OPTIONS, p =>
    Object.prototype.hasOwnProperty.call(PROVIDER_LABEL_KEY, p) ? i18nT(PROVIDER_LABEL_KEY[p]) : p)
  const providerNotice = speechProviderNotice(servedProviders, voiceCfg.provider)
  // A host with no built-in engine is a real state the user has to act on
  // (install one, or switch provider), so it is surfaced rather than left to a
  // failed synthesis. Only claimed once the probe has actually answered.
  const systemUnavailable = isSystem && systemVoicesQ.isSuccess && !systemVoicesQ.data.available
  // A failed probe would otherwise collapse to a picker offering only the OS
  // default, which reads as "this host has one voice" rather than as a failure.
  const systemVoicesFailed = isSystem && systemVoicesQ.isError
  // The list grows from 1 entry to dozens when the probe lands, so opening it
  // early would show a host that appears to have a single voice.
  const systemVoicesLoading = isSystem && systemVoicesQ.isPending
  const systemVoiceOptions = [SYSTEM_VOICE_DEFAULT, ...(systemVoicesQ.data?.voices ?? []).map(v => v.id)]
  const systemVoiceLabels = [
    i18nT('pages.settings.voicePanel.os_default_voice'),
    ...(systemVoicesQ.data?.voices ?? []).map(v => `${v.name} (${v.language})`),
  ]
  const voiceOptions = voicesQ.data?.voices
    ? voicesQ.data.voices.map(v => ({ value: v.id, label: `${v.name} (${v.languageCode} ${v.gender[0]})`, engines: v.engines }))
    : VOICE_OPTIONS_FALLBACK.map(v => ({ ...v, engines: ENGINE_OPTIONS }))
  const selectedVoiceEngines = voiceOptions.find(v => v.value === voiceCfg.voice)?.engines ?? ENGINE_OPTIONS
  // An older gateway serves no list; offering only the default keeps the select
  // from naming a language it cannot save.
  const languageCodes = voiceCfg.language_codes?.length ? voiceCfg.language_codes : [LANGUAGE_PROVIDER_DEFAULT]
  const languageLabels = languageCodes.map(code => code === LANGUAGE_PROVIDER_DEFAULT
    ? i18nT('pages.settings.voicePanel.language_provider_default')
    : languageOptionLabel(code))

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
        window.dispatchEvent(new CustomEvent('voice-config-changed', { detail: ctx.prev }))
      }
      setSaveError(i18nT('pages.settings.voicePanel.failed_to_save_voice_config'))
    },
    onSuccess: (_data, patch) => {
      // The consent gate caches the resolved account under this key; a new
      // profile/region does not touch it, so the gate keeps showing the old
      // account and Confirm 409s on the stale value. Re-probe on a real change.
      if ('aws_profile' in patch || 'region' in patch) {
        qc.invalidateQueries({ queryKey: ['awsConsent', PROVIDER_POLLY] })
      }
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
      {/* askAgent ON: `onError` above has already reverted localProfile /
          localRegion to the server value
          by the time this banner shows, so the navigation cannot destroy an
          unsaved edit — the fields hold exactly what is persisted. */}
      <ErrorNotice message={saveError} onDismiss={() => setSaveError('')} className="mb-4 animate-rise" askAgent />

      <SettingsSection title={i18nT('pages.settings.voicePanel.text_to_speech')}>
        <SettingsCard>
          {voiceQ.isError ? (
            // Load failure: no control is mounted in this branch, so the
            // hand-off is on. Retry stays as a sibling.
            <div className="flex flex-wrap items-center gap-2 mb-2">
              <ErrorNotice variant="inline" message={i18nT('pages.settings.voicePanel.failed_to_load_voice_config')} askAgent />
              <Btn danger disabled={voiceQ.isFetching} onClick={() => voiceQ.refetch()}>{i18nT('pages.settings.voicePanel.retry')}</Btn>
            </div>
          ) : !voiceQ.isSuccess ? (
            <FormSkeleton rows={['toggle', 'field', 'field', 'field', 'field', 'field']} />
          ) : (
            <>
              <SettingsToggle label={i18nT('pages.settings.voicePanel.auto_speak_responses')} hint={i18nT('pages.settings.voicePanel.speak_every_assistant_reply_automatically')} checked={voiceCfg.autoSpeak} onChange={v => setVoice({ autoSpeak: v, ...(v ? { enabled: true } : {}) })} disabled={voiceDisabled} />
              <SettingsSelect settingId="voice.provider-2" label={i18nT('pages.settings.voicePanel.provider')} description={i18nT('pages.settings.voicePanel.the_built_in_engine_needs_no_setup_amazon_polly_uses')} value={voiceCfg.provider} options={providerChoices.options} optionLabels={providerChoices.labels} onChange={v => setVoice({ provider: v })} disabled={voiceDisabled} />
              {/* Hand-off on: with a registered provider selected, no Polly
                  field is mounted, and every control left commits on change. */}
              {providerNotice && <SpeechProviderNotice entry={providerNotice} testId="tts-provider-notice" askAgent />}
              {isSystem ? (
                <>
                  {systemUnavailable && (
                    /* Not an ErrorNotice: the probe SUCCEEDED and simply
                       reported no engine, so this is status text about
                       something that has not failed. It carries default
                       foreground weight rather than muted, because it is the
                       only thing explaining why both selects below are
                       disabled. The re-check is what makes the instruction
                       actionable — the voices query caches for an hour, so a
                       user who installs espeak-ng would otherwise stare at this
                       line until a full reload. */
                    <div className="flex flex-wrap items-center gap-2 mb-2 text-[13px]" data-testid="system-engine-absent">
                      <span className="flex items-center gap-1.5"><Info size={13} className="lucide-inline" /> {i18nT('pages.settings.voicePanel.this_host_has_no_built_in_speech_engine_install_es')}</span>
                      {/* "Check again", not "Retry": nothing failed here. The engine is
                          absent, so this re-runs detection after the user installs one --
                          the two genuine fetch failures below keep "Retry". */}
                      <Btn disabled={systemVoicesQ.isFetching} onClick={() => systemVoicesQ.refetch()}>{i18nT('pages.settings.voicePanel.recheck')}</Btn>
                    </div>
                  )}
                  {systemVoicesFailed && (
                    <div className="flex flex-wrap items-center gap-2 mb-2">
                      {/* askAgent ON: this branch mounts no editable field —
                          both selects below commit on change, so there is no
                          draft the navigation could destroy. */}
                      <ErrorNotice variant="inline" message={i18nT('pages.settings.voicePanel.could_not_read_the_hosts_voice_list_only_the_operat')} askAgent />
                      {/* Same label as the no-engine branch: both trigger the
                          same one refetch, so one term covers the one act. */}
                      <Btn danger disabled={systemVoicesQ.isFetching} onClick={() => systemVoicesQ.refetch()}>{i18nT('pages.settings.voicePanel.retry')}</Btn>
                    </div>
                  )}
                  <SettingsSelect label={i18nT('pages.settings.voicePanel.voice')} hint={i18nT('pages.settings.voicePanel.voice_from_the_hosts_built_in_speech_engine')} value={voiceCfg.system_voice} options={systemVoiceOptions} optionLabels={systemVoiceLabels} onChange={v => setVoice({ system_voice: v })} disabled={voiceDisabled || systemUnavailable || systemVoicesLoading} />
                  <SettingsSelect label={i18nT('pages.settings.voicePanel.speed')} hint={i18nT('pages.settings.voicePanel.speech_rate_for_spoken_replies_built_in')} value={voiceCfg.rate} options={SPEED_OPTIONS} onChange={v => setVoice({ rate: v })} disabled={voiceDisabled || systemUnavailable} />
                </>
              ) : isPolly ? (
                <>
                  <AwsConsentGate
                    service={PROVIDER_POLLY}
                    onConsentChange={() => qc.invalidateQueries({ queryKey: ['voiceVoices'] })}
                  />
                  {/* The catalogue read failed and the picker below is showing the
                      offline fallback list, not Polly's. No hand-off: the profile /
                      region inputs further down commit on blur, so a click here
                      mid-edit would drop what is in them. */}
                  {voicesQ.isError && (
                    <ErrorNotice variant="inline" className="mb-2" message={i18nT('pages.settings.voicePanel.voice_catalogue_unavailable')} />
                  )}
                  <SettingsSelect label={i18nT('pages.settings.voicePanel.voice')} hint={i18nT('pages.settings.voicePanel.amazon_polly_voice_for_tts')} value={voiceCfg.voice} options={voiceOptions.map(o => o.value)} optionLabels={voiceOptions.map(o => o.label)} onChange={v => { const engines = voiceOptions.find(o => o.value === v)?.engines ?? ENGINE_OPTIONS; const patch: Partial<VoiceConfig> = { voice: v }; if (!engines.includes(voiceCfg.engine)) patch.engine = engines[0]; setVoice(patch) }} disabled={voiceDisabled} />
                  <SettingsSelect label={i18nT('pages.settings.voicePanel.engine')} hint={i18nT('pages.settings.voicePanel.polly_engine_type')} value={voiceCfg.engine} options={selectedVoiceEngines} onChange={v => setVoice({ engine: v })} disabled={voiceDisabled} />
                  <SettingsSelect label={i18nT('pages.settings.voicePanel.speed')} hint={i18nT('pages.settings.voicePanel.speech_rate_for_spoken_replies_polly')} value={voiceCfg.rate} options={SPEED_OPTIONS} onChange={v => setVoice({ rate: v })} disabled={voiceDisabled} />
                  <SettingsInput label={i18nT('pages.settings.voicePanel.aws_profile_polly')} hint={i18nT('pages.settings.voicePanel.aws_credentials_profile_for_polly')} value={localProfile} onChange={setLocalProfile} onBlur={() => setVoice({ aws_profile: localProfile.trim() })} placeholder={i18nT('pages.settings.voicePanel.default')} disabled={voiceDisabled} />
                  <SettingsInput label={i18nT('pages.settings.voicePanel.aws_region_polly')} hint={i18nT('pages.settings.voicePanel.aws_region_for_polly_api')} value={localRegion} onChange={setLocalRegion} onBlur={() => setVoice({ region: localRegion.trim() })} placeholder={i18nT('pages.settings.voicePanel.us_east_1')} disabled={voiceDisabled} />
                </>
              ) : (
                /* Edition providers only: Polly and the built-in engine speak
                   the language of the voice picked above, so a Language select
                   on their branches would be a control that changes nothing. */
                <SettingsSelect settingId="voice.language-2" label={i18nT('pages.settings.voicePanel.language')} hint={i18nT('pages.settings.voicePanel.language_for_spoken_replies')} value={voiceCfg.language_code ?? LANGUAGE_PROVIDER_DEFAULT} options={languageCodes} optionLabels={languageLabels} onChange={v => setVoice({ language_code: v })} disabled={voiceDisabled} />
              )}
            </>
          )}
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('pages.settings.voicePanel.speech_to_text')}>
        <SttSettings cardIndex={1} />
      </SettingsSection>
    </>
  )
}
