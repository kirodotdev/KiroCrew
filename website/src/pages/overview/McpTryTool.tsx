import { useEffect, useId, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { api } from '../../api/client'
import type { McpTryToolAnswer, McpTryToolRow } from '../../api/client/mcp'
import { Card, Btn, Input } from '../../components/ui'
import { NativeSelect, NativeSelectOption } from '../../components/ui/native-select'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'

/** One argument of a simple type; a schema with any other shape gets the raw JSON box. */
export type ToolField = { name: string; kind: string; options: string[]; required: boolean }

/**
 * The form for an input schema, or `null` when it needs the raw JSON box. A `null` schema is
 * one the server discarded (not an object, too big, or not JSON once redacted), so its
 * required arguments stay reachable only there; `{}` is a tool with no arguments.
 */
export function schemaFields(schema: Record<string, unknown> | null): ToolField[] | null {
  if (schema === null) return null
  // A composed or referenced schema keeps fields the form cannot see, so it gets the raw box.
  if ([schema.allOf, schema.anyOf, schema.oneOf, schema.not, schema.$ref].some(v => v !== undefined)) return null
  const props = (schema.properties ?? {}) as Record<string, { type?: unknown; enum?: unknown }>
  if ((schema.type ?? 'object') !== 'object' || typeof props !== 'object' || props === null) return null
  const required = Array.isArray(schema.required) ? schema.required : []
  const fields: ToolField[] = []
  for (const [name, p] of Object.entries(props)) {
    const options = Array.isArray(p?.enum) && p.enum.every(v => typeof v === 'string') ? (p.enum as string[]) : null
    const kind = options ? 'enum' : p?.type
    if (typeof kind !== 'string' || !['enum', 'string', 'number', 'integer', 'boolean'].includes(kind)) return null
    fields.push({ name, kind, options: kind === 'boolean' ? ['true', 'false'] : options ?? [], required: required.includes(name) })
  }
  return fields
}

/**
 * The arguments for filled-in values. A string or enum goes out as typed, spaces included;
 * only a number is trimmed. A field never filled in is left out (an empty enum or boolean
 * choice is "not set"), while a string emptied on purpose is sent as `""`. A bad one is named,
 * apart from the arguments, so an argument of any name is sent as it is.
 */
export type ArgumentsResult = { ok: true; args: Record<string, unknown> } | { ok: false; invalid: string }
export function argumentsFrom(fields: ToolField[], values: Record<string, string>): ArgumentsResult {
  const out: Record<string, unknown> = {}
  for (const f of fields) {
    const numeric = f.kind === 'number' || f.kind === 'integer'
    const raw = values[f.name] as string | undefined
    const v = numeric ? raw?.trim() : raw
    if (v === undefined || (f.kind !== 'string' && v === '')) {
      if (f.required) return { ok: false, invalid: f.name }
      continue
    }
    const n = Number(v)
    // An integer past the safe range would reach the tool as a different number.
    if (numeric && (!Number.isFinite(n) || (f.kind === 'integer' && !Number.isSafeInteger(n)))) return { ok: false, invalid: f.name }
    out[f.name] = f.kind === 'boolean' ? v === 'true' : numeric ? n : v
  }
  return { ok: true, args: out }
}

/**
 * Whether `text` holds, outside its quoted strings, a number literal that parsing would
 * change: a plain run of digits that is not a safe integer (the tool would get a different
 * number), or a fraction or an exponent too large to be finite (it would be sent as null).
 * A finite fraction or exponent is a number the writer chose to round, so it is kept.
 */
export function hasUnsafeNumber(text: string): boolean {
  let inString = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (inString) {
      if (c === '\\') i++
      else if (c === '"') inString = false
      continue
    }
    if (c === '"') { inString = true; continue }
    if (c !== '-' && (c < '0' || c > '9')) continue
    let end = i + 1
    while (end < text.length && '0123456789+-.eE'.includes(text[end])) end++
    const literal = text.slice(i, end)
    const digits = literal.startsWith('-') ? literal.slice(1) : literal
    const n = Number(literal)
    const plain = digits !== '' && [...digits].every(d => d >= '0' && d <= '9')
    if (plain ? !Number.isSafeInteger(n) : !Number.isFinite(n)) return true
    i = end - 1
  }
  return false
}

