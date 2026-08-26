// Auto-Improvement — dashboard page (/auto-improvement).
//
// Measures a GitHub repository before it changes it: the ruler is calibrated and
// proved first, then keep-or-revert cycles draft survivors as DRAFT pull requests.
// This page is the read + discuss surface over that: ruler trust state, the
// findings ledger, and live PR status pulled through the gateway's own provider.
//
// Every row can open a RESUMABLE chat session about its subject (see
// lib/agentSession.ts) — a repeat click returns to the same conversation instead
// of starting a new one, which is the main upgrade over the app this was ported
// from.
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Brain,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  GitBranch,
  GitCommitVertical,
  GitPullRequest,
  MessageSquare,
  RefreshCw,
  Ruler,
} from 'lucide-react'

import Clickable from '../../components/Clickable'
import { Badge, Card, CardTitle, StatCard } from '../../components/ui'
import { i18nT } from '../../i18n/t'

import FindingDetail from './FindingDetail'
import SetupPanel from './SetupPanel'
import { useAgentSession, truncate } from './lib/agentSession'
import { findingPrompt, prPrompt, rulerPrompt } from './lib/prompts'

const API = '/api/apps/auto-improvement'

interface RulerState {
  status: string
  primary?: { name?: string; unit?: string; direction?: string; label?: string }
  noiseBand?: { value?: number; unit?: string; method?: string }
  canary?: { result?: string; clearedBand?: boolean }
}

interface Finding {
  fp: string
  kind: string
  target: string
  status: string
  note?: string
  /** The pull request reference. ``cr`` is the spine's historical field name for
   *  the same thing; both are read so a link renders either way. */
  pr?: string
  cr?: string
  ts?: string
}

interface PrStatus {
  ok: boolean
  url: string
  number?: number
  title?: string
  state?: string
  draft?: boolean
  mergeable?: string
  verdict?: string
  verdictReason?: string
  checks?: { label?: string; failingCount?: number }
  error?: string
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return (await res.json()) as T
}

/** How many ledger rows the findings list renders.
 *
 *  The ledger grows without bound, and every expanded row mounts a detail query, so
 *  the list is capped. The cap is named rather than inlined because the count has to
 *  be shown to the reader — a silent cap under a stat card reporting the FULL count
 *  is just two numbers that disagree. */
const FINDINGS_SHOWN = 40

/** Catalog key per ledger status, so the row shows prose instead of `failed_gate`.
 *
 *  A STATIC map rather than an assembled `autoImprovement.status_${s}`: `dynamicKeys`
 *  forbids building a key, and `check-i18n-keys` resolves a map's values as a finite set —
 *  so every label here is verified to exist. Statuses come from `spine/ledger.py`'s
 *  `VALID_STATUSES`; an unknown one falls back to the raw token, which is better than
 *  rendering a dotted key path. */
const STATUS_LABEL_KEY = {
  seen: 'autoImprovement.statusSeen',
  filed: 'autoImprovement.statusFiled',
  committed: 'autoImprovement.statusCommitted',
  discarded_noise: 'autoImprovement.statusDiscardedNoise',
  failed_gate: 'autoImprovement.statusFailedGate',
  failed_verify: 'autoImprovement.statusFailedVerify',
  duplicate: 'autoImprovement.statusDuplicate',
  error: 'autoImprovement.statusError',
  no_defect: 'autoImprovement.statusNoDefect',
  purged: 'autoImprovement.statusPurged',
} as const

/** Ledger statuses that mean a change is queued and can still be committed. */
const DRAFTED_STATUSES = new Set(['filed'])

/** Already landed on the branch — there is nothing left to commit, only to view. */
const COMMITTED_STATUS = 'committed'

/** A short commit sha, as the direct-commit path records it in the ledger's `cr`. */
const SHA_RE = /^[0-9a-f]{7,40}$/i

/** The GitHub URL for a committed finding's sha, or null when we cannot build one.
 *
 *  The direct-commit path stores a bare sha (e.g. `1537c449`) rather than a url, so
 *  the generic `prUrlOf` — which only accepts `http…` — rendered NOTHING for a
 *  committed finding and the row instead showed a re-commit button. `repo` is the
 *  `owner/name` display string the config already carries. */
function commitUrlOf(finding: Finding, repo: string): string | null {
  const sha = (finding.pr || finding.cr || '').trim()
  if (!SHA_RE.test(sha)) return null
  // Only build a url for an `owner/name` we recognize; never guess a host.
  if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) return null
  return `https://github.com/${repo}/commit/${sha}`
}

