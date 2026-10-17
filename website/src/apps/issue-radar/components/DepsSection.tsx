// "Blocked by / Blocking" — the detail-pane dependency section (#5187 M2),
// shared by IssueDetail and PrDetail.
//
// Driven by the SAME `/deps` payload the Graph tab uses (react-query dedupes the
// fetch by key), it lists the items this issue/PR is blocked by and the items it
// blocks. Each row reuses the in-app reference affordance (`openRef`) so a click
// opens the target in the RefSheet, exactly like the "Linked" section and a
// `#N` link in the body.
//
// The backend `/deps` route lands in a separate PR, so a 404/error/empty answer
// renders NOTHING (the section simply does not appear) — this must never surface
// an error in a detail pane that is otherwise fine.
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { CircleDot, GitPullRequest, Lock, Unlock } from 'lucide-react'
import { useIssueRadar } from '../context'
import { issueRadarApi, type DepEdge, type DepsResponse } from '../api'
import { repoScopeKey } from '../lib/links'
import { resolveLifecycle, hasSeparateNumberSpaces, type NodeKind } from '../lib/deps'
import { i18nT } from '../../../i18n/t'

/** One resolved dependency row: the counterpart number, its kind/lifecycle, and
 * the edge source (native vs inferred). */
interface DepRow {
  number: number
  kind: 'issue' | 'pull'
  lifecycle: 'open' | 'closed' | 'merged'
  title: string
  source: DepEdge['source']
}

/** Icon tint by lifecycle — an open counterpart reads live (accent), a
 * closed/merged one reads muted. */
function tint(lifecycle: DepRow['lifecycle']): string {
  return lifecycle === 'open' ? 'text-accent' : 'text-muted'
}

export default function DepsSection({ number, kind }: { number: number; kind: NodeKind }) {
  const { active, issues, pulls, openRef } = useIssueRadar()
  const scopeKey = repoScopeKey(active)

  // Same key as GraphView — one fetch serves both surfaces.
  const depsQuery = useQuery({
    queryKey: ['issue-radar', 'deps', scopeKey],
    queryFn: () => issueRadarApi.deps(active),
    retry: false,
  })
  const deps: DepsResponse | null = depsQuery.data ?? null

  const { blockedBy, blocking } = useMemo(() => {
    if (!deps) return { blockedBy: [] as DepRow[], blocking: [] as DepRow[] }
    const issueByNumber = new Map(issues.map((i) => [i.number, i]))
    const pullByNumber = new Map(pulls.map((p) => [p.number, p]))
    // One lookup shared with the Graph tab, so the two surfaces can never show
    // one number with two different lifecycles (see lib/deps.resolveLifecycle for
    // the per-forge number-space rule).
    const resolve = (n: number, src: DepEdge['source']): DepRow => {
      const r = resolveLifecycle(n, deps, issueByNumber, pullByNumber, active.provider)
      return { number: n, kind: r.kind, lifecycle: r.lifecycle, title: r.title, source: src }
    }
    const bb: DepRow[] = []
    const bl: DepRow[] = []
    // On a separate-number-space forge (GitLab) the deps graph is built from
    // issue links only, so every edge endpoint is an issue. An edge therefore
    // belongs to THIS item only when the payload node at that number is this
    // item's own kind: a GitLab MR (iid 6) must not borrow issue #6's rows.
    // GitHub shares one number space, so this gate is a no-op there.
    const ownsEndpoint = (n: number): boolean => {
      if (!hasSeparateNumberSpaces(active.provider)) return true
      const node = deps.nodes[String(n)]
      return node !== undefined && node.kind === kind
    }
    for (const e of deps.edges) {
      if (e.blocked === number && ownsEndpoint(e.blocked)) bb.push(resolve(e.blocker, e.source))
      if (e.blocker === number && ownsEndpoint(e.blocker)) bl.push(resolve(e.blocked, e.source))
    }
    bb.sort((a, b) => a.number - b.number)
    bl.sort((a, b) => a.number - b.number)
    return { blockedBy: bb, blocking: bl }
  }, [deps, number, kind, issues, pulls, active.provider])

  if (blockedBy.length === 0 && blocking.length === 0) return null

  // Every blocker closed/merged => this item is unblocked.
  const allBlockersMet = blockedBy.length > 0 && blockedBy.every((r) => r.lifecycle !== 'open')

  return (
    <section className="mb-6">
      <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted mb-3 font-medium">
        {allBlockersMet ? <Unlock size={12} className="text-accent" /> : <Lock size={12} />}
        {i18nT('apps.issueRadar.components.depsSection.title')}
        <span className="text-muted normal-case tracking-normal opacity-70">
          · {blockedBy.length + blocking.length}
        </span>
      </div>

      {blockedBy.length > 0 && (
        <DepGroup
          heading={i18nT('apps.issueRadar.components.depsSection.blocked_by')}
          rows={blockedBy}
          onOpen={(r) => openRef({ kind: r.kind, number: r.number })}
        />
      )}
      {blocking.length > 0 && (
        <DepGroup
          heading={i18nT('apps.issueRadar.components.depsSection.blocking')}
          rows={blocking}
          onOpen={(r) => openRef({ kind: r.kind, number: r.number })}
        />
      )}
    </section>
  )
}

function DepGroup({ heading, rows, onOpen }: {
  heading: string; rows: DepRow[]; onOpen: (r: DepRow) => void
}) {
  return (
    <div className="mb-2.5 last:mb-0">
      <div className="text-[10.5px] text-muted opacity-80 mb-1.5">{heading}</div>
      <div className="flex flex-col gap-1.5">
        {rows.map((r) => {
          const sigil = r.kind === 'pull' ? 'PR' : '#'
          const stateLabel =
            r.lifecycle === 'merged'
              ? i18nT('apps.issueRadar.components.depsSection.state_merged')
              : r.lifecycle === 'closed'
                ? i18nT('apps.issueRadar.components.depsSection.state_closed')
                : i18nT('apps.issueRadar.components.depsSection.state_open')
          const srcLabel =
            r.source === 'inferred'
              ? i18nT('apps.issueRadar.components.depsSection.source_inferred')
              : i18nT('apps.issueRadar.components.depsSection.source_native')
          return (
            <button
              key={`${r.kind}-${r.number}`}
              type="button"
              onClick={() => onOpen(r)}
              title={i18nT('apps.issueRadar.components.depsSection.open_here', { sigil, number: r.number })}
              className="group flex items-start gap-2.5 rounded-lg border border-border bg-card px-3.5 py-2.5 text-left cursor-pointer hover:border-accent/50 transition-colors"
            >
              <span className={`mt-0.5 flex-shrink-0 ${tint(r.lifecycle)}`}>
                {r.kind === 'pull' ? <GitPullRequest size={15} /> : <CircleDot size={15} />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[13px] text-text group-hover:text-accent leading-snug line-clamp-2 break-words">
                  {r.title || `${sigil}${r.number}`}
                </span>
                <span className="mt-0.5 flex items-center gap-1.5 flex-wrap text-[11.5px] text-muted">
                  <span className="font-mono">{sigil}{r.number}</span>
                  <span>· {stateLabel}</span>
                  <span className="opacity-70">· {srcLabel}</span>
                </span>
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
