// Expanded evidence for one finding.
//
// The findings list only carries kind / target / status, which is not enough to
// judge a result — and the original app's only affordance was "open a chat about
// it". This panel shows what the run actually recorded: what the defect is, the
// reproducing test, how each gate stage voted, the diff, the drafted PR, and the
// finding's own status history. Chat stays available, but it is no longer the
// only way to see why something was kept or thrown away.
//
// All colors come from the app's design tokens via Tailwind utilities.
import { useQuery } from '@tanstack/react-query'
import { Check, ExternalLink, Minus, X } from 'lucide-react'

import { Badge } from '../../components/ui'
import { i18nT } from '../../i18n/t'

const API = '/api/apps/auto-improvement'

// Wire-protocol prefix the backend puts on a `pr` value that is queued rather than
// drafted. Named rather than inlined so it reads as protocol, not as display copy.
const QUEUED_PREFIX = 'QUEUED:'

export interface FindingDetailData {
  fp: string
  kind?: string
  target?: string
  status?: string
  note?: string
  pr?: string
  candidate?: {
    cand_id?: string
    signature?: string
    hypothesis?: string
    evidence?: string
    severity_note?: string
    blast_radius?: string
    reproducing_test?: { test_id?: string; test_path?: string }
  }
  gate?: {
    passed?: boolean
    reason?: string
    red?: boolean
    green?: boolean
    staygreen?: boolean
    build_ok?: boolean
    lint_ok?: boolean
    collected?: boolean
    failing_tests?: string[]
    detail?: string
  }
  measurement?: { primary_delta?: number | string; noise_band?: number | string }
  archive?: { cycle?: number; primary_delta?: string; noise_band?: string; commit?: string }
  run?: { profile_id?: string; track?: string; branch?: string; base_sha?: string }
  diff?: string
  diffTruncated?: boolean
  prBody?: string
  history?: Array<{ status?: string; note?: string; ts?: number }>
  candidateStatus?: string
}

/** Tri-state gate flag: pass / fail / not-applicable. A missing flag is NOT a
 *  failure — the perf track never runs the bug ladder, and showing a red X there
 *  would misreport the run. */
function GateFlag({ label, value }: { label: string; value?: boolean }) {
  const icon =
    value === true ? (
      <Check className="lucide-inline text-ok" size={13} />
    ) : value === false ? (
      <X className="lucide-inline text-danger" size={13} />
    ) : (
      <Minus className="lucide-inline text-muted" size={13} />
    )
  return (
    <span className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[11px]">
      {icon}
      {label}
    </span>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-2 py-0.5 text-[12px]">
      <span className="w-28 shrink-0 text-muted">{label}</span>
      <span className="min-w-0 flex-1 break-words">{children}</span>
    </div>
  )
}

