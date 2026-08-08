/**
 * Security Scanner — builtin dashboard page.
 *
 * Self-contained port of the app's UI: React hooks + fetch against the builtin
 * backend (``/api/apps/security-scanner/*``), inline styles keyed off the
 * dashboard theme CSS custom properties. Kept dependency-light (no ui-kit /
 * MarkdownRenderer imports) so it renders identically on every theme and stays
 * simple to review. "Scan Now" launches the security-scan skill in a background
 * agent slot via ``/api/chat?ws=1`` — the page never drives the scan itself.
 */
import { useCallback, useEffect, useState } from 'react'

const ACCENT = '#7c3aed'
const ACCENT_SUBTLE = '#e8d5f5'
const TABS = ['Overview', 'Findings', 'Knowledge', 'Exploit Lab', 'Settings'] as const
type Tab = (typeof TABS)[number]

const SEV_COLOR: Record<string, string> = {
  critical: '#b91c1c', high: '#b45309', medium: '#7c3aed', low: '#6b7280', info: '#6b7280',
}
const STATUS_COLOR: Record<string, string> = {
  exploited: '#b91c1c', confirmed: '#b45309', 'pattern-learned': '#7c3aed',
  blocked: '#6b7280', suppressed: '#6b7280',
}

interface Finding {
  id: string
  topic: string
  title: string
  location: string
  severity: string
  description?: string
  exploit_suggestion?: string
  status: string
  evidence?: string
}

interface Pattern {
  id: string
  topic: string
  pattern: string
  source?: string
  confidence: number
}

interface Status {
  running?: boolean
  last_scan_at?: string
  findings_total?: number
  findings_by_status?: Record<string, number>
  findings_by_severity?: Record<string, number>
  patterns_total?: number
  coverage?: Record<string, number>
  avg_false_positive_rate?: number
}

const BASE = '/api/apps/security-scanner'

async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const resp = await fetch(BASE + path, opts)
  if (!resp.ok) throw new Error('HTTP ' + resp.status)
  return (await resp.json()) as T
}

function Pill({ text, bg, fg }: { text: string; bg: string; fg: string }) {
  return (
    <span style={{ background: bg, color: fg, padding: '2px 7px', borderRadius: '9999px', fontSize: 10, fontWeight: 600, letterSpacing: '0.02em', whiteSpace: 'nowrap' }}>
      {text}
    </span>
  )
}

function Card({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return (
    <div style={{ background: 'var(--card, #1a1b26)', border: '1px solid var(--border, #2d2f3d)', borderRadius: 6, padding: 14, ...(style || {}) }}>
      {children}
    </div>
  )
}

function StatCard({ label, value, sub, color }: { label: string; value: React.ReactNode; sub?: string; color?: string }) {
  return (
    <Card style={{ flex: 1 }}>
      <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color: color || 'var(--text, #e2e8f0)', marginTop: 2 }}>{value}</div>
      {sub ? <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', marginTop: 2 }}>{sub}</div> : null}
    </Card>
  )
}

