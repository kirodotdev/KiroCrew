import { useState, useEffect, useCallback, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Cpu, CheckCircle, XCircle, Loader2 } from 'lucide-react'
import { Trans } from 'react-i18next'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, Input, Badge } from '../../components/ui'
import Modal from '../../components/Modal'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { embeddingRepairMessage, isModelPathErrorCode, EMBED_MODEL_PATH_ID, type EmbeddingSetupFields } from './embeddingStatusText'
import { SettingRef } from '../../components/settingRef/SettingRef'

/** Live re-embed progress, mirrored from the backend's ReembedProgress. */
export interface ReembedState {
  step?: string          // idle | applying | running | done | failed
  done?: number
  total?: number
  error?: string
}

export interface EmbedModelStatus extends EmbeddingSetupFields {
  model_id?: string
  model_dim?: number
  model_source?: string  // 'default' | 'custom'
  model_path?: string
  reembed?: ReembedState
}

/** Bar geometry: how wide to draw the fill, and whether it means "unknown".
 *
 * Kept separate from `reembedPct` (which drives the numeric LABEL) because the
 * bar must stay honest when there is no percentage to show. A null percentage
 * previously fell through to width:100%, so "Loading the new model…" rendered a
 * full bar — indistinguishable from finished, which is the exact ambiguity the
 * null was introduced to avoid. Indeterminate therefore draws a SHORT pulsing
 * fill, and a failure stops at the fraction it actually reached instead of
 * filling red to the end. */
export function reembedBar(r: ReembedState | undefined): { widthPct: number; indeterminate: boolean } {
  if (!r) return { widthPct: 0, indeterminate: false }
  const total = r.total ?? 0
  const done = r.done ?? 0
  const frac = total > 0 ? Math.min(100, Math.max(0, Math.round((done / total) * 100))) : null
  if (r.step === 'deferred') return { widthPct: 0, indeterminate: true }
  if (r.step === 'applying') return { widthPct: 30, indeterminate: true }
  if (r.step === 'running') {
    return frac == null ? { widthPct: 30, indeterminate: true } : { widthPct: frac, indeterminate: false }
  }
  if (r.step === 'failed') {
    // Stop where it stopped; only a failure with no denominator fills the track.
    return { widthPct: frac ?? 100, indeterminate: false }
  }
  return { widthPct: 100, indeterminate: false }
}

/** Percentage for the progress bar, or null when no denominator is known yet.
 *
 * `applying` deliberately yields null: the model is still loading, so there is
 * no total, and rendering 0 % would imply work has started and stalled. */
export function reembedPct(r: ReembedState | undefined): number | null {
  if (!r || r.step !== 'running') return null
  const total = r.total ?? 0
  if (total <= 0) return null
  return Math.min(100, Math.round(((r.done ?? 0) / total) * 100))
}

/** True while a change is being applied or vectors are being rebuilt. */
export function reembedBusy(r: ReembedState | undefined): boolean {
  return r?.step === 'applying' || r?.step === 'running'
}

/** Full literal key map. `as const` + an explicit switch is the repo's standard
 * fix for a lookup the i18n key gate must be able to resolve statically: a
 * dynamically-indexed key is one it cannot verify exists, so the call site would
 * be exempt from every catalog check (see `UPDATE_ERROR_KEYS` in AboutPanel). */
const EMBED_ERROR_KEYS = {
  notAbsolute: 'pages.overview.embedModel.err_not_absolute',
  notFound: 'pages.overview.embedModel.err_not_found',
  notAFile: 'pages.overview.embedModel.err_not_a_file',
  tooSmall: 'pages.overview.embedModel.err_too_small',
  protectedPath: 'pages.overview.embedModel.err_protected',
  unreadable: 'pages.overview.embedModel.err_unreadable',
  inProgress: 'pages.overview.embedModel.err_in_progress',
  config: 'pages.overview.embedModel.err_config',
  restricted: 'pages.overview.embedModel.err_restricted',
  envOverride: 'pages.overview.embedModel.err_env_override',
} as const

/** Localize a backend error by its machine-readable `code`.
 *
 * The repo contract (test_error_code_contract.py) is that `code` is the wire
 * contract and `error` is advisory prose — rendering the prose verbatim into a
 * localized UI is untranslatable by construction. Unknown codes fall back to the
 * prose so a new backend code is never silently swallowed.
 *
 * The api client rejects with an `ApiError` that keeps the payload as a raw JSON
 * STRING on `.body`, not as own properties — reading `err.code` directly finds
 * nothing and silently falls through to `String(err)`, which renders
 * "ApiError: <English prose>". So parse `.body` first. */
