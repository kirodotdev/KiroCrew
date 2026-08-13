// Spec Builder — native builtin port of the external kiro-specs app.
//
// Talk through an idea in chat, review the generated plan (requirements /
// design / tasks), then hand it to an agent to build. Three-column layout:
// a collapsible specs rail, the native chat (ChatEmbed), and a docs card with
// selection-to-comment review + phase-gated approvals.
//
// ChatEmbed depends on the app-sdk's useAppApi(), which requires an
// <AppApiProvider>. Builtin pages are NOT wrapped by AppHost, so this page
// mounts its own scoped provider (limited to /api/chat for the embed).
import { useState, useEffect, useCallback, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { AppApiProvider } from '../../app-sdk'
import { specApi, LS, type SpecSummary } from './api'
import { Btn } from './components/shared'
import Workspace from './components/Workspace'
import NewSpecView from './components/NewSpecView'
import SettingsModal from './components/SettingsModal'

import { i18nT } from '../../i18n/t'
// ChatEmbed only needs the chat endpoints; scope the provider tightly.
//
// NOT /api/approvals. A mid-turn permission prompt is actionable through
// POST /api/chat/slots/{slot}/approve, which ChatEmbed uses deliberately:
// /api/approvals/{id}/{action} takes only approve|reject, so routing a Trust
// click through it silently downgraded trust to a one-shot approve. Granting
// the prefix here would also widen the page past app.json's declared
// permissions.api, which lists /api/chat and /api/chat/* only.
const CHAT_API_PATHS = ['/api/chat']

function SpecBuilderInner() {
  const [sel, setSelRaw] = useState<string | null>(() => {
    try { return localStorage.getItem(LS.lastOpen) || null } catch { return null }
  })
  const setSel = useCallback((name: string | null) => {
    setSelRaw(name)
    try {
      if (name) localStorage.setItem(LS.lastOpen, name)
      else localStorage.removeItem(LS.lastOpen)
    } catch { /* private mode — ignore */ }
  }, [])
  const [creating, setCreating] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [err, setErr] = useState('')

  // React Query rather than useState + setInterval. Two overlapping manual
  // polls could resolve OUT OF ORDER — a slow earlier request landing after a
  // fast later one replaced fresh server state with stale data, so a spec that
  // had just appeared (or just been removed) flickered back. React Query keeps
  // one in-flight request per key and discards superseded results, which is
  // also the repo's standard for server state.
  const specsQuery = useQuery({
    queryKey: ['spec-builder', 'specs'],
    queryFn: () => specApi.list(),
    refetchInterval: 15000,
  })
  // useMemo so the array identity is stable across renders — otherwise the
  // stale-selection effect below re-runs on every render.
  const specs: SpecSummary[] = useMemo(() => specsQuery.data?.specs ?? [], [specsQuery.data])
  const loadingSpecs = specsQuery.isPending
  const queryClient = useQueryClient()
  const loadSpecs = useCallback(
    () => { void queryClient.invalidateQueries({ queryKey: ['spec-builder', 'specs'] }) },
    [queryClient],
  )

  // Surface a fetch error without clobbering the last good list.
  useEffect(() => {
    if (specsQuery.error) setErr((specsQuery.error as Error).message)
  }, [specsQuery.error])

  // Drop a restored selection that no longer exists (spec removed elsewhere).
  useEffect(() => {
    if (specsQuery.isPending || !sel) return
    if (!specs.some((s) => s.name === sel)) setSelRaw(null)
  }, [specsQuery.isPending, specs, sel])

  return (
    <div
      className="w-full h-full min-h-0 overflow-hidden box-border flex flex-col text-text bg-bg relative"
      // Full-bleed shell (Issue Radar's convention): columns run to the edges and
      // are separated by borders + drag handles, so no page gutters here. The
      // new-spec view is a centred form page, so it keeps its own padding.
      style={creating ? { padding: '18px 22px' } : undefined}
    >
      {creating && (
        <div className="flex items-end justify-between gap-4 mb-3.5 shrink-0">
          <div>
            <div className="text-[22px] font-bold tracking-tight text-text-strong">{i18nT('apps.specBuilder.specBuilderPage.spec_builder')}</div>
            <div className="text-[13px] text-muted mt-0.5">{i18nT('apps.specBuilder.specBuilderPage.talk_through_an_idea_review_the_plan_then_let_an')}</div>
          </div>
        </div>
      )}

      {err && (
        <div role="alert" aria-live="assertive" className="bg-danger-subtle text-danger px-3.5 py-2 text-[12px] shrink-0 flex justify-between items-center border-b border-border">
          <span>{err}</span>
          <Btn label={<X className="lucide-inline" />} ariaLabel={i18nT('apps.specBuilder.specBuilderPage.dismiss_error')} onClick={() => setErr('')} />
        </div>
      )}

      {creating ? (
        <NewSpecView
          onCancel={() => setCreating(false)}
          onCreated={(name) => { setCreating(false); loadSpecs(); setSel(name) }}
          setErr={setErr}
          onSettings={() => setShowSettings(true)}
        />
      ) : (
        <Workspace
          specs={specs}
          sel={sel}
          setSel={setSel}
          setErr={setErr}
          onNew={() => setCreating(true)}
          loading={loadingSpecs}
          onSettings={() => setShowSettings(true)}
        />
      )}

      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} setErr={setErr} />}
    </div>
  )
}

export default function SpecBuilderPage() {
  const navigate = useNavigate()
  return (
    <AppApiProvider
      appName="spec-builder"
      allowedApiPaths={CHAT_API_PATHS}
      allowedEvents={[]}
      subscribeFn={() => () => {}}
      navigateFn={(path) => navigate(path)}
      notifyFn={(message, opts) => window.dispatchEvent(new CustomEvent('mc:notify', { detail: { message, ...opts } }))}
    >
      <SpecBuilderInner />
    </AppApiProvider>
  )
}
