import { useState } from 'react'
import { fmtDateFields } from '../../i18n/format'

export function typeBadgeVariant(t: string): 'ok' | 'warn' | 'err' | 'aim' {
  if (['design_doc', 'code_doc'].includes(t)) return 'aim'
  if (['runbook', 'policy'].includes(t)) return 'warn'
  if (['report', 'presentation'].includes(t)) return 'ok'
  return 'ok'
}

export function formatDate(iso: string) {
  return fmtDateFields(iso, { month: 'short', day: 'numeric', year: 'numeric' })
}

export function formatRelativeDate(iso: string): string {
  const diff = Math.max(0, Date.now() - new Date(iso).getTime())
  const days = Math.floor(diff / (1000 * 60 * 60 * 24))
  if (days === 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 7) return `${days}d ago`
  if (days < 30) return `${Math.floor(days / 7)}w ago`
  return `${Math.floor(days / 30)}mo ago`
}

export function copyText(text: string) {
  navigator.clipboard.writeText(text)
}

export function useCopy() {
  const [copied, setCopied] = useState(false)
  const copy = (text: string) => {
    navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return { copied, copy }
}

export const ITEM_TYPES = ['design_doc', 'runbook', 'meeting_notes', 'code_doc', 'presentation', 'report', 'policy', 'personal_notes', 'external_reference', 'document']
export const STATUSES = ['active', 'archived']
// Status the list view opens on. This is the default view, NOT user narrowing,
// so the onboarding empty state treats this value as "no filter applied".
export const DEFAULT_STATUS_FILTER = 'active'
export const SUPPORTED_FORMATS = 'Markdown, Plain text, Code files (.py, .ts, .java, .go, .rs, etc.), HTML, JSON, YAML, CSV, DOCX'

export const ONBOARDING = {
  title: 'Welcome to the Knowledge Library',
  description: 'Your centralized knowledge base with entity extraction, graph relationships, and full-text search.',
  steps: [
    'Drop files here or click Upload to ingest documents',
    'Documents are chunked, entities extracted, and relationships mapped automatically',
    'Search across all knowledge, filter by type, or explore the entity graph',
    `Supported formats: ${SUPPORTED_FORMATS}`,
  ],
}