export function embedModelErrorMessage(err: unknown): string {
  let obj: Record<string, unknown> = {}
  if (err != null && typeof err === 'object') {
    obj = err as Record<string, unknown>
    const raw = obj.body
    if (typeof raw === 'string' && raw.trim()) {
      try {
        const parsed = JSON.parse(raw)
        if (parsed && typeof parsed === 'object') obj = { ...obj, ...parsed }
      } catch { /* not JSON — fall back to the fields already present */ }
    }
  }
  const code = typeof obj.code === 'string' ? obj.code : ''
  const prose = typeof obj.error === 'string' ? obj.error : ''
  switch (code) {
    case 'model_path_not_absolute': return i18nT(EMBED_ERROR_KEYS.notAbsolute)
    case 'model_path_not_found': return i18nT(EMBED_ERROR_KEYS.notFound)
    case 'model_path_not_a_file': return i18nT(EMBED_ERROR_KEYS.notAFile)
    case 'model_path_too_small': return i18nT(EMBED_ERROR_KEYS.tooSmall)
    case 'model_path_protected': return i18nT(EMBED_ERROR_KEYS.protectedPath)
    case 'model_path_unreadable': return i18nT(EMBED_ERROR_KEYS.unreadable)
    case 'model_change_in_progress': return i18nT(EMBED_ERROR_KEYS.inProgress)
    case 'config_unparseable': return i18nT(EMBED_ERROR_KEYS.config)
    case 'restricted_session': return i18nT(EMBED_ERROR_KEYS.restricted)
    case 'env_override_active': return i18nT(EMBED_ERROR_KEYS.envOverride)
    default: return prose || String(err)
  }
}

/** Extract the machine-readable error code from an API error (for conditional rendering). */
export function embedModelErrorCode(err: unknown): string {
  let obj: Record<string, unknown> = {}
  if (err != null && typeof err === 'object') {
    obj = err as Record<string, unknown>
    const raw = obj.body
    if (typeof raw === 'string' && raw.trim()) {
      try {
        const parsed = JSON.parse(raw)
        if (parsed && typeof parsed === 'object') obj = { ...obj, ...parsed }
      } catch { /* ignore */ }
    }
  }
  return typeof obj.code === 'string' ? obj.code : ''
}

const POLL_MS = 2000

