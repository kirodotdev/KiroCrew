import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, Plus, ShieldCheck, X } from 'lucide-react'
import { api, ApiError } from '../api/client'
import { Btn, Input } from './ui'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './ui/dialog'
import SimpleSelect from './SimpleSelect'
import InfoTip from './InfoTip'
import type { CatalogSkill } from './AgentSkillsEditor'

import { i18nT } from '../i18n/t'

/**
 * Mirrors the backend's `_TEMPLATE_NAME_RE`: the name doubles as the on-disk
 * file stem, so the client validates the same charset the server enforces and
 * can explain the rule before a request is spent on it.
 */
const NAME_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/
// Mirrors the backend's _TOOL_REF_RE (agents.py) so bad tool refs are caught
// at Add time instead of after a spent POST.
const TOOL_REF_RE = /^@?[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$/

/** Built-in tool suggestions; free-text covers everything else. */
const BUILTIN_TOOL_SUGGESTIONS = [
  'fs_read',
  'fs_write',
  'execute_bash',
  'grep',
  'glob',
  'web_search',
  'thinking',
]

/**
 * Tokenize an args line honoring simple single/double quoting, so
 * `--path "/my dir"` becomes two args, not three. No escapes — the hint
 * documents one flag per token outside quotes.
 */
export function splitArgs(line: string): string[] {
  const out: string[] = []
  let cur = ''
  let quote: '"' | "'" | null = null
  let started = false
  for (const ch of line) {
    if (quote) {
      if (ch === quote) quote = null
      else cur += ch
    } else if (ch === '"' || ch === "'") {
      quote = ch
      started = true
    } else if (/\s/.test(ch)) {
      if (started || cur) out.push(cur)
      cur = ''
      started = false
    } else {
      cur += ch
    }
  }
  if (started || cur) out.push(cur)
  return out
}

interface McpServerRow {
  name: string
  command: string
  /** Space-separated on screen; split into the args array on save. */
  args: string
}

interface Props {
  open: boolean
  onClose: () => void
  /** Called with the created template's name after a successful save. */
  onCreated: (name: string) => void
  /** Model names offered by the provider (the page already fetches these). */
  modelOptions: string[]
  /** Existing template names — duplicate names are refused before the POST. */
  existingNames: string[]
  /** Probed MCP server names, offered as `@server` mount suggestions. */
  mcpServerNames?: string[]
  /** When set, the dialog opens in edit mode pre-filled with this template's data. */
  editTarget?: {
    name: string
    description?: string
    model?: string
    prompt?: string
    skills?: string[]
    tools?: string[]
    allowedTools?: string[]
    mcpServers?: Record<string, { command: string; args?: string[] }>
    resources?: string[]
    deniedCommands?: string[]
  } | null
  /** When true, treat editTarget as a clone source (name cleared, submit creates new). */
  cloneMode?: boolean
}

/**
 * Authoring flow for a new user-owned Agent Template.
 *
 * One structured form over the same agent model the inspector reads: identity,
 * model, system prompt, skill mappings (catalog keys — the backend materializes
 * `skill://` resources through its enumerated catalog), tools with per-tool
 * auto-approve, inline MCP server definitions, steering resources, and
 * denied-command guardrails. Nothing saves until Create: the dialog collects a
 * complete draft and submits it in ONE `POST /api/agents/installed`, and a
 * validation rejection highlights the offending field without discarding
 * anything the user typed.
 */
export default function AgentTemplateCreator({
  open,
  onClose,
  onCreated,
  modelOptions,
  existingNames,
  mcpServerNames = [],
  editTarget = null,
  cloneMode = false,
}: Props) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [model, setModel] = useState('')
  const [prompt, setPrompt] = useState('')
  const [skills, setSkills] = useState<string[]>([])
  const [tools, setTools] = useState<string[]>([])
  const [allowed, setAllowed] = useState<string[]>([])
  const [toolDraft, setToolDraft] = useState('')
  const [mcpRows, setMcpRows] = useState<McpServerRow[]>([])
  const [resourcesText, setResourcesText] = useState('')
  const [deniedText, setDeniedText] = useState('')
  const [showAdvanced, setShowAdvanced] = useState(false)
  /** Server-side rejection mapped to the field it names; '' key = form-level. */
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})

  const isEdit = !!editTarget && !cloneMode

  useEffect(() => {
    if (!open) return
    if (!editTarget) { reset(); return }
    if (!cloneMode) setName(editTarget.name)
    else setName('')
    setDescription(editTarget.description || '')
    setModel(editTarget.model || '')
    setPrompt(editTarget.prompt || '')
    setSkills(editTarget.skills || [])
    setTools(editTarget.tools || [])
    setAllowed(editTarget.allowedTools || [])
    setMcpRows(
      Object.entries(editTarget.mcpServers || {}).map(([n, s]) => ({
        name: n,
        command: s.command,
        args: (s.args || []).join(' '),
      }))
    )
    setResourcesText((editTarget.resources || []).join('\n'))
    setDeniedText((editTarget.deniedCommands || []).join('\n'))
    if (editTarget.resources?.length || editTarget.deniedCommands?.length) setShowAdvanced(true)
    setFieldErrors({})
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editTarget, cloneMode])

  const { data: catalog = [], isLoading: catalogLoading } = useQuery<CatalogSkill[]>({
    queryKey: ['skills-catalog'],
    queryFn: async () => {
      const rows = await api.skills()
      return Array.isArray(rows) ? (rows as CatalogSkill[]).filter(s => s?.key) : []
    },
    staleTime: 30_000,
    enabled: open,
  })

  const nameTaken = !isEdit && existingNames.includes(name)
  const nameInvalid = name !== '' && !NAME_RE.test(name)
  const canSubmit = name !== '' && !nameInvalid && !nameTaken

  const toolSuggestions = useMemo(() => {
    const mounts = mcpServerNames.map(s => `@${s}`)
    const inline = mcpRows.map(r => r.name.trim()).filter(Boolean).map(s => `@${s}`)
    return [...BUILTIN_TOOL_SUGGESTIONS, ...mounts, ...inline].filter(t => !tools.includes(t))
  }, [mcpServerNames, mcpRows, tools])

  const reset = () => {
    setName(''); setDescription(''); setModel(''); setPrompt('')
    setSkills([]); setTools([]); setAllowed([]); setToolDraft('')
    setMcpRows([]); setResourcesText(''); setDeniedText('')
    setShowAdvanced(false); setFieldErrors({})
  }

  const submit = () => {
    // A named server row without a command would round-trip as a
    // section-level server 400; catch it client-side with the row intact.
    if (mcpRows.some(r => r.name.trim() && !r.command.trim())) {
      setFieldErrors({ mcpServers: i18nT('components.agentTemplateCreator.each_mcp_server_row_needs_a_command') })
      return
    }
    // The mirror case: a command without a name would be silently skipped by
    // the payload builder — the template would be created WITHOUT the server
    // the user just defined, with no feedback and no edit flow to repair it.
    if (mcpRows.some(r => r.command.trim() && !r.name.trim())) {
      setFieldErrors({ mcpServers: i18nT('components.agentTemplateCreator.each_mcp_server_row_needs_a_name') })
      return
    }
    // Two rows with the same name would overwrite each other in the
    // mcpServers object — the earlier definition silently discarded.
    const mcpNames = mcpRows.map(r => r.name.trim()).filter(Boolean)
    if (new Set(mcpNames).size !== mcpNames.length) {
      setFieldErrors({ mcpServers: i18nT('components.agentTemplateCreator.mcp_server_names_must_be_unique') })
      return
    }
    create.mutate()
  }

  const create = useMutation({
    mutationFn: () => {
      const mcpServers: Record<string, { command: string; args?: string[] }> = {}
      for (const row of mcpRows) {
        const n = row.name.trim()
        if (!n) continue
        const args = row.args.trim() ? splitArgs(row.args) : undefined
        mcpServers[n] = { command: row.command.trim(), ...(args ? { args } : {}) }
      }
      const lines = (t: string) => t.split('\n').map(s => s.trim()).filter(Boolean)
      // Item #2: commit pending tool draft into the payload.
      const effectiveTools = [...tools]
      const pendingTool = toolDraft.trim()
      if (pendingTool && TOOL_REF_RE.test(pendingTool) && !effectiveTools.includes(pendingTool)) {
        effectiveTools.push(pendingTool)
      }
      const payload = {
        name,
        ...(description.trim() ? { description: description.trim() } : {}),
        ...(model && model !== 'auto' ? { model } : {}),
        ...(prompt.trim() ? { prompt } : {}),
        ...(skills.length ? { skills } : {}),
        ...(effectiveTools.length ? { tools: effectiveTools } : {}),
        ...(allowed.length ? { allowedTools: allowed.filter(t => effectiveTools.includes(t)) } : {}),
        ...(Object.keys(mcpServers).length ? { mcpServers } : {}),
        ...(lines(resourcesText).length ? { resources: lines(resourcesText) } : {}),
        ...(lines(deniedText).length ? { deniedCommands: lines(deniedText) } : {}),
      }
      if (isEdit) {
        return api.agentUpdate(name, payload)
      }
      return api.agentCreate(payload)
    },
    onMutate: () => {
      setFieldErrors({})
      // Item #2: visually commit pending tool draft.
      const pending = toolDraft.trim()
      if (pending && TOOL_REF_RE.test(pending) && !tools.includes(pending)) {
        setTools(prev => [...prev, pending])
        setToolDraft('')
      }
    },
    onSuccess: (res: { name?: string }) => {
      const created = res?.name ?? name
      reset()
      onCreated(created)
    },
    onError: (e: unknown) => {
      // The backend names the offending field so the draft survives the
      // rejection — surface the message next to that field, or form-level.
      if (e instanceof ApiError) {
        try {
          const parsed = JSON.parse(e.body) as { field?: string; error?: string; code?: string }
          // Known machine codes map to localized strings — the raw backend
          // prose is English-only in an 11-locale UI. Unknown codes fall
          // back to the (more specific) server text.
          const codeMessages: Record<string, string> = {
            name_exists: i18nT('components.agentTemplateCreator.a_template_with_this_name_already_exists'),
            name_reserved: i18nT('components.agentTemplateCreator.this_name_is_reserved_by_the_framework'),
            sensitive_path: i18nT('components.agentTemplateCreator.this_path_points_at_a_protected_location'),
            glob_not_allowed: i18nT('components.agentTemplateCreator.resources_must_be_literal_paths_no_wildcards'),
            path_traversal: i18nT('components.agentTemplateCreator.paths_must_not_contain_parent_directory_segments'),
            env_secret_rejected: i18nT('components.agentTemplateCreator.credentials_cannot_be_stored_in_a_template'),
            governance_unavailable: i18nT('components.agentTemplateCreator.governance_check_unavailable_try_again'),
          }
          const message = (parsed.code && codeMessages[parsed.code]) || parsed.error || e.message
          setFieldErrors({ [parsed.field ?? '']: message })
          // A rejection on a field inside collapsed Advanced would otherwise
          // be completely invisible — the button just un-pends.
          if (parsed.field === 'resources' || parsed.field === 'deniedCommands') {
            setShowAdvanced(true)
          }
          // The dialog body scrolls; a rejection on a field above the fold
          // would otherwise be invisible (the button just un-pends).
          setTimeout(() => {
            document.querySelector('[data-field-error]')?.scrollIntoView({ block: 'center' })
          }, 0)
          return
        } catch { /* non-JSON body — fall through */ }
      }
      setFieldErrors({ '': e instanceof Error ? e.message : String(e) })
      // The form-level slot renders at the bottom of a scrollable body —
      // without this, a network-level failure just un-pends the button with
      // the message off-screen.
      setTimeout(() => {
        document.querySelector('[data-field-error]')?.scrollIntoView({ block: 'center' })
      }, 0)
    },
  })

  const addTool = (raw: string) => {
    const t = raw.trim()
    if (!t || tools.includes(t)) return
    // Mirror the backend's _TOOL_REF_RE so a space or bad character is
    // rejected at Add time with the rule, not after a spent POST.
    if (!TOOL_REF_RE.test(t)) {
      setFieldErrors(prev => ({ ...prev, tools: i18nT('components.agentTemplateCreator.tool_refs_use_letters_digits_and_char') }))
      return
    }
    setFieldErrors(prev => { const { tools: _drop, ...rest } = prev; return rest })
    setTools(prev => [...prev, t])
    setToolDraft('')
  }
  const removeTool = (t: string) => {
    setTools(prev => prev.filter(x => x !== t))
    setAllowed(prev => prev.filter(x => x !== t))
  }
  const toggleAllowed = (t: string) =>
    setAllowed(prev => (prev.includes(t) ? prev.filter(x => x !== t) : [...prev, t]))

  const toggleSkill = (key: string) =>
    setSkills(prev => (prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key]))

  const fieldError = (field: string) =>
    fieldErrors[field] ? (
      <div role="alert" data-field-error className="text-[12px] text-danger mt-1">{fieldErrors[field]}</div>
    ) : null

  return (
    <Dialog open={open} onOpenChange={v => { if (!v) onClose() }}>
      <DialogContent maxWidth={640} aria-label={isEdit ? i18nT('components.agentTemplateCreator.edit_agent_template') : cloneMode ? i18nT('components.agentTemplateCreator.clone_agent_template') : i18nT('components.agentTemplateCreator.create_agent_template')}>
        <DialogHeader>
          <DialogTitle>{isEdit ? i18nT('components.agentTemplateCreator.edit_agent_template') : cloneMode ? i18nT('components.agentTemplateCreator.clone_agent_template') : i18nT('components.agentTemplateCreator.create_agent_template')}</DialogTitle>
        </DialogHeader>
        <DialogBody className="space-y-4 max-h-[65vh] overflow-y-auto">
          {/* Identity */}
          <div>
            {/* eslint-disable-next-line jsx-a11y/label-has-for */}
            <label htmlFor="tpl-name" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('components.agentTemplateCreator.name')}</label>
            <Input id="tpl-name" autoFocus={!isEdit} value={name} placeholder={i18nT('components.agentTemplateCreator.e_g_code_reviewer')} onChange={e => setName(e.target.value.toLowerCase())} disabled={isEdit} className={`w-full mt-1 font-mono ${isEdit ? 'opacity-60' : ''}`} />
            {nameInvalid && <div role="alert" className="text-[12px] text-danger mt-1">{i18nT('components.agentTemplateCreator.must_start_with_a_letter_or_digit_then_lowercase')}</div>}
            {nameTaken && <div role="alert" className="text-[12px] text-danger mt-1">{i18nT('components.agentTemplateCreator.a_template_with_this_name_already_exists')}</div>}
            {fieldError('name')}
          </div>
          <div>
            {/* eslint-disable-next-line jsx-a11y/label-has-for */}
            <label htmlFor="tpl-desc" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('components.agentTemplateCreator.description')}</label>
            <Input id="tpl-desc" value={description} placeholder={i18nT('components.agentTemplateCreator.what_this_template_is_for')} onChange={e => setDescription(e.target.value)} className="w-full mt-1" />
            {fieldError('description')}
          </div>
          {/* Model */}
          <div>
            <div className="text-[11px] text-muted uppercase tracking-wider font-medium mb-1">{i18nT('components.agentTemplateCreator.model')}</div>
            <SimpleSelect
              options={['auto', ...modelOptions.filter(m => m && m !== 'auto')]}
              value={model || 'auto'}
              onChange={v => setModel(v === 'auto' ? '' : v)}
              aria-label={i18nT('components.agentTemplateCreator.model')}
            />
            {fieldError('model')}
          </div>
          {/* System prompt */}
          <div>
            {/* eslint-disable-next-line jsx-a11y/label-has-for */}
            <label htmlFor="tpl-prompt" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('components.agentTemplateCreator.system_prompt')}</label>
            <textarea id="tpl-prompt" aria-label={i18nT('components.agentTemplateCreator.system_prompt')} value={prompt} onChange={e => setPrompt(e.target.value)} rows={5}
              placeholder={i18nT('components.agentTemplateCreator.you_are_a_leave_empty_to_use_the_provider_defaul')}
              className="w-full mt-1 text-[13px] font-mono bg-bg-elevated border border-border rounded-md p-2.5 text-text placeholder:text-muted focus:outline-none focus:border-accent resize-y" />
            {fieldError('prompt')}
          </div>
          {/* Skills */}
          <div>
            <div className="flex items-center gap-1.5 mb-1">
              <span className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('components.agentTemplateCreator.skills')}</span>
              <InfoTip text={i18nT('components.agentTemplateCreator.mapped_as_skill_resources_the_agent_loads_only_t')} />
            </div>
            {catalog.length === 0 ? (
              !catalogLoading && <div className="text-[12px] text-muted italic">{i18nT('components.agentTemplateCreator.no_skills_in_the_catalog')}</div>
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {catalog.map(s => {
                  const on = skills.includes(s.key)
                  return (
                    <button key={s.key} type="button" aria-pressed={on} title={s.description || s.key}
                      onClick={() => toggleSkill(s.key)}
                      className={`px-2 py-1 rounded-full text-[12px] font-mono border transition-colors ${on ? 'bg-accent-subtle border-accent/40 text-text' : 'bg-bg-elevated border-border text-muted hover:border-border-strong hover:text-text'}`}>
                      {s.name}
                    </button>
                  )
                })}
              </div>
            )}
            {fieldError('skills')}
          </div>
          {/* Tools */}
          <div>
            <div className="flex items-center gap-1.5 mb-1">
              <span className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('components.agentTemplateCreator.tools')}</span>
              <InfoTip text={i18nT('components.agentTemplateCreator.built_in_tool_names_or_server_mounts_click_the_s')} />
            </div>
            {tools.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mb-1.5">
                {tools.map(t => {
                  const isAllowed = allowed.includes(t)
                  return (
                    <span key={t} className={`inline-flex items-center gap-1 pl-2 pr-1 py-1 rounded-full text-[12px] font-mono border ${isAllowed ? 'bg-ok/10 border-ok/30 text-ok' : 'bg-bg-elevated border-border text-text'}`}>
                      {t}
                      <button type="button" aria-pressed={isAllowed}
                        aria-label={i18nT('components.agentTemplateCreator.toggle_auto_approve_for_tool', { tool: t })}
                        title={i18nT('components.agentTemplateCreator.toggle_auto_approve_for_tool', { tool: t })}
                        onClick={() => toggleAllowed(t)}
                        className={`rounded-full p-0.5 inline-flex items-center gap-0.5 transition-colors ${isAllowed ? 'text-ok' : 'text-muted hover:text-ok'}`}>
                        <ShieldCheck className="lucide-inline" />
                        {/* The grant is a security state — green tint alone is
                            not discoverable, so granted chips carry the word. */}
                        {isAllowed && <span className="text-[10px] font-semibold uppercase">{i18nT('components.agentTemplateCreator.auto')}</span>}
                      </button>
                      <button type="button" aria-label={i18nT('components.agentTemplateCreator.remove_tool', { tool: t })}
                        onClick={() => removeTool(t)}
                        className="rounded-full p-0.5 text-muted hover:text-danger transition-colors">
                        <X className="lucide-inline" />
                      </button>
                    </span>
                  )
                })}
              </div>
            )}
            <div className="flex gap-1.5">
              <Input aria-label={i18nT('components.agentTemplateCreator.add_tool')} value={toolDraft} placeholder={i18nT('components.agentTemplateCreator.e_g_fs_read_or_server')}
                onChange={e => setToolDraft(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addTool(toolDraft) } }}
                className="flex-1 font-mono" />
              <Btn onClick={() => addTool(toolDraft)} disabled={!toolDraft.trim()} className="px-2.5"><Plus className="lucide-inline" /> {i18nT('components.agentTemplateCreator.add')}</Btn>
            </div>
            {/* Empty-list semantics are a permission surface — a constrained
                agent left blank ships the FULL default toolset, so the
                consequence must be visible, not buried in the tooltip. */}
            <div className="text-[11px] text-muted mt-1">
              {i18nT('components.agentTemplateCreator.leave_empty_to_use_the_default_toolset')}
              {tools.length > 0 && <> {i18nT('components.agentTemplateCreator.click_the_shield_to_auto_approve')}</>}
            </div>
            {toolSuggestions.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1.5">
                {toolSuggestions.slice(0, 12).map(t => (
                  <button key={t} type="button" onClick={() => addTool(t)}
                    className="px-1.5 py-0.5 rounded text-[11px] font-mono text-muted border border-dashed border-border hover:text-text hover:border-border-strong transition-colors">
                    + {t}
                  </button>
                ))}
                {toolSuggestions.length > 12 && (
                  <span className="px-1.5 py-0.5 text-[11px] text-muted italic">
                    {i18nT('components.agentTemplateCreator.plus_n_more', { count: String(toolSuggestions.length - 12) })}
                  </span>
                )}
              </div>
            )}
            {fieldError('tools')}
            {fieldError('allowedTools')}
          </div>
          {/* MCP servers */}
          <div>
            <div className="flex items-center gap-1.5 mb-1">
              <span className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('components.agentTemplateCreator.mcp_servers')}</span>
              <InfoTip text={i18nT('components.agentTemplateCreator.inline_server_definitions_started_for_this_agent')} />
            </div>
            {mcpRows.length > 0 && (
              <div className="flex gap-1.5 mb-0.5 items-center" aria-hidden="true">
                <span className="w-[120px] text-[10px] text-muted uppercase tracking-wider">{i18nT('components.agentTemplateCreator.name_2')}</span>
                <span className="flex-1 text-[10px] text-muted uppercase tracking-wider">{i18nT('components.agentTemplateCreator.command')}</span>
                <span className="flex-1 text-[10px] text-muted uppercase tracking-wider">{i18nT('components.agentTemplateCreator.args')}</span>
                <span className="w-[26px]" />
              </div>
            )}
            {mcpRows.map((row, i) => (
              <div key={i} className="flex gap-1.5 mb-1.5 items-center">
                <Input aria-label={i18nT('components.agentTemplateCreator.server_name')} value={row.name} placeholder={i18nT('components.agentTemplateCreator.name_2')}
                  onChange={e => setMcpRows(rows => rows.map((r, j) => (j === i ? { ...r, name: e.target.value } : r)))}
                  className="w-[120px] font-mono" />
                <Input aria-label={i18nT('components.agentTemplateCreator.command')} value={row.command} placeholder={i18nT('components.agentTemplateCreator.command')}
                  onChange={e => setMcpRows(rows => rows.map((r, j) => (j === i ? { ...r, command: e.target.value } : r)))}
                  className="flex-1 font-mono" />
                <Input aria-label={i18nT('components.agentTemplateCreator.arguments_space_separated')} value={row.args} placeholder={i18nT('components.agentTemplateCreator.args')}
                  onChange={e => setMcpRows(rows => rows.map((r, j) => (j === i ? { ...r, args: e.target.value } : r)))}
                  className="flex-1 font-mono" />
                <button type="button" aria-label={i18nT('components.agentTemplateCreator.remove_server', { name: row.name || String(i + 1) })}
                  onClick={() => setMcpRows(rows => rows.filter((_, j) => j !== i))}
                  className="text-muted hover:text-danger p-1 transition-colors"><X className="lucide-inline" /></button>
              </div>
            ))}
            <Btn onClick={() => setMcpRows(rows => [...rows, { name: '', command: '', args: '' }])} className="px-2.5 text-[12px]">
              <Plus className="lucide-inline" /> {i18nT('components.agentTemplateCreator.add_server')}
            </Btn>
            {/* The space-splitting rule must be visible, not aria-only: an
                unquoted path with spaces parses into several args, creates
                fine, and only breaks later at agent start. */}
            {mcpRows.length > 0 && (
              <div className="text-[11px] text-muted mt-1">{i18nT('components.agentTemplateCreator.args_are_space_separated_quote_values_containing')}</div>
            )}
            {fieldError('mcpServers')}
          </div>
          {/* Advanced: resources + guardrails */}
          <div>
            <button type="button" aria-expanded={showAdvanced} onClick={() => setShowAdvanced(v => !v)}
              className="text-[12px] text-muted hover:text-text transition-colors">
              {showAdvanced ? <ChevronDown className="lucide-inline" /> : <ChevronRight className="lucide-inline" />} {i18nT('components.agentTemplateCreator.advanced_resources_guardrails')}
            </button>
            {showAdvanced && (
              <div className="mt-2 space-y-3">
                <div>
                  {/* eslint-disable-next-line jsx-a11y/label-has-for */}
                  <label htmlFor="tpl-resources" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('components.agentTemplateCreator.resources_one_file_uri_per_line')}</label>
                  <textarea id="tpl-resources" aria-label={i18nT('components.agentTemplateCreator.resources_one_file_uri_per_line')} value={resourcesText} onChange={e => setResourcesText(e.target.value)} rows={2}
                    placeholder={i18nT('components.agentTemplateCreator.file_kiro_steering_md')}
                    className="w-full mt-1 text-[12px] font-mono bg-bg-elevated border border-border rounded-md p-2 text-text placeholder:text-muted focus:outline-none focus:border-accent resize-y" />
                  {fieldError('resources')}
                </div>
                <div>
                  {/* eslint-disable-next-line jsx-a11y/label-has-for */}
                  <label htmlFor="tpl-denied" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('components.agentTemplateCreator.denied_commands_one_pattern_per_line')}</label>
                  <textarea id="tpl-denied" aria-label={i18nT('components.agentTemplateCreator.denied_commands_one_pattern_per_line')} value={deniedText} onChange={e => setDeniedText(e.target.value)} rows={2}
                    placeholder={i18nT('components.agentTemplateCreator.git_push_force')}
                    className="w-full mt-1 text-[12px] font-mono bg-bg-elevated border border-border rounded-md p-2 text-text placeholder:text-muted focus:outline-none focus:border-accent resize-y" />
                  {fieldError('deniedCommands')}
                </div>
              </div>
            )}
          </div>
          {fieldError('')}
        </DialogBody>
        <DialogFooter>
          {/* Cancel preserves the draft, matching Escape/overlay close — a
              mis-click beside Create must not wipe a long system prompt.
              State resets only after a successful create. */}
          <Btn onClick={onClose}>{i18nT('components.agentTemplateCreator.cancel')}</Btn>
          <Btn primary disabled={!canSubmit || create.isPending} onClick={submit}>
            {create.isPending
              ? (isEdit ? i18nT('components.agentTemplateCreator.saving') : i18nT('components.agentTemplateCreator.creating'))
              : (isEdit ? i18nT('components.agentTemplateCreator.save_changes') : i18nT('components.agentTemplateCreator.create_template'))}
          </Btn>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