/** Map a watcher verdict onto the shared badge vocabulary. */
function verdictVariant(verdict?: string): 'ok' | 'err' | 'warn' | 'muted' {
  if (verdict === 'READY') return 'ok'
  if (verdict === 'BLOCKED') return 'err'
  if (verdict === 'PROGRESS') return 'warn'
  return 'muted'
}

/** A PR url stored on a ledger row, when it is a real url rather than a queue id.
 *
 *  Reads BOTH keys on purpose: the spine's ledger field is historically named
 *  `cr` (the app was ported from a change-request world), so a UI that only read
 *  `pr` never rendered a link even once a pull request existed. */
function prUrlOf(finding: Finding): string | null {
  const pr = finding.pr || finding.cr || ''
  return pr.startsWith('http') ? pr : null
}

/** The `/pr-status` path plus its encoded query, as one string. */
function prStatusQuery(url: string): string {
  // The '?' is concatenated rather than written inside the template literal: a
  // literal ending in '?' cannot be exempted by shape without also exempting
  // prose questions ("are you sure?"), so the i18n scanner would flag it.
  const query = new URLSearchParams({ url })
  return '/pr-status' + '?' + query
}

/** Badge tone for a ledger status, so the list is scannable at a glance. */
function statusVariant(status: string): 'ok' | 'err' | 'warn' | 'muted' {
  if (status === 'filed' || status === 'committed') return 'ok'
  if (status === 'failed_gate' || status === 'failed_verify' || status === 'error') return 'err'
  if (status === 'discarded_noise' || status === 'no_defect') return 'warn'
  return 'muted'
}

function PrRow({ finding, repo }: { finding: Finding; repo: string }) {
  const url = prUrlOf(finding)
  const { openSession, busy } = useAgentSession()
  // Only poll a row that actually has a pull request; a queued finding has
  // nothing to fetch and would burn a request per refresh interval.
  const { data, refetch, isFetching } = useQuery({
    queryKey: ['auto-improvement-pr', url],
    // URLSearchParams encodes for us, and building the whole query (leading `?`
    // included) inside the interpolation keeps every string literal here a plain
    // path — no `?url=`-shaped fragment for the i18n scanner to mistake for copy.
    queryFn: () => getJson<PrStatus>(`${API}${prStatusQuery(url as string)}`),
    enabled: Boolean(url),
    refetchInterval: 60_000,
  })

  if (!url) return null

  const discuss = () =>
    openSession({
      kind: 'pr',
      id: data?.number ?? finding.fp,
      repo,
      title: `${i18nT('autoImprovement.prTitlePrefix')} #${data?.number ?? '?'} · ${truncate(data?.title || finding.target, 32)}`,
      url,
      prompt: prPrompt({
        number: data?.number ?? 0,
        title: data?.title || finding.target,
        url,
        verdict: data?.verdict,
        verdictReason: data?.verdictReason,
        checks: data?.checks?.label,
        mergeable: data?.mergeable,
      }),
    })

  return (
    <tr className="border-t border-border">
      <td className="py-2 pr-3 align-top">
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-[13px] text-accent hover:underline"
        >
          <GitPullRequest className="lucide-inline" size={14} />
          {data?.number ? `#${data.number}` : i18nT('autoImprovement.draftPr')}
          <ExternalLink className="lucide-inline" size={12} />
        </a>
      </td>
      <td className="py-2 pr-3 align-top text-[13px]">{data?.title || finding.target}</td>
      <td className="py-2 pr-3 align-top">
        {data?.ok === false ? (
          <Badge variant="muted">{i18nT('autoImprovement.statusUnavailable')}</Badge>
        ) : (
          <Badge variant={verdictVariant(data?.verdict)}>{data?.verdict || '—'}</Badge>
        )}
      </td>
      <td className="py-2 pr-3 align-top text-[13px] text-muted">
        {data?.checks?.label || '—'}
        {data?.verdictReason ? <div className="text-[11px] opacity-80">{data.verdictReason}</div> : null}
      </td>
      <td className="py-2 align-top">
        <div className="flex items-center gap-1">
          <Clickable
            onClick={() => refetch()}
            aria-label={i18nT('autoImprovement.refreshPr')}
            title={i18nT('autoImprovement.refreshPr')}
            className="rounded p-1 hover:bg-accent/20"
          >
            <RefreshCw className={`lucide-inline ${isFetching ? 'animate-spin' : ''}`} size={14} />
          </Clickable>
          <Clickable
            onClick={discuss}
            aria-label={i18nT('autoImprovement.discussPr')}
            title={i18nT('autoImprovement.discussPr')}
            className="rounded p-1 hover:bg-accent/20"
          >
            <MessageSquare className={`lucide-inline ${busy ? 'opacity-50' : ''}`} size={14} />
          </Clickable>
        </div>
      </td>
    </tr>
  )
}

