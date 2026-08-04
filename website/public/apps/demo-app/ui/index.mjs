// Demo KiroCrew App — proves the federated loading pipeline works end-to-end.
// This file is loaded dynamically by AppHost via import('/apps/demo-app/ui/index.mjs').
// It uses the import map to resolve 'react' and '@kirocrew/app-sdk' from the host.

const React = window.__kirocrew_modules.react
const { useAppApi, useAppEvents } = window.__kirocrew_modules['@kirocrew/app-sdk']
const { Sparkles, Bot, Zap, RefreshCw } = window.__kirocrew_modules['lucide-react']

const { useState, useEffect, createElement: h } = React

function DemoApp() {
  const api = useAppApi()
  const [agents, setAgents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const fetchAgents = () => {
    setLoading(true)
    api.get('/api/agents')
      .then(data => { setAgents(Array.isArray(data) ? data : []); setError(null) })
      .catch(err => setError(err.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => { fetchAgents() }, [])

  // Live updates
  useAppEvents('agent:status', fetchAgents)

  const activeCount = agents.filter(a => a.running || a.status === 'running').length

  return h('div', { className: 'px-6 pt-4 pb-8' },
    // Header
    h('div', { className: 'flex items-end justify-between gap-4 pb-3' },
      h('div', null,
        h('div', { className: 'text-2xl font-bold tracking-tight text-text-strong flex items-center gap-2' },
          h(Sparkles, { size: 22 }),
          'Demo App'
        ),
        h('div', { className: 'text-muted text-sm mt-1' },
          'Federated app loaded via dynamic ESM import'
        ),
      ),
      h('button', {
        className: 'px-2.5 py-1 rounded-md border border-border bg-transparent text-muted hover:text-text hover:border-border-strong text-[13px] cursor-pointer transition-all inline-flex items-center gap-1.5',
        onClick: fetchAgents,
      }, h(RefreshCw, { size: 14 }), 'Refresh'),
    ),

    // Stats
    h('div', { className: 'grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(130px,1fr))] mb-6' },
      h(StatCard, { label: 'Total Agents', value: agents.length, accent: true }),
      h(StatCard, { label: 'Active', value: activeCount }),
      h(StatCard, { label: 'SDK Version', value: '1.0' }),
    ),

    // Content
    h('div', { className: 'border border-border bg-card rounded-lg p-5 shadow-sm' },
      h('h3', { className: 'text-sm font-semibold tracking-tight text-text-strong mb-3.5 flex items-center gap-2' },
        h(Bot, { size: 16 }),
        'Loaded Agents'
      ),
      loading
        ? h('div', { className: 'text-muted text-sm py-4' }, 'Loading…')
        : error
          ? h('div', { className: 'text-danger text-sm py-4' }, 'Error: ', error)
          : agents.length === 0
            ? h('div', { className: 'text-muted text-sm py-4' }, 'No agents found')
            : h('div', { className: 'space-y-2' },
                ...agents.slice(0, 10).map((a, i) =>
                  h('div', { key: i, className: 'flex items-center gap-3 py-2 px-3 rounded-md bg-bg-elevated border border-border' },
                    h(Zap, { size: 14, className: a.running ? 'text-ok' : 'text-muted' }),
                    h('span', { className: 'text-sm text-text font-medium' }, a.name || a.label || `Agent ${i + 1}`),
                    h('span', { className: `text-[12px] px-2 py-[2px] rounded-full font-medium ${a.running ? 'bg-ok-subtle text-ok' : 'bg-bg-elevated text-muted'}` },
                      a.running ? 'Active' : 'Idle'
                    ),
                  )
                )
              ),
    ),

    // Footer
    h('div', { className: 'mt-6 text-[12px] text-muted/60 text-center' },
      'This app was loaded dynamically from /apps/demo-app/ui/index.mjs via the Kiro Crew App Platform.'
    ),
  )
}

function StatCard({ label, value, accent }) {
  return h('div', { className: 'bg-card rounded-md px-4 py-3.5 border border-border shadow-[inset_0_1px_0_var(--card-hl)]' },
    h('div', { className: 'text-muted text-[13px] font-medium uppercase tracking-[.04em]' }, label),
    h('div', { className: `text-2xl font-bold mt-1.5 tracking-tight leading-none ${accent ? 'text-accent' : ''}` }, String(value ?? '—')),
  )
}

// Default export — this is what AppHost renders
export default DemoApp
