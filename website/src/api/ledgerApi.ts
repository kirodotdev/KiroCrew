/**
 * Ledger API client — typed wrappers for the shared-scratchpad backend.
 */

const _sk = { 'X-Session-Key': 'dashboard:ui' }
const headers = (extra?: Record<string, string>) => ({ 'Content-Type': 'application/json', ..._sk, ...extra })

class LedgerApiError extends Error {
  readonly status: number
  readonly body: unknown
  constructor(status: number, body: unknown) {
    super(`Ledger API ${status}`)
    this.name = 'LedgerApiError'
    this.status = status
    this.body = body
  }
}

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const body = await r.json().catch(() => r.text())
    throw new LedgerApiError(r.status, body)
  }
  return r.json()
}

// ── Types ──

export interface LedgerSummary {
  id: string
  title: string
  version: number
  created_at: string
  updated_at: string
  pinned_by: string[]
  progress: { done: number; total: number }
}

export interface Ledger {
  id: string
  title: string
  version: number
  content: string
  created_at: string
  updated_at: string
  pinned_by: string[]
}

export interface LedgerUpdateResult {
  id: string
  title: string
  version: number
  updated_at: string
}

export interface LedgerToggleResult {
  version: number
  content: string
}

export interface LedgerConflict {
  error: 'version_conflict'
  current: { content: string; version: number }
}

// ── API Functions ──

export function listLedgers(): Promise<LedgerSummary[]> {
  return fetch('/api/ledgers', { headers: _sk }).then(r => j<LedgerSummary[]>(r))
}

export function createLedger(title?: string): Promise<Ledger> {
  return fetch('/api/ledgers', { method: 'POST', headers: headers(), body: JSON.stringify(title ? { title } : {}) }).then(r => j<Ledger>(r))
}

export function getLedger(id: string): Promise<Ledger> {
  return fetch(`/api/ledgers/${encodeURIComponent(id)}`, { headers: _sk }).then(r => j<Ledger>(r))
}

export function updateLedger(id: string, body: { content?: string; base_version?: number; title?: string }): Promise<LedgerUpdateResult> {
  return fetch(`/api/ledgers/${encodeURIComponent(id)}`, { method: 'PUT', headers: headers(), body: JSON.stringify(body) }).then(async r => {
    if (r.status === 409) {
      const conflict = await r.json() as LedgerConflict
      throw new LedgerApiError(409, conflict)
    }
    return j<LedgerUpdateResult>(r)
  })
}

export function deleteLedger(id: string): Promise<{ ok: true }> {
  return fetch(`/api/ledgers/${encodeURIComponent(id)}`, { method: 'DELETE', headers: _sk }).then(r => j<{ ok: true }>(r))
}

export function toggleLedgerLine(id: string, line: number, expected: string): Promise<LedgerToggleResult> {
  return fetch(`/api/ledgers/${encodeURIComponent(id)}/toggle`, { method: 'POST', headers: headers(), body: JSON.stringify({ line, expected }) }).then(async r => {
    if (r.status === 409) {
      const conflict = await r.json()
      throw new LedgerApiError(409, conflict)
    }
    return j<LedgerToggleResult>(r)
  })
}

export function pinLedgerToSlot(slot: string, ledgerId: string): Promise<{ ok: boolean; ledger_id: string }> {
  return fetch(`/api/chat/slots/${encodeURIComponent(slot)}/ledger`, { method: 'PATCH', headers: headers(), body: JSON.stringify({ ledger_id: ledgerId }) }).then(r => j<{ ok: boolean; ledger_id: string }>(r))
}

export { LedgerApiError }