export default function AutoImprovementPage() {
  const { openSession } = useAgentSession()
  // Fingerprint of the finding whose evidence panel is open, or null. One at a
  // time: each panel fetches its own detail, and the interesting comparison is
  // between a finding and the code, not between two findings.
  const [expanded, setExpanded] = useState<string | null>(null)
  const { data: config } = useQuery({
    queryKey: ['auto-improvement-config'],
    queryFn: () => getJson<Record<string, unknown>>(`${API}/config`),
  })
  const { data: ruler } = useQuery({
    queryKey: ['auto-improvement-ruler'],
    queryFn: () => getJson<RulerState>(`${API}/ruler`),
    refetchInterval: 30_000,
  })
  const { data: findingsResp } = useQuery({
    queryKey: ['auto-improvement-findings'],
    queryFn: () => getJson<{ findings: Finding[] }>(`${API}/findings`),
    refetchInterval: 30_000,
  })

  const qc = useQueryClient()
  const findings = findingsResp?.findings ?? []
  const repo = String(config?.target_display || config?.target_url || 'repository')
  // The branch this run's findings belong to. Shown so it is always clear WHICH
  // repository+branch the list is scoped to — the findings set changes when
  // either does.
  const branch = String(config?.branch || '').replace(/^origin\//, '') || 'default'
  const calibrated = ruler?.status === 'calibrated'
  const drafted = findings.filter((f) => DRAFTED_STATUSES.has(f.status))
  const withPr = drafted.filter((f) => prUrlOf(f))

  // Commit a queued change straight to the branch. Refreshes the findings + PR list so the
  // row reflects the new state.
  //
  // `res.ok` is CHECKED and the error thrown: the route answers a refusal with HTTP 400 and
  // `{code, error}` (a protected branch, a push-policy denial, a run in progress), and the
  // old body-only `.then(r => r.json())` resolved those as SUCCESS — so react-query ran
  // `onSuccess`, the pulse stopped, and the operator saw no change and no reason. Throwing
  // routes it to `commitFinding.error`, which the row renders. Raised by the UX review.
  const commitFinding = useMutation({
    mutationFn: async (fp: string) => {
      const res = await fetch(`${API}/findings/${encodeURIComponent(fp)}/commit`, {
        method: 'POST',
      })
      const body = (await res.json().catch(() => ({}))) as { ok?: boolean; error?: string }
      if (!res.ok || body.ok === false) {
        throw new Error(body.error || `HTTP ${res.status}`)
      }
      return body
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['auto-improvement-findings'] })
      qc.invalidateQueries({ queryKey: ['auto-improvement-finding'] })
    },
  })

  const discussRuler = () =>
    openSession({
      kind: 'ruler',
      id: 'current',
      repo,
      title: i18nT('autoImprovement.rulerSessionTitle'),
      prompt: rulerPrompt({
        status: ruler?.status || 'uncalibrated',
        primary: ruler?.primary?.label || ruler?.primary?.name,
        noiseBand:
          ruler?.noiseBand?.value !== undefined
            ? `${ruler.noiseBand.value}${ruler.noiseBand.unit || ''}`
            : undefined,
        canary: ruler?.canary?.result,
      }),
    })

  return (
    <div className="mx-auto flex w-full max-w-[1100px] flex-col gap-4 p-4">
      <header className="flex items-center gap-2">
        <Brain className="lucide-inline" size={20} />
        <h1 className="text-[17px] font-semibold">{i18nT('autoImprovement.title')}</h1>
      </header>
      <p className="text-[13px] text-muted">{i18nT('autoImprovement.intro')}</p>

      <SetupPanel config={config} />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard
          label={i18nT('autoImprovement.rulerStat')}
          value={calibrated ? i18nT('autoImprovement.calibrated') : i18nT('autoImprovement.uncalibrated')}
          colorClass={calibrated ? undefined : 'text-warn'}
        />
        <StatCard label={i18nT('autoImprovement.findingsStat')} value={findings.length} />
        <StatCard label={i18nT('autoImprovement.keptStat')} value={drafted.length} />
        <StatCard label={i18nT('autoImprovement.prStat')} value={withPr.length} />
      </div>

      <Card>
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2">
            <Ruler className="lucide-inline" size={15} />
            {i18nT('autoImprovement.rulerHeading')}
          </CardTitle>
          <Clickable
            onClick={discussRuler}
            aria-label={i18nT('autoImprovement.discussRuler')}
            title={i18nT('autoImprovement.discussRuler')}
            className="rounded p-1 hover:bg-accent/20"
          >
            <MessageSquare className="lucide-inline" size={14} />
          </Clickable>
        </div>
        {calibrated ? (
          <p className="text-[13px] text-muted">
            {ruler?.primary?.label || ruler?.primary?.name || i18nT('autoImprovement.rulerReady')}
            {ruler?.noiseBand?.value !== undefined
              ? ` · ${i18nT('autoImprovement.band')} ${ruler.noiseBand.value}${ruler.noiseBand.unit || ''}`
              : ''}
          </p>
        ) : (
          <p className="text-[13px] text-muted">{i18nT('autoImprovement.rulerNotReady')}</p>
        )}
      </Card>

      <Card>
        <CardTitle className="flex items-center gap-2">
          <GitPullRequest className="lucide-inline" size={15} />
          {i18nT('autoImprovement.changesHeading')}
        </CardTitle>
        {withPr.length === 0 ? (
          <p className="text-[13px] text-muted">{i18nT('autoImprovement.noPrs')}</p>
        ) : (
          <table className="w-full text-left">
            <thead>
              <tr className="text-[11px] uppercase tracking-wide text-muted">
                <th className="pb-2 pr-3 font-medium">{i18nT('autoImprovement.colPr')}</th>
                <th className="pb-2 pr-3 font-medium">{i18nT('autoImprovement.colChange')}</th>
                <th className="pb-2 pr-3 font-medium">{i18nT('autoImprovement.colVerdict')}</th>
                <th className="pb-2 pr-3 font-medium">{i18nT('autoImprovement.colChecks')}</th>
                <th className="pb-2 font-medium">{i18nT('autoImprovement.colActions')}</th>
              </tr>
            </thead>
            <tbody>
              {withPr.map((f) => (
                <PrRow key={f.fp} finding={f} repo={repo} />
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card>
        <div className="flex items-center justify-between gap-2">
          <CardTitle>{i18nT('autoImprovement.findingsHeading')}</CardTitle>
          {/* Which repository+branch this set belongs to — the findings change
              when either does, so the scope is always visible. */}
          <span className="inline-flex items-center gap-1 text-[12px] text-muted">
            <GitBranch className="lucide-inline" size={12} />
            <span className="text-accent">{repo}</span>
            <span>@</span>
            <span className="text-accent">{branch}</span>
          </span>
        </div>
        {findings.length === 0 ? (
          <p className="text-[13px] text-muted">{i18nT('autoImprovement.noFindings')}</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {findings.slice(0, FINDINGS_SHOWN).map((f) => (
              <li key={f.fp} className="flex flex-col gap-1 py-1 text-[13px]">
                <div className="flex items-center justify-between gap-2">
                  {/* The row itself expands — reading the evidence is the common
                      action, so it should not require hunting for a control. */}
                  <Clickable
                    onClick={() => setExpanded(expanded === f.fp ? null : f.fp)}
                    aria-label={i18nT('autoImprovement.toggleDetail')}
                    aria-expanded={expanded === f.fp}
                    className="flex min-w-0 flex-1 items-center gap-1 rounded text-left hover:bg-accent/10"
                  >
                    {expanded === f.fp ? (
                      <ChevronDown className="lucide-inline shrink-0" size={13} />
                    ) : (
                      <ChevronRight className="lucide-inline shrink-0" size={13} />
                    )}
                    <Badge variant={statusVariant(f.status)}>{f.kind}</Badge>
                    <span className="ml-1 min-w-0 truncate">{f.target}</span>
                  </Clickable>
                  <span className="flex shrink-0 items-center gap-2">
                    {prUrlOf(f) ? (
                      <a
                        href={prUrlOf(f) as string}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-[12px] text-accent hover:underline"
                      >
                        <GitPullRequest className="lucide-inline" size={12} />
                        {i18nT('autoImprovement.viewPr')}
                      </a>
                    ) : null}
                    <span className="text-muted">
                      {STATUS_LABEL_KEY[f.status as keyof typeof STATUS_LABEL_KEY]
                        ? i18nT(STATUS_LABEL_KEY[f.status as keyof typeof STATUS_LABEL_KEY])
                        : f.status}
                    </span>
                    {/* Already committed: offer the COMMIT, not a re-commit action. The
                        ledger stores a bare sha here, so this is a link out to GitHub. */}
                    {f.status === COMMITTED_STATUS && commitUrlOf(f, repo) ? (
                      <a
                        href={commitUrlOf(f, repo) as string}
                        target="_blank"
                        rel="noopener noreferrer"
                        aria-label={i18nT('autoImprovement.viewCommit')}
                        title={i18nT('autoImprovement.viewCommit')}
                        className="inline-flex items-center gap-1 text-[12px] text-accent hover:underline"
                      >
                        <GitCommitVertical className="lucide-inline" size={13} />
                        {i18nT('autoImprovement.viewCommit')}
                      </a>
                    ) : null}
                    {DRAFTED_STATUSES.has(f.status) ? (
                      <Clickable
                        // CONFIRM first. This pushes to the configured branch and a published
                        // commit cannot be recalled, yet the control sat at identical weight
                        // beside the harmless Discuss icon with no label and no prompt. The
                        // prompt names the BRANCH, because "which branch" is the fact that
                        // makes the action safe or not. Catalog copy, not a literal:
                        // `englishIdentity` forbids a hardcoded confirm string, since those
                        // are call arguments the i18n codemod never saw. Raised by the UX
                        // review.
                        onClick={() => {
                          if (
                            !window.confirm(
                              i18nT('autoImprovement.commitConfirm', { branch }),
                            )
                          ) {
                            return
                          }
                          commitFinding.mutate(f.fp)
                        }}
                        // Disabled while ANY commit is in flight, not just this row's: each
                        // one mutates the SAME clone, so two concurrent requests interleave
                        // checkout/apply/commit and publish one commit containing both
                        // findings. The backend serializes on a lock regardless; this stops
                        // the operator queueing a second mutation that then waits on it.
                        disabled={commitFinding.isPending}
                        aria-label={i18nT('autoImprovement.commitFinding')}
                        title={i18nT('autoImprovement.commitFinding')}
                        className={`inline-flex items-center rounded border border-border px-1.5 py-0.5 hover:bg-accent/20 ${
                          commitFinding.isPending ? 'cursor-not-allowed opacity-50' : ''
                        }`}
                      >
                        <GitCommitVertical
                          className={`lucide-inline ${
                            commitFinding.isPending && commitFinding.variables === f.fp
                              ? 'animate-pulse'
                              : ''
                          }`}
                          size={13}
                        />
                        {/* A text label beside the glyph: an icon-only control that publishes
                            irreversibly should not be visually interchangeable with the
                            harmless Discuss icon next to it. */}
                        <span className="ml-1 text-[12px]">
                          {i18nT('autoImprovement.commitFinding')}
                        </span>
                      </Clickable>
                    ) : null}
                    <Clickable
                      onClick={() =>
                        openSession({
                          kind: 'finding',
                          id: f.fp,
                          repo,
                          title: `${i18nT('autoImprovement.findingTitlePrefix')} ${truncate(f.target, 32)}`,
                          prompt: findingPrompt({
                            fingerprint: f.fp,
                            kind: f.kind,
                            target: f.target,
                            status: f.status,
                            note: f.note,
                            pr: prUrlOf(f) || undefined,
                          }),
                        })
                      }
                      aria-label={i18nT('autoImprovement.discussFinding')}
                      title={i18nT('autoImprovement.discussFinding')}
                      className="rounded p-1 hover:bg-accent/20"
                    >
                      <MessageSquare className="lucide-inline" size={13} />
                    </Clickable>
                  </span>
                </div>
                {/* The failure, at the row that caused it. A refusal ("branch is protected",
                    "a run is in progress") previously produced no visible change at all:
                    the pulse stopped and nothing else happened. Scoped to the row whose
                    commit failed via `variables`, so a stale error cannot sit under an
                    unrelated finding. Mirrors what `SetupPanel` already does for clone. */}
                {commitFinding.isError && commitFinding.variables === f.fp ? (
                  <p className="text-[12px] text-warn">
                    {(commitFinding.error as Error)?.message ||
                      i18nT('autoImprovement.commitFailed')}
                  </p>
                ) : null}
                {expanded === f.fp ? <FindingDetail fp={f.fp} /> : null}
              </li>
            ))}
          </ul>
        )}
        {/* The list is capped at FINDINGS_SHOWN while the stat card above reports the full
            count, so without this line the two numbers silently contradict each other. */}
        {findings.length > FINDINGS_SHOWN ? (
          <p className="mt-1 text-[12px] text-muted">
            {i18nT('autoImprovement.findingsTruncated', {
              shown: FINDINGS_SHOWN,
              total: findings.length,
            })}
          </p>
        ) : null}
      </Card>
    </div>
  )
}
