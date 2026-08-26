/**
 * HistoryTab — past advisor sessions with feedback and search.
 *
 * Shows a timeline of conversations: problem → advice → products → feedback.
 * Search uses the RAG endpoint to find relevant past sessions.
 * Users can update feedback (liked/purchased/skipped) on products.
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clock, Lightbulb, Search, ShoppingCart, SkipForward, ThumbsUp } from 'lucide-react'
import * as shopApi from './api'
import { EmptyState, Input } from '../../components/ui'

import { i18nT } from '../../i18n/t'
import { fmtCurrency } from '../../i18n/format'
// ── Types ──

interface HistorySession {
  id: string
  date: string
  problem: string
  advice: string
  products: { name: string; price?: number }[]
  feedback: Record<string, string>
  created_at: string
}

// ── API ──

async function fetchHistory(limit = 20): Promise<{ sessions: HistorySession[] }> {
  return shopApi.get(`/history?limit=${limit}`)
}

async function updateFeedback(historyId: string, product: string, feedback: string): Promise<void> {
  await shopApi.put(`/history/${historyId}/feedback`, { product, feedback })
}

// ── Component ──

export function HistoryTab() {
  const queryClient = useQueryClient()
  const [searchQuery, setSearchQuery] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['personal-shopper', 'history'],
    queryFn: () => fetchHistory(),
  })

  const feedbackMutation = useMutation({
    mutationFn: ({ historyId, product, feedback }: { historyId: string; product: string; feedback: string }) =>
      updateFeedback(historyId, product, feedback),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['personal-shopper', 'history'] }),
  })

  const sessions: HistorySession[] = data?.sessions ?? []

  // Client-side search filter (simple substring; RAG search is via the preferences endpoint)
  const filtered = searchQuery.trim()
    ? sessions.filter(
        (s) =>
          s.problem.toLowerCase().includes(searchQuery.toLowerCase()) ||
          s.advice.toLowerCase().includes(searchQuery.toLowerCase()) ||
          s.products.some((p) => p.name.toLowerCase().includes(searchQuery.toLowerCase()))
      )
    : sessions

  if (isLoading) {
    return <div className="text-sm text-[var(--muted)] py-8 text-center">{i18nT('apps.personalShopper.historyTab.loading_history')}</div>
  }

  return (
    <div className="space-y-3">
      {/* Search */}
      <div className="relative">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--muted)]" />
        <Input
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder={i18nT('apps.personalShopper.historyTab.search_past_sessions')}
          className="pl-8"
        />
      </div>

      {/* Empty state */}
      {sessions.length === 0 && (
        <EmptyState
          icon={<Clock size={28} />}
          title={i18nT('apps.personalShopper.historyTab.no_history_yet')}
          subtitle={i18nT('apps.personalShopper.historyTab.your_past_advisor_conversations_and_recommendati')}
        />
      )}

      {/* No search results */}
      {sessions.length > 0 && filtered.length === 0 && (
        <p className="text-sm text-[var(--muted)] py-4 text-center">
          {i18nT('apps.personalShopper.historyTab.no_sessions_matching', { query: searchQuery })}
        </p>
      )}

      {/* Session cards */}
      {filtered.map((session) => (
        <SessionCard
          key={session.id}
          session={session}
          onFeedback={(product, feedback) =>
            feedbackMutation.mutate({ historyId: session.id, product, feedback })
          }
        />
      ))}
    </div>
  )
}

// ── Session Card ──

function SessionCard({
  session,
  onFeedback,
}: {
  session: HistorySession
  onFeedback: (product: string, feedback: string) => void
}) {
  return (
    <div className="p-4 rounded-lg bg-[var(--card)] border border-[var(--border)]">
      {/* Header */}
      <div className="flex justify-between items-start mb-2">
        <h3 className="text-sm font-medium text-[var(--text)]">{session.problem}</h3>
        <span className="text-[10px] text-[var(--muted)] whitespace-nowrap ml-2">{session.date}</span>
      </div>

      {/* Advice */}
      {session.advice && (
        <p className="text-xs text-[var(--muted)] mb-3 pl-2 border-l-2 border-[var(--border)] leading-relaxed">
          {session.advice}
        </p>
      )}

      {/* Products */}
      {session.products.length > 0 && (
        <div className="space-y-1.5 mt-2">
          {session.products.map((product, i) => (
            <ProductRow
              key={i}
              name={product.name}
              price={product.price}
              feedback={session.feedback[product.name]}
              onFeedback={(fb) => onFeedback(product.name, fb)}
            />
          ))}
        </div>
      )}

      {/* No products — advice was the solution */}
      {session.products.length === 0 && session.advice && (
        <div className="flex items-center justify-center gap-1.5 text-[11px] text-[var(--muted)] mt-2 px-2 py-2 rounded bg-[var(--bg-elevated)]">
          <Lightbulb size={12} />
          {i18nT('apps.personalShopper.historyTab.no_products_recommended_advice_was_the_solution')}
        </div>
      )}
    </div>
  )
}