export default function FindingDetail({ fp }: { fp: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['auto-improvement-finding', fp],
    queryFn: async () => {
      const res = await fetch(`${API}/findings/${encodeURIComponent(fp)}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return (await res.json()).finding as FindingDetailData
    },
  })

  if (isLoading) {
    return <div className="p-2 text-[12px] text-muted">{i18nT('autoImprovement.loadingDetail')}</div>
  }
  if (error || !data) {
    return <div className="p-2 text-[12px] text-warn">{i18nT('autoImprovement.detailUnavailable')}</div>
  }

  const cand = data.candidate
  const gate = data.gate
  const prUrl = data.pr && data.pr.startsWith('http') ? data.pr : ''
  const queued = data.pr && data.pr.startsWith(QUEUED_PREFIX) ? data.pr : ''

  return (
    <div className="flex flex-col gap-3 rounded border border-border bg-panel p-3">
      {/* What the defect is */}
      {cand?.signature ? (
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wide text-muted">
            {i18nT('autoImprovement.defectHeading')}
          </div>
          <p className="text-[13px]">{cand.signature}</p>
        </div>
      ) : null}

      {cand?.hypothesis ? (
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wide text-muted">
            {i18nT('autoImprovement.hypothesisHeading')}
          </div>
          <p className="text-[13px]">{cand.hypothesis}</p>
        </div>
      ) : null}

      {/* How each gate stage voted */}
      {gate && Object.keys(gate).length > 0 ? (
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wide text-muted">
            {i18nT('autoImprovement.gateHeading')}
          </div>
          <div className="flex flex-wrap gap-1">
            <GateFlag label={i18nT('apps.autoImprovement.findingDetail.red')} value={gate.red} />
            <GateFlag label={i18nT('apps.autoImprovement.findingDetail.green')} value={gate.green} />
            <GateFlag label={i18nT('apps.autoImprovement.findingDetail.staygreen')} value={gate.staygreen} />
            <GateFlag label={i18nT('apps.autoImprovement.findingDetail.build')} value={gate.build_ok} />
            <GateFlag label={i18nT('apps.autoImprovement.findingDetail.lint')} value={gate.lint_ok} />
            <GateFlag label={i18nT('apps.autoImprovement.findingDetail.collects')} value={gate.collected} />
          </div>
          {gate.detail ? <p className="mt-1 text-[12px] text-muted">{gate.detail}</p> : null}
          {gate.failing_tests && gate.failing_tests.length > 0 ? (
            <p className="mt-1 text-[12px] text-danger">
              {i18nT('autoImprovement.regressed', {
                tests: gate.failing_tests.slice(0, 5).join(', '),
              })}
            </p>
          ) : null}
        </div>
      ) : null}

      {/* Facts a reviewer needs to locate and re-run the work */}
      <div>
        {cand?.reproducing_test?.test_path ? (
          <Row label={i18nT('autoImprovement.reproTest')}>
            <code className="text-[11px]">{cand.reproducing_test.test_path}</code>
          </Row>
        ) : null}
        {data.archive?.cycle !== undefined ? (
          <Row label={i18nT('autoImprovement.cycle')}>{data.archive.cycle}</Row>
        ) : null}
        {data.archive?.primary_delta ? (
          <Row label={i18nT('autoImprovement.delta')}>
            {data.archive.primary_delta}
            {data.archive.noise_band ? ` (band ${data.archive.noise_band})` : ''}
          </Row>
        ) : null}
        {data.run?.base_sha ? (
          <Row label={i18nT('autoImprovement.base')}>
            <code className="text-[11px]">
              {data.run.branch} @ {data.run.base_sha.slice(0, 10)}
            </code>
          </Row>
        ) : null}
        <Row label={i18nT('autoImprovement.fingerprint')}>
          <code className="text-[11px]">{data.fp}</code>
        </Row>
        {data.note ? <Row label={i18nT('autoImprovement.gateNote')}>{data.note}</Row> : null}
      </div>

      {/* The pull request, or the fact that it only reached the local queue */}
      {prUrl ? (
        <Row label={i18nT('autoImprovement.colPr')}>
          <a
            href={prUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-accent hover:underline"
          >
            {prUrl}
            <ExternalLink className="lucide-inline" size={11} />
          </a>
        </Row>
      ) : queued ? (
        <Row label={i18nT('autoImprovement.colPr')}>
          <Badge variant="warn">{i18nT('autoImprovement.queuedOnly')}</Badge>
        </Row>
      ) : null}

      {/* Status history — a finding that was seen, gated out, then deduped tells
          a story the latest status alone hides. */}
      {data.history && data.history.length > 1 ? (
        <div>
          <div className="mb-1 text-[11px] uppercase tracking-wide text-muted">
            {i18nT('autoImprovement.historyHeading')}
          </div>
          <ol className="flex flex-wrap items-center gap-1 text-[11px]">
            {data.history.map((h, i) => (
              <li key={i} className="flex items-center gap-1">
                {i > 0 ? <span className="text-muted">→</span> : null}
                <span className="rounded border border-border px-1.5 py-0.5">{h.status}</span>
              </li>
            ))}
          </ol>
        </div>
      ) : null}

      {/* The change itself */}
      {data.diff ? (
        <details>
          <summary className="cursor-pointer text-[12px] text-muted hover:text-accent">
            {i18nT('autoImprovement.showDiff')}
          </summary>
          <pre className="mt-1 max-h-72 overflow-auto rounded border border-border bg-card p-2 font-mono text-[11px] leading-snug">
            {data.diff}
            {data.diffTruncated ? `\n\n… ${i18nT('autoImprovement.truncated')}` : ''}
          </pre>
        </details>
      ) : null}

      {data.prBody ? (
        <details>
          <summary className="cursor-pointer text-[12px] text-muted hover:text-accent">
            {i18nT('autoImprovement.showPrBody')}
          </summary>
          <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded border border-border bg-card p-2 font-mono text-[11px] leading-snug">
            {data.prBody}
          </pre>
        </details>
      ) : null}
    </div>
  )
}
