/**
 * The speech-to-text provider vocabulary, and how a refusal to run is rendered.
 *
 * It lives outside the settings page because three surfaces need the same
 * answers: the settings panel names providers in a picker, ChatPage names the
 * configured one when the mic cannot start, and the backend's availability
 * `code` has to become a sentence in both. One table means they cannot disagree
 * about what `local` is called or about what a missing wheel means. The live
 * dictation socket reports its own failures by `code` too, so its vocabulary
 * is here beside the availability one rather than inlined in the hook.
 */
import { fmtBytes, fmtPercent } from '../i18n/format'
import { i18nT } from '../i18n/t'

/** Resident speech recogniser inside the gateway process. The default, every OS. */
export const PROVIDER_LOCAL = 'local'
/** Apple's on-device speech. macOS 26+, nothing to download. */
export const PROVIDER_APPLE = 'apple'
/** AWS Transcribe: off-host and paid, behind the AWS consent gate. */
export const PROVIDER_TRANSCRIBE = 'transcribe'

/**
 * What the picker offers when the backend serves no provider list. `local` and
 * `transcribe` only: `apple` exists on macOS 26+ alone, so advertising it
 * unconditionally would offer a provider that cannot start on most hosts.
 */
export const FALLBACK_PROVIDERS = [PROVIDER_LOCAL, PROVIDER_TRANSCRIBE]

/**
 * Which providers stream partial transcripts, used only while the backend has
 * served no `streaming_providers` capability list. All three of them do, so the
 * honest fallback is all three; the served list stays authoritative the moment
 * it arrives, which is what keeps a newly streaming provider from having its
 * own toggle hidden by this file.
 */
export const FALLBACK_STREAMING_PROVIDERS = [
  PROVIDER_LOCAL,
  PROVIDER_APPLE,
  PROVIDER_TRANSCRIBE,
]

/**
 * Providers whose model is a name from the downloadable catalog, so the model
 * picker and its download control apply to them. Apple ships its model with the
 * OS and Transcribe runs the model on AWS, so neither has one to choose.
 *
 * A membership set rather than `provider === PROVIDER_LOCAL`: a second
 * catalog-backed provider joins by adding a row here, and it reads as a
 * capability rather than as the absence of the other two.
 */
export const CATALOG_MODEL_PROVIDERS = [PROVIDER_LOCAL]

/**
 * Catalog KEY for each provider's dropdown label.
 *
 * Keys, not resolved strings: the table is evaluated at module load, so an
 * `i18nT()` call here would freeze the boot language and never re-resolve on a
 * language switch. The lookup happens in `providerLabel()`, which runs during
 * render. Flat `Record` of full literal keys indexed inline at the `i18nT()`
 * call, the only shape `scripts/check-i18n-keys.mjs` resolves statically.
 */
export const PROVIDER_LABEL_KEY: Record<string, string> = {
  local: 'pages.settings.sttSettings.provider_local',
  apple: 'pages.settings.sttSettings.provider_apple',
  transcribe: 'pages.settings.sttSettings.provider_transcribe',
}

/** Localised dropdown label for a provider id, falling back to the raw id. */
export function providerLabel(provider: string): string {
  // `hasOwnProperty`, not `in`: the id list arrives over the wire, so a backend
  // reporting `toString` would otherwise resolve to an inherited
  // Object.prototype member and hand a function to i18next.
  return Object.prototype.hasOwnProperty.call(PROVIDER_LABEL_KEY, provider)
    ? i18nT(PROVIDER_LABEL_KEY[provider])
    : provider
}

/**
 * Catalog KEY for each machine-readable reason speech-to-text cannot run,
 * as reported by the backend's availability probe.
 *
 * One key per code because the ACTIONS differ: installing an extra, installing a
 * C++ toolchain, upgrading macOS and downloading a model are four unrelated
 * things, and collapsing them into "not installed" is what makes a feature read
 * as broken rather than as unconfigured. Same keys-not-strings reasoning as
 * `PROVIDER_LABEL_KEY`.
 */
export const UNAVAILABLE_CODE_KEY: Record<string, string> = {
  stt_disabled: 'pages.settings.sttSettings.unavailable_disabled',
  stt_extra_missing: 'pages.settings.sttSettings.unavailable_extra_missing',
  stt_no_wheel_for_platform: 'pages.settings.sttSettings.unavailable_no_wheel',
  stt_import_failed: 'pages.settings.sttSettings.unavailable_import_failed',
  stt_model_missing: 'pages.settings.sttSettings.unavailable_model_missing',
  stt_apple_unsupported: 'pages.settings.sttSettings.unavailable_apple_unsupported',
  stt_apple_needs_toolchain: 'pages.settings.sttSettings.unavailable_apple_needs_toolchain',
}

/**
 * Localised explanation for an availability refusal.
 *
 * Falls back to the backend's own `detail` for a code this build does not know:
 * an unrecognised reason with a server sentence beside it is still actionable,
 * whereas a generic "unavailable" throws away the only information there was.
 */
