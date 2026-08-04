import { useState, useEffect, useCallback, useRef } from 'react'
import { Cpu, CheckCircle, XCircle, Loader2 } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Btn, Input, Badge } from '../../components/ui'
import Modal from '../../components/Modal'
import { i18nT } from '../../i18n/t'

/** Live re-embed progress, mirrored from the backend's ReembedProgress. */
export interface ReembedState {
  step?: string          // idle | applying | running | done | failed
  done?: number
  total?: number
  error?: string
}

export interface EmbedModelStatus {
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

const POLL_MS = 2000

export default function EmbeddingModelCard() {
  const [status, setStatus] = useState<EmbedModelStatus | null>(null)
  const [path, setPath] = useState('')
  const [touched, setTouched] = useState(false)
  const [checking, setChecking] = useState(false)
  const [checked, setChecked] = useState<{ ok: boolean; msg: string } | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [applying, setApplying] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    try {
      const s = await api.vectorEmbeddingStatus() as EmbedModelStatus
      setStatus(s)
      // Seed the field from config once, so the user edits their real value
      // rather than retyping it. Never clobber in-progress typing.
      if (!touched) setPath(s.model_path || '')
    } catch { /* the Memory card surfaces connection errors */ }
  }, [touched])

  useEffect(() => { load() }, [load])

  // Poll only while something is in flight, then stop — the same discipline the
  // Memory card's download poll uses, so an idle dashboard is not chatty.
  useEffect(() => {
    const busy = reembedBusy(status?.reembed)
    if (busy && !pollRef.current) {
      pollRef.current = setInterval(load, POLL_MS)
    } else if (!busy && pollRef.current) {
      clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [status?.reembed, load])

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current) }, [])

  const isCustom = status?.model_source === 'custom'
  const activePath = status?.model_path || ''
  const dirty = touched && path.trim() !== activePath
  const reembed = status?.reembed
  const busy = reembedBusy(reembed)
  const pct = reembedPct(reembed)
  const bar = reembedBar(reembed)

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
      setChecked({ ok: false, msg: embedModelErrorMessage(e) })
    } finally { setChecking(false) }
  }, [path])

  const apply = useCallback(async () => {
    setConfirmOpen(false)
    setApplying(true)
    try {
      await api.vectorApplyEmbedModel(path.trim())
      setTouched(false)
      setChecked(null)
      await load()
    } catch (e) {
      setChecked({ ok: false, msg: embedModelErrorMessage(e) })
    } finally { setApplying(false) }
  }, [path, load])

  return (
    <>
      <Card data-testid="embed-model-card">
        <CardTitle><Cpu className="lucide-inline" /> {i18nT('pages.overview.embedModel.title')}</CardTitle>
        <div className="text-[13px] text-muted mb-3">{i18nT('pages.overview.embedModel.subtitle')}</div>

        {/* Active model */}
        <div className="flex items-center justify-between gap-2 px-2.5 py-1.5 bg-bg-elevated border border-border rounded mb-3">
          <span className="text-[13px]">
            {/* A gated candidate reports dim 0 until it finishes loading, and the
              * status endpoint passes that through — rendering "· 0d" mid-swap.
              * Treat an unknown width as an unknown model. */}
            {status?.model_id && status.model_dim
              ? i18nT('pages.overview.embedModel.active', { model: status.model_id, dim: status.model_dim })
              : i18nT('pages.overview.embedModel.active_unknown')}
          </span>
          <Badge variant={isCustom ? 'aim' : 'ok'}>
            {isCustom ? i18nT('pages.overview.embedModel.badge_custom') : i18nT('pages.overview.embedModel.badge_bundled')}
          </Badge>
        </div>

        {/* Path field */}
        <label className="block text-[11px] text-muted mb-1" htmlFor="embed-model-path">
          {i18nT('pages.overview.embedModel.path_label')}
        </label>
        <Input
          id="embed-model-path"
          className="w-full"
          value={path}
          placeholder={i18nT('pages.overview.embedModel.path_placeholder')}
          disabled={busy || applying}
          onChange={(e: React.ChangeEvent<HTMLInputElement>) => { setTouched(true); setPath(e.target.value); setChecked(null) }}
          onBlur={check}
        />
        <div className="text-[11px] text-muted mt-1">{i18nT('pages.overview.embedModel.path_hint')}</div>

        {checking && (
          <div className="text-[11px] text-muted mt-1.5 flex items-center gap-1">
            <Loader2 className="lucide-inline animate-spin" /> {i18nT('pages.overview.embedModel.checking')}
          </div>
        )}
        {checked && !checking && (
          <div className={`text-[11px] mt-1.5 flex items-start gap-1 ${checked.ok ? 'text-ok' : 'text-danger'}`}>
            {checked.ok ? <CheckCircle className="lucide-inline" /> : <XCircle className="lucide-inline" />}
            <span>{checked.msg}</span>
          </div>
        )}

        <div className="flex items-center gap-2 mt-3">
          <Btn
            primary
            onClick={() => setConfirmOpen(true)}
            disabled={!dirty || busy || applying || checking || checked?.ok === false}
          >
            {applying ? i18nT('pages.overview.embedModel.applying') : i18nT('pages.overview.embedModel.apply')}
          </Btn>
          <span className="text-[11px] text-muted">{i18nT('pages.overview.embedModel.no_restart')}</span>
        </div>

        {/* Re-embed progress */}
        {reembed && reembed.step !== 'idle' && (
          <div className="mt-3.5 pt-3 border-t border-border">
            <div className="flex items-center justify-between text-[13px] mb-1.5">
              <span>
                {reembed.step === 'applying' && i18nT('pages.overview.embedModel.loading_model')}
                {reembed.step === 'running' && i18nT('pages.overview.embedModel.reembedding')}
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
            <div
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
            </div>
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

      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title={i18nT('pages.overview.embedModel.confirm_title')}
        maxWidth={520}
        footer={
          <div className="flex justify-end gap-2">
            <Btn onClick={() => setConfirmOpen(false)}>
              {i18nT('pages.overview.embedModel.cancel')}
            </Btn>
            <Btn primary onClick={apply}>{i18nT('pages.overview.embedModel.confirm_apply')}</Btn>
          </div>
        }
      >
        <div className="text-[13px] space-y-2">
          <p>{i18nT('pages.overview.embedModel.confirm_body')}</p>
          <p className="text-muted">{i18nT('pages.overview.embedModel.confirm_detail')}</p>
        </div>
      </Modal>
    </>
  )
}