export default function EmbeddingModelCard() {
  const queryClient = useQueryClient()
  const embeddingRead = useQuery({
    queryKey: ['member-memory', 'default', 'embedding-status'],
    queryFn: () => api.vectorEmbeddingStatus() as Promise<EmbedModelStatus>,
    staleTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: query => {
      const progress = query.state.data?.reembed
      return progress?.step === 'deferred' ? 30000 : reembedBusy(progress) ? POLL_MS : false
    },
  })
  // Both cards observe one request/cache, not independently copied responses.
  const status = embeddingRead.data ?? null
  const [path, setPath] = useState('')
  const [touched, setTouched] = useState(false)
  const [checking, setChecking] = useState(false)
  const [checked, setChecked] = useState<{ ok: boolean; msg: string; code?: string } | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [applying, setApplying] = useState(false)
  // Mirror of `touched` that a fetch already in flight can read when its
  // response lands. The closure value is the edit state at the time the request
  // was SENT; a keystroke typed while the response is pending would otherwise be
  // overwritten by the configured path when it arrives.
  const touchedRef = useRef(false)

  const load = useCallback(async () => {
    try {
      await queryClient.fetchQuery({
        queryKey: ['member-memory', 'default', 'embedding-status'],
        queryFn: () => api.vectorEmbeddingStatus() as Promise<EmbedModelStatus>,
        staleTime: 0,
      })
    } catch { /* the Memory card surfaces connection errors */ }
  }, [queryClient])

  useEffect(() => {
    // Background status refreshes must never replace an in-progress draft.
    if (status && !touchedRef.current) setPath(status.model_path || '')
  }, [status])

  const isCustom = status?.model_source === 'custom'
  const reembed = status?.reembed
  const busy = reembedBusy(reembed)
  const pct = reembedPct(reembed)
  const bar = reembedBar(reembed)

  // Three honest header states, not two. The backend reports the configured
  // model's identity (`model_id` / `model_dim`) separately from whether that model
  // is serving vectors right now (`model_active`: loaded AND not a gated
  // candidate). `model_active === false` covers unrelated reasons — the bundled
  // file not downloaded yet or its download failed, a present file not loaded
  // until first use, a custom candidate still loading during Apply, a custom file
  // that failed verification — so the header says only what is true of all of
  // them: configured, not active. The Vector Memory card carries the reason; this
  // line never guesses "rebuilding" or "activating". An absent `model_active`
  // (older backend) keeps the previous reading, active. A gated candidate reports
  // dim 0 until it finishes loading, and an unknown width is an unknown model
  // (the existing fallback). Known models keep a provenance badge without a
  // success colour when not serving; unknown models omit that badge.
  const modelState: 'active' | 'inactive' | 'unknown' =
    !(status?.model_id && status.model_dim) ? 'unknown'
    : status.model_active === false ? 'inactive'
    : 'active'

  // The configured path is unusable (file moved, deleted, unreadable) and the
  // field showed that path: say so under the field and keep Apply off, because
  // submitting it would fail with the same error. This is derived, not stored,
  // and yields only to a VERDICT, never to a keystroke: an edit alone proves
  // nothing about the new path, so the last known verdict (this notice) stays
  // on screen and keeps the gate until the live check runs on blur and its
  // result replaces it (a passed check enables Apply, a failed one shows its
  // own message, an emptied field reverts to the bundled model). Dropping the
  // notice on the first keystroke left an enabled Apply beside a path nobody
  // had checked yet, which is the state a reader would not trust. A field
  // whose configured path is healthy is untouched by this: editing it enables
  // Apply as before. A file restored IN PLACE is the case nothing on screen
  // explains: the idle card does not poll, so the notice would outlive the
  // fault until the user happened to blur the field. The effect below re-reads
  // the status when the window regains focus while this notice is showing for
  // the untouched configured path.
  const statusPathError = status && !checked && !checking && isModelPathErrorCode(status.setup_error_code)
    ? embedModelErrorMessage({ code: status.setup_error_code, error: status.setup_error })
    : null

  // Re-read the status on return focus, only while the status-derived path
  // error is showing for the UNTOUCHED configured path. Returning from a
  // terminal or file manager is when the file has most likely just been
  // restored, and the status endpoint validates the configured path on every
  // call (`resolve_custom_model`), so one refetch is the whole re-check: a
  // restored file clears the notice and re-enables Apply; a still-missing file
  // re-reports the same code and nothing changes. Once the user edits the
  // field the notice stays (see above) but the listener goes: the refetch is
  // about the configured path, not their draft, and the live check on blur is
  // what owns the gate from then on, so no refetch can clobber their draft.
  const pathErrorShowing = statusPathError !== null && !touched
  useEffect(() => {
    if (!pathErrorShowing) return
    const onFocus = () => { void load() }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [pathErrorShowing, load])

  // What Apply will DO is decided by the path it will submit, not by whether the
  // field was touched: an edit typed and then restored submits the configured
  // path again, and `touched` stays true for it. Submitting the CONFIGURED path
  // unchanged (or an empty field while the bundled model is configured) keeps
  // the same setting and rebuilds its vectors, so the button says so; the file
  // behind that path may well have been replaced, which is exactly when a
  // reapply is wanted. Any other path is a model change. The label never gates
  // the button: the disabled guards below are the same for both readings.
  const samePath = !!status && path.trim() === (status.model_path || '')

  const check = useCallback(async () => {
    const p = path.trim()
    setChecked(null)
    if (!p) { setChecked({ ok: true, msg: i18nT('pages.overview.embedModel.will_revert') }); return }
    setChecking(true)
    try {
      const r = await api.vectorValidateEmbedModel(p) as { ok?: boolean; size_bytes?: number }
      const mb = Math.round((r.size_bytes ?? 0) / (1024 * 1024))
      setChecked({ ok: true, msg: i18nT('pages.overview.embedModel.check_ok', { mb }) })
    } catch (e) {
      setChecked({ ok: false, msg: embedModelErrorMessage(e), code: embedModelErrorCode(e) })
    } finally { setChecking(false) }
  }, [path])

  const apply = useCallback(async () => {
    setConfirmOpen(false)
    setApplying(true)
    try {
      await api.vectorApplyEmbedModel(path.trim())
      touchedRef.current = false
      setTouched(false)
      setChecked(null)
      await load()
    } catch (e) {
      setChecked({ ok: false, msg: embedModelErrorMessage(e), code: embedModelErrorCode(e) })
    } finally { setApplying(false) }
  }, [path, load])

  return (
    <>
      <Card data-testid="embed-model-card">
        <CardTitle><Cpu className="lucide-inline" /> {i18nT('pages.overview.embedModel.title')}</CardTitle>
        <div className="text-[13px] text-muted mb-3">{i18nT('pages.overview.embedModel.subtitle')}</div>

        {/* Active model */}
        <div className="flex items-center justify-between gap-2 px-2.5 py-1.5 bg-bg-elevated border border-border rounded mb-3">
          <span className="text-[13px]" data-testid="embed-model-active" data-state={modelState}>
            {modelState === 'active' && i18nT('pages.overview.embedModel.active', { model: status?.model_id, dim: status?.model_dim })}
            {modelState === 'inactive' && i18nT('pages.overview.embedModel.configured_inactive', { model: status?.model_id, dim: status?.model_dim })}
            {modelState === 'unknown' && i18nT('pages.overview.embedModel.active_unknown')}
          </span>
          {modelState !== 'unknown' && (
            <Badge variant={modelState !== 'active' ? 'muted' : isCustom ? 'aim' : 'ok'} data-testid="embed-model-active-badge" data-state={modelState}>
              {isCustom ? i18nT('pages.overview.embedModel.badge_custom') : i18nT('pages.overview.embedModel.badge_bundled')}
            </Badge>
          )}
        </div>

        {/* Path field */}
        <label className="block text-[11px] text-muted mb-1" htmlFor={EMBED_MODEL_PATH_ID}>
          {i18nT('pages.overview.embedModel.path_label')}
        </label>
        <Input
          id={EMBED_MODEL_PATH_ID}
          aria-invalid={statusPathError ? true : undefined}
          aria-describedby={statusPathError ? `${EMBED_MODEL_PATH_ID}-status-error` : undefined}
          className="w-full"
          value={path}
          placeholder={i18nT('pages.overview.embedModel.path_placeholder')}
          disabled={busy || applying}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => { touchedRef.current = true; setTouched(true); setPath(e.target.value); setChecked(null) }}
          onBlur={check}
        />
        <div className="text-[11px] text-muted mt-1">{i18nT('pages.overview.embedModel.path_hint')}</div>

        {/* No hand-off: navigating away would discard unsaved memory edits elsewhere on this tab. */}
        <ErrorNotice
          id={`${EMBED_MODEL_PATH_ID}-status-error`}
          testId="embed-model-path-status-error"
          message={statusPathError}
          variant="inline"
          className="mt-1.5"
        />
        {checking && (
          <div className="text-[11px] text-muted mt-1.5 flex items-center gap-1">
            <Loader2 className="lucide-inline animate-spin" /> {i18nT('pages.overview.embedModel.checking')}
          </div>
        )}
        {checked && !checking && (
          <div className={`text-[11px] mt-1.5 flex items-start gap-1 ${checked.ok ? 'text-ok' : 'text-danger'}`}>
            {checked.ok ? <CheckCircle className="lucide-inline" /> : <XCircle className="lucide-inline" />}
            <span>
              {checked.code === 'env_override_active' ? (
                <Trans
                  i18nKey="pages.overview.embedModel.err_env_override_with_ref"
                  components={{
                    settingRef: <SettingRef kind="env" configKey="KIROCREW_EMBED_MODEL_PATH" valuePlaceholder="path" envIntent="unset" />,
                  }}
                />
              ) : (
                checked.msg
              )}
            </span>
          </div>
        )}

        <div className="flex items-center gap-2 mt-3">
          <Btn
            primary={!samePath}
            onClick={() => setConfirmOpen(true)}
            disabled={!status || busy || applying || checking || checked?.ok === false || !!statusPathError}
          >
            {applying
              ? i18nT('pages.overview.embedModel.applying')
              : samePath
                ? i18nT('pages.overview.embedModel.apply_reapply')
                : i18nT('pages.overview.embedModel.apply')}
          </Btn>
          <span className="text-[11px] text-muted">{i18nT('pages.overview.embedModel.no_restart')}</span>
        </div>

        {/* Re-embed progress */}
        {reembed && reembed.step !== 'idle' && (
          <div className="mt-3.5 pt-3 border-t border-border">
            <div className="flex items-center justify-between text-[13px] mb-1.5">
              {/* The standing-rebuild summary renders HERE and only here: this is
                * the card that owns Apply, so the numbers sit next to the control
                * that changes them. The Vector Memory card keeps its own 30s
                * refetch but does not repeat this line. */}
              <span role={reembed.step === 'deferred' ? 'status' : undefined} data-testid={reembed.step === 'deferred' ? 'embed-model-repair-status' : undefined}>
                {reembed.step === 'applying' && i18nT('pages.overview.embedModel.loading_model')}
                {reembed.step === 'running' && i18nT('pages.overview.embedModel.reembedding')}
                {reembed.step === 'deferred' && embeddingRepairMessage(status)}
                {/* Keep an unsaved model-path edit in this tab while inspecting logs. */}
                {reembed.step === 'deferred' && status?.repair?.unknown_scope && (
                  <> {' '}
                    <a href="/logs" target="_blank" rel="noopener noreferrer" className="underline">
                      {i18nT('hooks.usePanelTabs.logs')}
                    </a>
                  </>
                )}
                {reembed.step === 'done' && i18nT('pages.overview.embedModel.reembed_done')}
                {reembed.step === 'failed' && i18nT('pages.overview.embedModel.reembed_failed')}
              </span>
              {(reembed.step === 'running' || reembed.step === 'failed') && (reembed.total ?? 0) > 0 && (
                <span className="text-muted text-[11px]">
                  {i18nT('pages.overview.embedModel.counts', {
                    done: reembed.done ?? 0,
                    total: reembed.total ?? 0,
                    pct: pct ?? bar.widthPct,
                  })}
                </span>
              )}
            </div>
            {/* role/aria per the in-repo convention (DevFleetPage, TaskProgressBar):
              * without it a screen reader gets no re-embed progress at all. */}
            {reembed.step !== 'deferred' && <div
              className="w-full bg-bg-elevated rounded-full h-2 border border-border overflow-hidden"
              role="progressbar"
              aria-label={i18nT('pages.overview.embedModel.reembedding')}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={bar.indeterminate ? undefined : bar.widthPct}
            >
              <div
                className={`h-full rounded-full ${bar.indeterminate ? 'animate-pulse' : 'transition-all duration-1000 ease-out'}`}
                style={{
                  width: `${bar.widthPct}%`,
                  background: reembed.step === 'failed' ? 'var(--danger)' : 'var(--accent)',
                }}
              />
            </div>}
            {/* `reembed.error` is backend English prose. It arrives on the 200
              * status endpoint, so the error-code contract test does not cover
              * it — rendering it as the message would put untranslated text on
              * the one path a non-English user most needs to read. Show the
              * localized reason and keep the raw detail in the tooltip. */}
            <div
              className={`text-[11px] mt-1.5 ${reembed.step === 'failed' ? 'text-danger' : 'text-muted'}`}
              title={reembed.step === 'failed' ? (reembed.error || '') : undefined}
            >
              {reembed.step === 'failed'
                ? i18nT('pages.overview.embedModel.reembed_failed_hint')
                : i18nT('pages.overview.embedModel.keyword_meanwhile')}
            </div>
          </div>
        )}
      </Card>

      {/* A reapply of the configured path changes no setting (the file behind
        * it may have been replaced; that is the point of reapplying), so the
        * modal must not ask "Change the embedding model?": it borrows the card's
        * own neutral title and names the action it confirms. Its body describes
        * reloading the configured model, without claiming a new model exists. */}
      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title={samePath ? i18nT('pages.overview.embedModel.title') : i18nT('pages.overview.embedModel.confirm_title')}
        maxWidth={520}
        footer={
          <div className="flex justify-end gap-2">
            <Btn onClick={() => setConfirmOpen(false)}>
              {i18nT('pages.overview.embedModel.cancel')}
            </Btn>
            <Btn primary onClick={apply}>
              {samePath ? i18nT('pages.overview.embedModel.apply_reapply') : i18nT('pages.overview.embedModel.confirm_apply')}
            </Btn>
          </div>
        }
      >
        <div className="text-[13px] space-y-2">
          <p>{samePath ? i18nT('pages.overview.embedModel.confirm_reapply_body') : i18nT('pages.overview.embedModel.confirm_body')}</p>
          <p className="text-muted">{i18nT('pages.overview.embedModel.confirm_detail')}</p>
        </div>
      </Modal>
    </>
  )
}
