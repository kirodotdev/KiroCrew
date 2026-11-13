/**
 * Oncall Watchtower — full-featured example app.
 *
 * Demonstrates: useAppApi, useAppEvents, useNavBadge, IssueList, PageHeader,
 * StatCard, Card, Badge, and real-time event handling.
 */
import { useAppApi, useAppEvents, useNavBadge } from '@kirocrew/app-sdk'
import * as sdk from '@kirocrew/app-sdk'
import { Card, CardTitle, PageHeader, StatCard, Badge, EmptyState } from '@kirocrew/app-sdk/ui'
import { useState, useEffect } from 'react'

interface Ticket {
  id: string
  title: string
  severity: string
  status: string
  assignee: string
}

export default function OncallWatchtower() {
  const api = useAppApi()
  const setBadge = useNavBadge()
  const [tickets, setTickets] = useState<Ticket[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.get<Ticket[]>('/api/tickets')
      .then((data) => setTickets(data))
      .catch((err) => console.error('Failed to load tickets', err))
      .finally(() => setLoading(false))
  }, [])

  // Update badge when ticket count changes
  useEffect(() => {
    const urgent = tickets.filter(t => t.severity === 'sev-1' || t.severity === 'sev-2')
    setBadge(urgent.length)
  }, [tickets, setBadge])

  // Listen for real-time notifications — update state, not just log
  useAppEvents('notification', (data: any) => {
    if (data?.type === 'new-ticket' && data.ticket) {
      setTickets(prev => [data.ticket, ...prev])
    }
  })

  const urgentCount = tickets.filter(t => t.severity === 'sev-1').length
  const activeCount = tickets.filter(t => t.status === 'open').length

  return (
    <>
      <PageHeader title="Oncall Watchtower" subtitle="Your on-call dashboard" />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          <StatCard label="Open Tickets" value={activeCount} accent />
          <StatCard label="Urgent (Sev-1)" value={urgentCount} />
          <StatCard label="Total" value={tickets.length} />
        </div>
        <Card>
          <CardTitle>Recent Tickets</CardTitle>
          {tickets.length === 0 ? (
            <EmptyState icon="📋" title="No tickets" />
          ) : (
            <table className="w-full border-collapse table-striped">
              <thead>
                <tr>
                  <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">ID</th>
                  <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">Title</th>
                  <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">Severity</th>
                  <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {tickets.map(t => (
                  <tr key={t.id}>
                    <td className="px-2.5 py-2 text-sm">{t.id}</td>
                    <td className="px-2.5 py-2 text-sm">{t.title}</td>
                    <td className="px-2.5 py-2">
                      <Badge variant={t.severity === 'sev-1' ? 'err' : t.severity === 'sev-2' ? 'warn' : 'ok'}>
                        {t.severity}
                      </Badge>
                    </td>
                    <td className="px-2.5 py-2 text-sm">{t.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        {/* Needs `/api/source/issues` in permissions.api; a host below level 6
            has no IssueList, so the namespace import is feature-detected. */}
        {sdk.APP_SDK_LEVEL >= 6 && sdk.IssueList && (
          <Card>
            <CardTitle>My oncall issues</CardTitle>
            <sdk.IssueList
              query={{ provider: 'github', scope: 'acme/oncall', assignee: 'me', labels: ['oncall'] }}
              view="condensed"
              showFilters
              emptyText="Nothing assigned to you"
            />
          </Card>
        )}
      </div>
    </>
  )
}