export function unavailableMessage(code: string, detail = ''): string {
  return Object.prototype.hasOwnProperty.call(UNAVAILABLE_CODE_KEY, code)
    ? i18nT(UNAVAILABLE_CODE_KEY[code])
    : detail
}

/**
 * Catalog KEY for each machine-readable reason a live dictation STREAM failed.
 *
 * The `error` frame's `code` is the contract and its `message` is advisory English,
 * so a surface that renders the message shows English in a 12-language UI. These are
 * the codes only the streaming path can produce, plus `stt_model_missing`, which
 * reaches the socket too and needs different words there: the settings panel's
 * version says "Download it below", and below the composer there is no "below".
 *
 * Keys, not resolved strings, for the same reason as `PROVIDER_LABEL_KEY` — the
 * table is evaluated at module load, so an `i18nT()` here would freeze the boot
 * language.
 */
export const STREAM_ERROR_CODE_KEY: Record<string, string> = {
  stt_decode_failed: 'lib.sttProviders.stream_error_decode_failed',
  stt_session_failed: 'lib.sttProviders.stream_error_session_failed',
  stt_max_duration_exceeded: 'lib.sttProviders.stream_error_max_duration',
  stt_model_missing: 'lib.sttProviders.stream_error_model_missing',
}

/**
 * Localised text for a streaming `error` frame, or `''` when nothing is known.
 *
 * Three tiers, narrowest first: a stream-specific key, then the availability
 * vocabulary (a missing extra, no wheel, an import failure and the Apple codes all
 * travel through the socket unchanged, and their remedy is the same sentence the
 * settings panel already shows), then the server's own `detail`. An unrecognised
 * code with a server sentence beside it is still actionable; the caller supplies
 * the generic last resort when even that is empty.
 */
export function streamErrorMessage(code: string, detail = ''): string {
  return Object.prototype.hasOwnProperty.call(STREAM_ERROR_CODE_KEY, code)
    ? i18nT(STREAM_ERROR_CODE_KEY[code])
    : unavailableMessage(code, detail)
}

/**
 * Localised "downloading the audio decoder, N% (x of y)" line.
 *
 * A separate key from the model transfer's, not a shared "downloading {{what}}"
 * with a substituted noun: a sentence assembled from a translated fragment
 * inflects wrongly in most of the twelve languages, and these two transfers can
 * be in flight at the same time — so each bar has to name what it is.
 */
export function decoderDownloadLabel(download: { done: number; total: number }): string {
  return i18nT('lib.sttProviders.downloading_decoder', {
    done: fmtBytes(download.done),
    total: fmtBytes(download.total),
    pct: fmtPercent(downloadRatio(download), { maximumFractionDigits: 0 }),
  })
}

/**
 * The prompt handed to an agent session when the decoder fetch has failed.
 *
 * A catalog value, not a template built here, because the whole thing is text the
 * user reads in the composer before they press send — so it is translated like any
 * other copy, and a reader who works in Japanese is not asked to approve a
 * paragraph of English. The variables are the four facts the agent cannot
 * otherwise obtain from the page: the machine-readable failure `code`, the
 * backend's own `detail`, and the GATEWAY's OS and architecture (which are not the
 * browser's — the dashboard may be open against a remote host).
 *
 * `detail` can be empty when a failure carried only a code, and an empty
 * interpolation would read as a dangling dash; the code alone is substituted then.
 */
export function decoderRepairPrompt(failure: {
  code: string
  detail: string
  os: string
  arch: string
}): string {
  return i18nT('pages.settings.sttSettings.decoder_agent_prompt', {
    code: failure.code || i18nT('pages.settings.sttSettings.decoder_failure_unknown'),
    detail: failure.detail || i18nT('pages.settings.sttSettings.decoder_failure_unknown'),
    os: failure.os,
    arch: failure.arch,
  })
}

/**
 * Fraction of a model transfer that has arrived, in [0, 1].
 *
 * A zero `total` means the transfer has been announced but its size has not been
 * reported yet, so this answers 0 rather than dividing by it. One owner for that
 * guard, because the settings panel draws a progress bar from the same number the
 * label below renders as a percentage.
 */
export function downloadRatio(download: { done: number; total: number }): number {
  return download.total > 0 ? download.done / download.total : 0
}

/**
 * Localised "downloading the speech model, N% (x of y)" line.
 *
 * Here rather than in either surface that shows it: the recording chrome and the
 * settings panel both report the SAME transfer, and two keys for one event is how
 * they end up describing it differently in ten languages. Absolute bytes as well
 * as a percentage, because a percentage alone hides how much is left on a slow
 * link, and this transfer runs from 78 MB to 1.6 GB.
 */
export function downloadLabel(download: { done: number; total: number }): string {
  return i18nT('lib.sttProviders.downloading_speech_model', {
    done: fmtBytes(download.done),
    total: fmtBytes(download.total),
    pct: fmtPercent(downloadRatio(download), { maximumFractionDigits: 0 }),
  })
}