export default function SecurityScannerPage() {
  const [tab, setTab] = useState<Tab>('Overview')
  const [status, setStatus] = useState<Status | null>(null)
  const [findings, setFindings] = useState<Finding[]>([])
  const [patterns, setPatterns] = useState<Pattern[]>([])
  const [selected, setSelected] = useState<Finding | null>(null)
  const [err, setErr] = useState('')
  const [scanning, setScanning] = useState(false)
  const [ingestText, setIngestText] = useState('')
  const [ingesting, setIngesting] = useState(false)
  const [filter, setFilter] = useState('')

  const load = useCallback(async () => {
    try {
      const [st, fd, kn] = await Promise.all([
        api<Status>('/status'),
        api<{ findings: Finding[] }>('/findings'),
        api<{ patterns: Pattern[] }>('/knowledge'),
      ])
      setStatus(st)
      setFindings(fd.findings || [])
      setPatterns(kn.patterns || [])
      setErr('')
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    load()
    const id = window.setInterval(load, 30000)
    return () => window.clearInterval(id)
  }, [load])

  const scanNow = useCallback(async () => {
    setScanning(true)
    await fetch('/api/chat?ws=1', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message:
          'Run the security-scan skill against the KiroCrew codebase for all active topics now. Persist findings and notify only on new actionable findings.',
        slot: 'security-scanner-scan',
      }),
    }).catch(() => {})
    window.setTimeout(() => { setScanning(false); load() }, 6000)
  }, [load])

  const ingest = useCallback(async () => {
    if (!ingestText.trim()) return
    setIngesting(true)
    try {
      await api('/knowledge/ingest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: ingestText }),
      })
    } catch {
      /* surfaced via reload */
    }
    setIngestText('')
    setIngesting(false)
    load()
  }, [ingestText, load])

  const s = status || {}
  const cov = s.coverage || {}
  const covMax = Math.max(1, ...Object.values(cov))
  const exploited = (s.findings_by_status || {}).exploited || 0

  const tabBar = (
    <div style={{ display: 'flex', gap: 2, marginBottom: 16, background: 'var(--bg, #12131a)', borderRadius: 8, padding: 3 }}>
      {TABS.map((t) => (
        <button
          key={t}
          onClick={() => { setTab(t); setSelected(null) }}
          style={{
            fontSize: 11, padding: '6px 12px', borderRadius: 6, border: 'none', cursor: 'pointer', fontWeight: 500,
            background: tab === t ? 'var(--card, #1a1b26)' : 'transparent',
            color: tab === t ? 'var(--text, #e2e8f0)' : 'var(--muted, #6b7280)',
          }}
        >
          {t}
        </button>
      ))}
    </div>
  )

  let body: React.ReactNode
  if (err) {
    body = <Card><div style={{ fontSize: 12, color: 'var(--muted, #6b7280)' }}>Could not reach the scanner backend ({err}). If the app was just enabled, retry shortly. Retrying every 30s.</div></Card>
  } else if (selected) {
    body = (
      <Card>
        <button onClick={() => setSelected(null)} style={{ fontSize: 11, color: ACCENT, background: 'none', border: 'none', cursor: 'pointer', padding: 0, marginBottom: 8 }}>← back</button>
        <div style={{ fontSize: 14, fontWeight: 600 }}>{selected.title}</div>
        <div style={{ fontSize: 11, color: 'var(--muted, #6b7280)', margin: '4px 0 10px', display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <Pill text={selected.severity.toUpperCase()} bg="transparent" fg={SEV_COLOR[selected.severity] || '#6b7280'} />
          <Pill text={selected.status} bg="transparent" fg={STATUS_COLOR[selected.status] || '#6b7280'} />
          <span>{selected.location}</span>
          <span>{selected.topic}</span>
        </div>
        {selected.description ? <div style={{ fontSize: 12, marginBottom: 10, lineHeight: 1.5 }}>{selected.description}</div> : null}
        {selected.exploit_suggestion ? (
          <div style={{ marginBottom: 10 }}>
            <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', textTransform: 'uppercase' }}>Exploit suggestion</div>
            <div style={{ fontSize: 11, fontFamily: 'monospace', background: 'var(--bg, #12131a)', padding: '6px 8px', borderRadius: 4, marginTop: 2 }}>{selected.exploit_suggestion}</div>
          </div>
        ) : null}
        {selected.evidence ? (
          <div>
            <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', textTransform: 'uppercase' }}>Exploit evidence (secrets scrubbed)</div>
            <pre style={{ fontSize: 11, fontFamily: 'monospace', background: 'var(--bg, #12131a)', padding: 8, borderRadius: 4, marginTop: 2, whiteSpace: 'pre-wrap', overflowX: 'auto' }}>{selected.evidence}</pre>
          </div>
        ) : null}
      </Card>
    )
  } else if (tab === 'Overview') {
    body = (
      <>
        <div style={{ display: 'flex', gap: 12, marginBottom: 16 }}>
          <StatCard label="Confirmed Vulns" value={exploited} sub="exploit-proven" color={SEV_COLOR.critical} />
          <StatCard label="Patterns Learned" value={s.patterns_total || 0} sub="knowledge base" />
          <StatCard label="Findings" value={s.findings_total || 0} sub="total tracked" />
          <StatCard label="Avg FP Rate" value={`${Math.round((s.avg_false_positive_rate || 0) * 100)}%`} sub="lower is better" color="#047857" />
        </div>
        <div style={{ fontSize: 13, fontWeight: 600, margin: '4px 0 8px' }}>Knowledge Coverage by Attack Surface</div>
        <Card>
          {Object.keys(cov).length === 0 ? (
            <div style={{ fontSize: 11, color: 'var(--muted, #6b7280)' }}>No knowledge yet — run a scan to begin learning.</div>
          ) : (
            Object.entries(cov).map(([topic, n]) => (
              <div key={topic} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 0' }}>
                <div style={{ width: 130, fontSize: 11, color: 'var(--muted, #6b7280)' }}>{topic}</div>
                <div style={{ flex: 1, height: 6, background: 'var(--bg, #12131a)', borderRadius: 3, overflow: 'hidden' }}>
                  <div style={{ width: `${(n / covMax) * 100}%`, height: '100%', background: ACCENT, borderRadius: 3 }} />
                </div>
                <div style={{ width: 28, textAlign: 'right', fontSize: 10, color: 'var(--muted, #6b7280)' }}>{n}</div>
              </div>
            ))
          )}
        </Card>
      </>
    )
  } else if (tab === 'Findings') {
    const filters = ['', 'critical', 'high', 'exploited', 'blocked']
    const list = findings.filter((f) => !filter || f.status === filter || f.severity === filter)
    body = (
      <>
        <div style={{ display: 'flex', gap: 6, marginBottom: 10, flexWrap: 'wrap' }}>
          {filters.map((f) => (
            <button
              key={f || 'all'}
              onClick={() => setFilter(f)}
              style={{
                fontSize: 11, padding: '4px 10px', borderRadius: 9999, cursor: 'pointer', fontWeight: 500,
                border: `1px solid ${filter === f ? ACCENT : 'var(--border, #2d2f3d)'}`,
                background: filter === f ? ACCENT_SUBTLE : 'transparent',
                color: filter === f ? ACCENT : 'var(--muted, #6b7280)',
              }}
            >
              {f || 'all'}
            </button>
          ))}
        </div>
        {list.length === 0 ? (
          <Card><div style={{ fontSize: 11, color: 'var(--muted, #6b7280)' }}>No findings.</div></Card>
        ) : (
          <Card style={{ padding: 0 }}>
            {list.map((f, i) => (
              <div
                key={f.id}
                onClick={() => setSelected(f)}
                style={{ display: 'flex', alignItems: 'flex-start', gap: 10, padding: '10px 14px', cursor: 'pointer', borderBottom: i < list.length - 1 ? '1px solid var(--border, #2d2f3d)' : 'none' }}
              >
                <div style={{ width: 6, height: 6, borderRadius: '50%', marginTop: 5, flexShrink: 0, background: SEV_COLOR[f.severity] || '#6b7280' }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 12, fontWeight: 500 }}>{f.title}</div>
                  <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', display: 'flex', gap: 8, marginTop: 2, flexWrap: 'wrap' }}>
                    <span>{f.location}</span>
                    <span>{f.topic}</span>
                  </div>
                </div>
                <Pill text={f.status === 'exploited' ? 'EXPLOITED' : f.status} bg="transparent" fg={STATUS_COLOR[f.status] || '#6b7280'} />
              </div>
            ))}
          </Card>
        )}
      </>
    )
  } else if (tab === 'Knowledge') {
    body = (
      <>
        <Card style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>Ingest External Report</div>
          <textarea
            value={ingestText}
            onChange={(e) => setIngestText(e.target.value)}
            placeholder="Paste a security report — a JSON array of {topic, pattern, tags} objects, or one finding per line."
            style={{ width: '100%', minHeight: 70, fontSize: 11, padding: 8, borderRadius: 4, background: 'var(--bg, #12131a)', color: 'var(--text, #e2e8f0)', border: '1px solid var(--border, #2d2f3d)', boxSizing: 'border-box', resize: 'vertical' }}
          />
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 8 }}>
            <button
              disabled={ingesting || !ingestText.trim()}
              onClick={ingest}
              style={{ fontSize: 11, padding: '5px 14px', borderRadius: 9999, border: 'none', fontWeight: 500, background: ingesting || !ingestText.trim() ? 'var(--border, #2d2f3d)' : ACCENT, color: '#fff', cursor: ingesting || !ingestText.trim() ? 'default' : 'pointer' }}
            >
              {ingesting ? 'Ingesting…' : 'Ingest & Learn'}
            </button>
          </div>
        </Card>
        <Card style={{ padding: 0 }}>
          <div style={{ padding: '12px 14px', borderBottom: '1px solid var(--border, #2d2f3d)', fontSize: 12, fontWeight: 600 }}>Learned Patterns ({patterns.length})</div>
          {patterns.length === 0 ? (
            <div style={{ padding: '12px 14px', fontSize: 11, color: 'var(--muted, #6b7280)' }}>No patterns yet.</div>
          ) : (
            patterns.map((p, i) => (
              <div key={p.id} style={{ padding: '10px 14px', borderBottom: i < patterns.length - 1 ? '1px solid var(--border, #2d2f3d)' : 'none' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                  <div style={{ fontSize: 11, fontWeight: 600 }}>{p.topic}</div>
                  <Pill text={`${p.source || ''} · ${Math.round(p.confidence * 100)}%`} bg={ACCENT_SUBTLE} fg={ACCENT} />
                </div>
                <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', fontFamily: 'monospace' }}>{p.pattern}</div>
              </div>
            ))
          )}
        </Card>
      </>
    )
  } else if (tab === 'Exploit Lab') {
    const validated = findings.filter((f) => f.status === 'exploited' || f.status === 'blocked')
    body = validated.length === 0 ? (
      <Card><div style={{ fontSize: 11, color: 'var(--muted, #6b7280)' }}>No exploit validations yet. Confirmed findings get a sandboxed PoC run against an isolated pod.</div></Card>
    ) : (
      <Card style={{ padding: 0 }}>
        {validated.map((f, i) => (
          <div key={f.id} style={{ padding: '10px 14px', borderBottom: i < validated.length - 1 ? '1px solid var(--border, #2d2f3d)' : 'none' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
              <div style={{ fontSize: 11, fontWeight: 500 }}>{f.title}</div>
              <Pill text={f.status === 'exploited' ? 'EXPLOITED' : 'BLOCKED'} bg={f.status === 'exploited' ? '#fde2e1' : 'var(--bg, #12131a)'} fg={f.status === 'exploited' ? '#b91c1c' : '#6b7280'} />
            </div>
            {f.evidence ? <pre style={{ fontSize: 10, color: 'var(--muted, #6b7280)', fontFamily: 'monospace', background: 'var(--bg, #12131a)', padding: '6px 8px', borderRadius: 4, margin: '4px 0 0', whiteSpace: 'pre-wrap', overflowX: 'auto' }}>{f.evidence}</pre> : null}
            <div style={{ fontSize: 9, color: 'var(--muted, #6b7280)', marginTop: 4 }}>{f.location} · {f.topic}</div>
          </div>
        ))}
      </Card>
    )
  } else {
    const row = (label: string, value: string) => (
      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid var(--border, #2d2f3d)', fontSize: 12 }}>
        <span style={{ color: 'var(--muted, #6b7280)' }}>{label}</span>
        <span>{value}</span>
      </div>
    )
    body = (
      <Card>
        {row('Schedule', 'Every 6 hours (cron)')}
        {row('Active topics', 'path-traversal, auth-bypass, prompt-injection')}
        {row('Exploit sandbox', 'isolated kirocrew pod (never the live gateway)')}
        {row('Last scan', s.last_scan_at || '—')}
        <div style={{ fontSize: 10, color: 'var(--muted, #6b7280)', marginTop: 10, lineHeight: 1.5 }}>
          Exploit PoCs run only against an isolated pod under strict time/output limits. Findings are never auto-filed — you decide what to act on. Secrets are scrubbed from all evidence.
        </div>
      </Card>
    )
  }

  return (
    <div style={{ maxWidth: 1200, margin: '0 auto', padding: 16, fontFamily: 'system-ui, -apple-system, sans-serif', color: 'var(--text, #e2e8f0)' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <svg xmlns="http://www.w3.org/2000/svg" width={20} height={20} viewBox="0 0 24 24" fill="none" stroke={ACCENT} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            <path d="m9 12 2 2 4-4" />
          </svg>
          <h2 style={{ margin: 0, fontSize: 18 }}>Security Scanner</h2>
          <Pill text="Every 6h" bg={ACCENT_SUBTLE} fg={ACCENT} />
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 11, color: 'var(--muted, #6b7280)' }}>
            {status?.running ? 'scan running…' : status?.last_scan_at ? `last: ${status.last_scan_at.replace('T', ' ').replace('Z', '')}` : 'no scans yet'}
          </span>
          <button
            disabled={scanning || !!status?.running}
            onClick={scanNow}
            style={{ fontSize: 11, padding: '5px 14px', borderRadius: 9999, fontWeight: 500, border: `1px solid ${ACCENT_SUBTLE}`, background: scanning ? 'var(--border, #2d2f3d)' : 'transparent', color: scanning ? 'var(--muted, #6b7280)' : ACCENT, cursor: scanning ? 'default' : 'pointer' }}
          >
            {scanning ? '⟳ Starting…' : '⟳ Scan Now'}
          </button>
        </div>
      </div>
      {tabBar}
      {body}
    </div>
  )
}