// ── Product Row ──

function ProductRow({
  name,
  price,
  feedback,
  onFeedback,
}: {
  name: string
  price?: number
  feedback?: string
  onFeedback: (fb: string) => void
}) {
  // Feedback was previously a one-way door: once set, the badge replaced the
  // buttons and no route existed to change it, so a single misclick permanently
  // recorded "purchased". Clicking the badge now reopens the choices.
  const [editing, setEditing] = useState(false)
  const showChoices = !feedback || editing

  return (
    <div className="flex items-center gap-2 text-xs px-3 py-2 rounded-lg bg-[var(--bg-elevated)]">
      <span className="flex-1 text-[var(--text)] font-medium">{name}</span>
      {price != null && (
        <span className="text-[var(--accent)] font-semibold">
          {fmtCurrency(price)}
        </span>
      )}

      {/* Feedback buttons or badge */}
      {!showChoices ? (
        <FeedbackBadge feedback={feedback as string} onEdit={() => setEditing(true)} />
      ) : (
        <div className="flex gap-1">
          <button
            onClick={() => { onFeedback('liked'); setEditing(false) }}
            className="p-1 rounded hover:bg-[var(--ok-subtle)] text-[var(--muted)] hover:text-[var(--ok)] transition-colors"
            title={i18nT('apps.personalShopper.historyTab.liked')}
            aria-label={i18nT('apps.personalShopper.historyTab.mark_liked')}
          >
            <ThumbsUp size={12} />
          </button>
          <button
            onClick={() => { onFeedback('purchased'); setEditing(false) }}
            className="p-1 rounded hover:bg-[var(--accent-subtle)] text-[var(--muted)] hover:text-[var(--accent)] transition-colors"
            title={i18nT('apps.personalShopper.historyTab.purchased')}
            aria-label={i18nT('apps.personalShopper.historyTab.mark_purchased')}
          >
            <ShoppingCart size={12} />
          </button>
          <button
            onClick={() => { onFeedback('skipped'); setEditing(false) }}
            className="p-1 rounded hover:bg-[var(--bg-hover)] text-[var(--muted)] transition-colors"
            title={i18nT('apps.personalShopper.historyTab.skipped')}
            aria-label={i18nT('apps.personalShopper.historyTab.mark_skipped')}
          >
            <SkipForward size={12} />
          </button>
        </div>
      )}
    </div>
  )
}

// ── Feedback Badge ──

function FeedbackBadge({ feedback, onEdit }: { feedback: string; onEdit: () => void }) {
  // Labels come from the catalog, not literals: an object property is neither a
  // JSX attribute nor a text child, so the extraction codemod and the i18n lint
  // both walk past it -- which is how these shipped as lowercase English visible
  // in all 12 locales.
  const config: Record<
    string,
    { labelKey: string; icon: typeof ThumbsUp; className: string }
  > = {
    purchased: {
      labelKey: 'apps.personalShopper.historyTab.purchased',
      icon: ShoppingCart,
      className: 'bg-[var(--accent-subtle)] text-[var(--accent)]',
    },
    liked: {
      labelKey: 'apps.personalShopper.historyTab.liked',
      icon: ThumbsUp,
      className: 'bg-[var(--ok-subtle)] text-[var(--ok)]',
    },
    skipped: {
      labelKey: 'apps.personalShopper.historyTab.skipped',
      icon: SkipForward,
      className: 'bg-[var(--bg-elevated)] text-[var(--muted)]',
    },
  }

  const c = config[feedback] ?? config.skipped
  const Icon = c.icon
  const label = i18nT(c.labelKey)

  return (
    <button
      type="button"
      onClick={onEdit}
      title={i18nT('apps.personalShopper.historyTab.change_feedback')}
      aria-label={i18nT('apps.personalShopper.historyTab.change_feedback')}
      className={`inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full font-medium transition-opacity hover:opacity-80 ${c.className}`}
    >
      <Icon size={10} />
      {label}
    </button>
  )
}
