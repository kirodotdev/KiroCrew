/**
 * McpCustomServerModal — add custom MCP servers and edit installed specs
 * as raw JSON.
 *
 * JSON-first by design: the mcp.json entry IS the interface people copy
 * from READMEs, so the modal is a validating editor for it rather than a
 * bespoke form. Add mode accepts a full {"mcpServers": {...}} block, a
 * {name: spec} map, or a single bare spec (with the name given in the
 * Name field). Edit mode loads the server's FULL spec (including env)
 * from GET /api/mcp/custom/{name} and saves via PUT — the enabled state
 * is never changed by an edit.
 *
 * Consent: added servers land disabled unless "Enable immediately" is
 * ticked — the tick is the consent act (same stance as registry installs).
 */
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, Loader2 } from 'lucide-react'
import { api, ApiError } from '../api/client'
import Modal from './Modal'
import { Btn } from './ui'
import type { McpCustomSpec } from '../types'

interface Props {
  open: boolean
  onClose: () => void
  /** When set, the modal edits this installed server instead of adding. */
  editName?: string | null
}

/** Result of client-side parsing of the pasted JSON. */
type ParseOutcome =
  | { ok: true; servers: Record<string, McpCustomSpec>; needsName: false }
  | { ok: true; servers: Record<string, McpCustomSpec>; needsName: true }
  | { ok: false; error: string }

/** True when the object looks like ONE server spec rather than a name map. */
function looksLikeSpec(obj: Record<string, unknown>): boolean {
  return typeof obj.command === 'string' || typeof obj.url === 'string'
}

/**
 * Normalize pasted JSON into a {name: spec} map. Accepted shapes:
 * {"mcpServers": {...}} (README convention), a bare {name: spec} map, or
 * a single spec object — which needs the Name field (needsName: true).
 */
export function parseCustomJson(text: string, nameForBareSpec: string): ParseOutcome {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (e) {
    return { ok: false, error: `Not valid JSON: ${e instanceof Error ? e.message : String(e)}` }
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, error: 'Expected a JSON object' }
  }
  const obj = parsed as Record<string, unknown>

  const block = obj.mcpServers
  if (block !== undefined) {
    if (block === null || typeof block !== 'object' || Array.isArray(block)) {
      return { ok: false, error: '"mcpServers" must be an object' }
    }
    const servers = block as Record<string, McpCustomSpec>
    if (Object.keys(servers).length === 0) return { ok: false, error: '"mcpServers" is empty' }
    return { ok: true, servers, needsName: false }
  }

  if (looksLikeSpec(obj)) {
    const name = nameForBareSpec.trim()
    if (!name) return { ok: true, servers: {}, needsName: true }
    return { ok: true, servers: { [name]: obj as McpCustomSpec }, needsName: true }
  }

  if (Object.keys(obj).length === 0) return { ok: false, error: 'Expected a JSON object' }
  return { ok: true, servers: obj as Record<string, McpCustomSpec>, needsName: false }
}

/** One-line human summary of a spec for the preview list. */
function specSummary(spec: McpCustomSpec): string {
  if (typeof spec?.url === 'string') return spec.url
  const parts = [spec?.command, ...(Array.isArray(spec?.args) ? spec.args : [])]
  return parts.filter(Boolean).join(' ') || '(invalid spec)'
}

const PLACEHOLDER = `{
  "mcpServers": {
    "my-server": {
      "command": "npx",
      "args": ["-y", "@example/mcp-server"],
      "env": { "API_KEY": "" }
    }
  }
}`