/** The text a tool put in its error reply (its first text part, or its message), or `''`. */
export function toolErrorText(result: string): string {
  try {
    const v = JSON.parse(result) as { content?: unknown; message?: unknown } | null
    const part = Array.isArray(v?.content) ? (v.content as { type?: unknown; text?: unknown }[]).find(c => c?.type === 'text') : undefined
    const text = part ? part.text : v?.message
    return typeof text === 'string' ? text.trim().slice(0, 500) : ''
  } catch { return '' }
}

/** The MCP Servers page's Try a tool panel, drawn only while `mcp.tool_tester` is on. */
export default function McpTryTool({ servers }: { servers: string[] }) {
  const { data: cfg, error: cfgError } = useQuery<{ mcp?: { tool_tester?: boolean } }>({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  // A failed first read decides whether the panel shows at all; nothing is typed yet.
  if (cfgError && !cfg) return <ErrorNotice className="mt-4" title={i18nT('pages.overview.mcpTryTool.config_failed')} message={cfgError.message} askAgent testId="mcp-try-tool-config-error" />
  // A failed refresh keeps the panel, and what is typed in it, with the error beside it.
  return cfg?.mcp?.tool_tester === true ? <TryToolPanel servers={servers} refreshError={cfgError?.message ?? ''} /> : null
}

type Listing = { server: string; tools: McpTryToolRow[]; leftOut: number; withheld: number }

function TryToolPanel({ servers, refreshError }: { servers: string[]; refreshError: string }) {
  const [picked, setPicked] = useState('')
  // The server list can arrive after this panel mounts, so a pick is only kept while it is still listed.
  const server = servers.includes(picked) ? picked : servers[0] ?? ''
  const [listing, setListing] = useState<Listing | null>(null)
  const [toolName, setToolName] = useState('')
  const [values, setValues] = useState<Record<string, string>>({})
  const [raw, setRaw] = useState('{}')
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [answer, setAnswer] = useState<McpTryToolAnswer | null>(null)
  const [error, setError] = useState('')
  // A failed run is titled; a failed listing has no run to say did not finish.
  const [runFailed, setRunFailed] = useState(false)
  const [hint, setHint] = useState('')
  const asked = useRef(server)
  useEffect(() => { asked.current = server }, [server])
  // Each request's number; picking a tool or a server makes any answer in flight stale.
  const latest = useRef(0)
  const tools = listing?.server === server ? listing.tools : null
  const tool = tools?.find(x => x.name === toolName)
  const fields = tool ? schemaFields(tool.inputSchema) : null
  const changesDataId = useId()

  // What a run said belongs to the server and the tool it was for: once either changes, it goes,
  // including a server list refresh that changes the effective server.
  useEffect(() => { setError(''); setRunFailed(false); setHint(''); setAnswer(null); setConfirming(false) }, [server, toolName])

  // An answer for a server or tool the user has since left is dropped, so it never lands under another.
  const send = async (body: Parameters<typeof api.mcpTryTool>[0]) => {
    const mine = ++latest.current
    setBusy(true); setError(''); setHint(''); setAnswer(null); setConfirming(false)
    let got: McpTryToolAnswer | null = null
    let failure = ''
    try { got = await api.mcpTryTool(body) } catch (e) { failure = (e as Error).message }
    setBusy(false)
    if (asked.current !== body.server || latest.current !== mine) return null
    if (failure) { setError(failure); setRunFailed(Boolean(body.tool)) }
    return got
  }
  const pick = (name: string) => { latest.current++; setToolName(name); setValues({}); setRaw('{}'); setConfirming(false); setAnswer(null) }
  const choose = (name: string) => { latest.current++; setPicked(name); setAnswer(null); setError(''); setHint(''); setConfirming(false) }
  const load = async () => {
    setListing(null)
    const got = await send({ server })
    // A switched-off tool cannot be run, so the first one that can be is the one selected; with none, nothing is.
    if (got?.tools) { setListing({ server, tools: got.tools, leftOut: got.leftOut ?? 0, withheld: got.withheld ?? 0 }); pick(got.tools.find(x => !x.switchedOff)?.name ?? '') }
  }
  const run = async (confirm: boolean) => {
    if (!tool) return
    let parsed: ArgumentsResult | null = null
    if (fields) parsed = argumentsFrom(fields, values)
    else {
      try {
        const value: unknown = hasUnsafeNumber(raw) ? null : JSON.parse(raw)
        if (value !== null && typeof value === 'object' && !Array.isArray(value)) parsed = { ok: true, args: value as Record<string, unknown> }
      } catch { parsed = null }
    }
    if (parsed === null) return setHint(i18nT('pages.overview.mcpTryTool.invalid_json'))
    if (!parsed.ok) return setHint(i18nT('pages.overview.mcpTryTool.invalid_field', { name: parsed.invalid }))
    if (tool.needsConfirm && !confirm) { setHint(''); return setConfirming(true) }
    setAnswer(await send({ server, tool: tool.name, arguments: parsed.args, confirm }))
  }

  const box = 'rounded-md border border-border bg-bg px-3 py-1.5 font-mono text-[12px] text-text focus-ring-accent'
  return (
    <Card className="mt-4" data-testid="mcp-try-tool">
      <h4 className="text-sm font-semibold text-text-strong m-0 mb-1">{i18nT('pages.overview.mcpTryTool.title')}</h4>
      <p className="text-xs text-muted m-0 mb-3">{i18nT('pages.overview.mcpTryTool.help')}</p>
      {/* No hand-off: the arguments typed in this panel would be lost to the chat it opens. */}
      {refreshError && <ErrorNotice className="mb-3" impact="partial" title={i18nT('pages.overview.mcpTryTool.config_failed')} message={refreshError} testId="mcp-try-tool-config-refresh-error" />}
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <NativeSelect value={server} onChange={e => choose(e.target.value)} aria-label={i18nT('pages.overview.mcpTryTool.server')}>
          {servers.map(s => <NativeSelectOption key={s} value={s}>{s}</NativeSelectOption>)}
        </NativeSelect>
        <Btn onClick={load} busy={busy && !tools} disabled={!server || busy}>{i18nT('pages.overview.mcpTryTool.load_tools')}</Btn>
      </div>
      {tools?.length === 0 && <p className="text-xs text-muted m-0">{i18nT('pages.overview.mcpTryTool.no_tools')}</p>}
      {listing && listing.leftOut > 0 && tools && <p className="text-xs text-muted m-0 mb-2" data-testid="mcp-try-tool-left-out">{i18nT('pages.overview.mcpTryTool.left_out', { count: listing.leftOut })}</p>}
      {listing && listing.withheld > 0 && tools && <p className="text-xs text-muted m-0 mb-2" data-testid="mcp-try-tool-withheld">{i18nT('pages.overview.mcpTryTool.withheld', { count: listing.withheld })}</p>}
      {tools && tools.length > 0 && (
        <div className="flex flex-col gap-2">
          <NativeSelect value={toolName} onChange={e => { if (!busy) pick(e.target.value) }} busy={busy} aria-busy={busy} aria-disabled={busy} aria-label={i18nT('pages.overview.mcpTryTool.tool')}>
            {tools.map(x => <NativeSelectOption key={x.name} value={x.name} disabled={x.switchedOff}>{x.name}</NativeSelectOption>)}
          </NativeSelect>
          {tool?.description && <p className="text-xs text-muted m-0 whitespace-pre-wrap">{tool.description}</p>}
          {fields ? fields.map(f => (
            <label key={f.name} className="flex items-center gap-2 text-[12px]">
              <span className="w-40 font-mono truncate" title={f.name}>{f.name}{f.required ? ' *' : ''}</span>
              {f.options.length > 0 ? (
                <NativeSelect value={values[f.name] ?? ''} onChange={e => setValues({ ...values, [f.name]: e.target.value })}>
                  <NativeSelectOption value="">{i18nT('pages.overview.mcpTryTool.not_set')}</NativeSelectOption>
                  {f.options.map(o => <NativeSelectOption key={o} value={o}>{o}</NativeSelectOption>)}
                </NativeSelect>
              ) : f.kind === 'string' ? (
                // A string can hold line breaks, which a one-line input would drop; the box
                // grows with them, up to six lines, then scrolls.
                <textarea value={values[f.name] ?? ''} onChange={e => setValues({ ...values, [f.name]: e.target.value })} rows={Math.min(6, (values[f.name] ?? '').split('\n').length)} spellCheck={false} aria-label={f.name} className={`flex-1 min-w-0 resize-y ${box}`} />
              ) : <Input value={values[f.name] ?? ''} onChange={e => setValues({ ...values, [f.name]: e.target.value })} />}
            </label>
          )) : <textarea value={raw} onChange={e => setRaw(e.target.value)} spellCheck={false} aria-label={i18nT('pages.overview.mcpTryTool.raw_arguments')} className={`w-full min-h-[96px] resize-y ${box}`} />}
          {hint && <p className="text-xs text-warn m-0" role="status" data-testid="mcp-try-tool-hint">{hint}</p>}
          {confirming && tool ? (
            <div className="rounded-md border border-warn/40 bg-bg-elevated px-3 py-2 text-[12px]" role="alert">
              <AlertTriangle className="lucide-inline text-warn" /> {i18nT('pages.overview.mcpTryTool.confirm_text', { tool: tool.name, server })}
              <div className="flex gap-2 mt-2">
                <Btn danger onClick={() => run(true)} disabled={busy}>{i18nT('pages.overview.mcpTryTool.confirm_run', { tool: tool.name })}</Btn>
                <Btn onClick={() => setConfirming(false)}>{i18nT('pages.overview.mcpTryTool.cancel')}</Btn>
              </div>
            </div>
          ) : (
            // A tool that can change data says so before the press, not only in the confirm after it.
            <div className="flex flex-wrap items-center gap-2">
              <Btn primary onClick={() => run(false)} busy={busy} disabled={!tool} aria-describedby={tool?.needsConfirm ? changesDataId : undefined}>
                {tool?.needsConfirm ? i18nT('pages.overview.mcpTryTool.run_needs_confirm') : i18nT('pages.overview.mcpTryTool.run')}
              </Btn>
              {tool?.needsConfirm && <span id={changesDataId} className="text-xs text-muted" data-testid="mcp-try-tool-changes-data"><AlertTriangle className="lucide-inline text-warn" /> {i18nT('pages.overview.mcpTryTool.changes_data')}</span>}
            </div>
          )}
        </div>
      )}
      {/* No hand-off: the arguments are typed here for one run, and an agent in the
          chat has no copy of them to retry with. */}
      {error && <ErrorNotice className="mt-3" title={runFailed ? i18nT('pages.overview.mcpTryTool.run_failed') : undefined} message={error} testId="mcp-try-tool-error" />}
      {answer?.result !== undefined && (
        <div className="mt-3" data-testid="mcp-try-tool-result">
          {/* No hand-off: the tool's reply is shown in full below, and the arguments
              that produced it are not kept for an agent chat to retry with. */}
          {answer.isError && (toolErrorText(answer.result)
            ? <ErrorNotice className="mb-2" title={i18nT('pages.overview.mcpTryTool.tool_error')} message={toolErrorText(answer.result)} testId="mcp-try-tool-tool-error" />
            : <ErrorNotice className="mb-2" message={i18nT('pages.overview.mcpTryTool.tool_error')} testId="mcp-try-tool-tool-error" />)}
          <pre className={`max-h-[360px] overflow-auto whitespace-pre-wrap break-words m-0 ${box}`}>{answer.result}</pre>
          {answer.truncated && <p className="text-xs text-muted m-0 mt-1">{i18nT('pages.overview.mcpTryTool.truncated')}</p>}
        </div>
      )}
    </Card>
  )
}