export default function McpCustomServerModal({ open, onClose, editName }: Props) {
  const queryClient = useQueryClient()
  const editing = !!editName
  const [text, setText] = useState('')
  const [bareName, setBareName] = useState('')
  const [enableNow, setEnableNow] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  // Reset per open so a reopened modal never carries stale state.
  useEffect(() => {
    if (open) {
      setText('')
      setBareName('')
      setEnableNow(false)
      setSubmitError(null)
    }
  }, [open, editName])

  // Edit mode: prefill from the FULL spec (the list endpoint omits env —
  // prefilling from it would silently drop the user's env vars on save).
  const specQuery = useQuery({
    queryKey: ['mcp-custom-spec', editName],
    queryFn: () => api.mcpCustomGet(editName!),
    enabled: open && editing,
    staleTime: 0,
    gcTime: 0,
  })
  useEffect(() => {
    if (open && editing && specQuery.data) {
      setText(JSON.stringify(specQuery.data.spec, null, 2))
    }
  }, [open, editing, specQuery.data])

  const parsed = useMemo<ParseOutcome | null>(() => {
    if (!text.trim()) return null
    if (editing) {
      // Edit mode expects exactly one bare spec.
      try {
        const spec = JSON.parse(text)
        if (spec === null || typeof spec !== 'object' || Array.isArray(spec)) {
          return { ok: false, error: 'Expected a JSON object' }
        }
        return { ok: true, servers: { [editName!]: spec as McpCustomSpec }, needsName: false }
      } catch (e) {
        return { ok: false, error: `Not valid JSON: ${e instanceof Error ? e.message : String(e)}` }
      }
    }
    return parseCustomJson(text, bareName)
  }, [text, bareName, editing, editName])

  const previewNames = parsed?.ok ? Object.keys(parsed.servers) : []
  const waitingForName = !!parsed?.ok && parsed.needsName && previewNames.length === 0
  const canSubmit = !!parsed?.ok && previewNames.length > 0

  const addMutation = useMutation({
    mutationFn: () => api.mcpCustomAdd(parsed!.ok ? parsed!.servers : {}, enableNow),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      onClose()
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setSubmitError('Name already in use — pick a different server name or edit the existing one.')
      } else {
        setSubmitError(err instanceof Error ? err.message : String(err))
      }
    },
  })

  const updateMutation = useMutation({
    mutationFn: () => api.mcpCustomUpdate(editName!, parsed!.ok ? parsed!.servers[editName!] : {}),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      onClose()
    },
    onError: (err) => setSubmitError(err instanceof Error ? err.message : String(err)),
  })

  const busy = addMutation.isPending || updateMutation.isPending
  const submit = () => {
    setSubmitError(null)
    if (!canSubmit || busy) return
    if (editing) updateMutation.mutate()
    else addMutation.mutate()
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={editing ? `Edit JSON — ${editName}` : 'Add Custom Server'}
      maxWidth={640}
    >
      <div className="flex flex-col gap-3 p-1">
        {!editing && (
          <p className="text-xs text-muted m-0">
            Paste an <code>mcpServers</code> block from a README, a{' '}
            <code>{'{name: spec}'}</code> map, or a single spec. Specs take{' '}
            <code>command</code>/<code>args</code>/<code>env</code> (stdio) or{' '}
            <code>url</code> (remote).
          </p>
        )}
        {editing && specQuery.isLoading && (
          <span className="flex items-center gap-1.5 text-xs text-muted" role="status">
            <Loader2 size={13} className="animate-spin" aria-hidden="true" /> Loading current spec…
          </span>
        )}
        {editing && specQuery.isError && (
          <span className="flex items-center gap-1 text-xs text-amber-400" role="alert">
            <AlertTriangle size={13} aria-hidden="true" />
            {specQuery.error instanceof Error ? specQuery.error.message : 'Failed to load spec'}
          </span>
        )}

        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={editing ? '' : PLACEHOLDER}
          spellCheck={false}
          aria-label={editing ? 'Server spec JSON' : 'Servers JSON'}
          className="w-full min-h-[220px] rounded-md border border-border bg-bg px-3 py-2 font-mono text-[12px] text-text focus:outline-none focus:ring-1 focus:ring-accent resize-y"
        />

        {parsed && !parsed.ok && (
          <span className="flex items-center gap-1 text-xs text-amber-400" role="alert">
            <AlertTriangle size={13} aria-hidden="true" /> {parsed.error}
          </span>
        )}

        {waitingForName && (
          <p className="text-xs text-muted m-0">That looks like a single server spec — give it a name:</p>
        )}
        {!editing && parsed?.ok && parsed.needsName && (
          <input
            value={bareName}
            onChange={(e) => setBareName(e.target.value)}
            placeholder="server-name"
            aria-label="Server name"
            className="w-60 rounded-md border border-border bg-bg px-3 py-1.5 font-mono text-[12px] text-text focus:outline-none focus:ring-1 focus:ring-accent"
          />
        )}

        {!editing && canSubmit && (
          <div className="rounded-md border border-border bg-bg-elevated px-3 py-2">
            <div className="text-[11px] uppercase tracking-wide text-muted mb-1">Will add</div>
            <ul className="m-0 p-0 list-none space-y-0.5">
              {previewNames.map((n) => (
                <li key={n} className="text-[12px] flex items-baseline gap-2 min-w-0">
                  <code className="font-semibold shrink-0">{n}</code>
                  <span className="text-muted font-mono truncate">{specSummary(parsed!.ok ? parsed!.servers[n] : {})}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {submitError && (
          <span className="flex items-center gap-1 text-xs text-amber-400" role="alert">
            <AlertTriangle size={13} aria-hidden="true" /> {submitError}
          </span>
        )}

        <div className="flex items-center justify-between gap-3 pt-1">
          {!editing ? (
            <label className="flex items-center gap-2 text-xs text-text cursor-pointer select-none">
              <input
                type="checkbox"
                checked={enableNow}
                onChange={(e) => setEnableNow(e.target.checked)}
                className="accent-[var(--accent)]"
              />
              Enable immediately
              <span className="text-muted">(otherwise added disabled — enable in the table)</span>
            </label>
          ) : (
            <span className="text-xs text-muted">Saving keeps the server's enabled/disabled state.</span>
          )}
          <div className="flex items-center gap-2 shrink-0">
            <Btn onClick={onClose}>Cancel</Btn>
            <Btn primary onClick={submit} disabled={!canSubmit || busy}>
              {busy ? (
                <Loader2 size={14} className="animate-spin" aria-hidden="true" />
              ) : (
                <Check size={14} aria-hidden="true" />
              )}
              {editing ? 'Save' : `Add${previewNames.length > 1 ? ` ${previewNames.length} servers` : ''}`}
            </Btn>
          </div>
        </div>
      </div>
    </Modal>
  )
}
