import { useState, useEffect, useMemo, useRef } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Brain, Plus, Shield, X, ChevronDown } from 'lucide-react'
import { api } from '../api/client'
import { Btn, Input } from './ui'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogBody, DialogFooter,
} from './ui/dialog'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from './ui/dropdown-menu'
import SimpleSelect from './SimpleSelect'
import { i18nT } from '../i18n/t'

/**
 * One MCP server entry as stored in a template spec. `args` is the only field
 * this dialog edits; `command`, `env` and any other key are carried through
 * untouched so an edit cannot strip them.
 */
type McpServerSpec = { args?: string[] } & Record<string, unknown>

interface AgentDetail {
  name: string
  description?: string
  model?: string
  prompt?: string
  skills?: string[]
  tools?: string[]
  allowedTools?: string[]
  mcpServers?: Record<string, McpServerSpec>
  toolsSettings?: { execute_bash?: { deniedCommands?: string[] } }
  /** Hand-authored resource URIs. Not editable here, but carried on a clone. */
  resources?: string[]
  hooks?: unknown
  includeMcpJson?: boolean
  /**
   * Fields the create path cannot express, declared so the clone warning can
   * detect them rather than dropping them silently. `toolAliases` is written by
   * Kiro Crew's Connections pass; the other two are documented kiro-cli fields.
   */
  toolAliases?: Record<string, string>
  keyboardShortcut?: string
  welcomeMessage?: string
}

/**
 * Spec keys a clone cannot reproduce, because the create endpoint builds the spec
 * from an allowlist that does not include them. Cloning a template that carries
 * one would silently produce a copy that behaves differently, so the dialog names
 * them instead of dropping them quietly. `resources` is deliberately absent: the
 * endpoint does accept it, so a clone carries it.
 */
const CLONE_UNCOPYABLE_KEYS = [
  'hooks',
  'includeMcpJson',
  'toolsSettings',
  // Documented kiro-cli agent fields (docs/reference/kiro-cli/custom-agents/
  // configuration-reference.md) plus `toolAliases`, which Kiro Crew's own
  // Connections pass writes into a spec. The create path's allowlist does not
  // carry any of them, so a clone drops them -- named here rather than dropped
  // quietly, since a copy that silently loses a keyboard shortcut or a resolved
  // tool-alias map behaves differently from its original.
  'toolAliases',
  'keyboardShortcut',
  'welcomeMessage',
] as const

/** An MCP server row in the form. */
interface McpDraft {
  name: string
  /** Space-separated args, as typed in the args field. */
  args: string
  /**
   * The argument array as LOADED, when this server came from a stored spec.
   * Submit sends it verbatim while `args` still matches its space-joined form, so
   * an argument containing a space survives a round trip untouched.
   */
  original?: string[]
  /**
   * Every key of the loaded server record except `args`. The dialog does not
   * render these, but it must submit them back verbatim: the backend replaces
   * a key that the request carries, so an args-only object would delete the
   * server's `command` and `env` and leave it unusable.
   */
  rest: Record<string, unknown>
}

interface Props {
  open: boolean
  onClose: () => void
  onCreated: (name: string) => void
  modelOptions: { name: string; label?: string }[]
  existingNames: string[]
  mcpServerNames?: string[]
  editTarget?: AgentDetail | null
  cloneMode?: boolean
}

/** Sanitize a name to a valid agent template slug. */
function slugify(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9_-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '')
}

export default function AgentTemplateCreator({
  open, onClose, onCreated, modelOptions, existingNames, mcpServerNames = [], editTarget, cloneMode,
}: Props) {
  const isEdit = !!editTarget && !cloneMode
  const isClone = !!editTarget && !!cloneMode

  // Form state
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [model, setModel] = useState('auto')
  const [prompt, setPrompt] = useState('')
  const [tools, setTools] = useState<string[]>([])
  const [allowedTools, setAllowedTools] = useState<string[]>([])
  const [mcpServers, setMcpServers] = useState<McpDraft[]>([])
  const [skills, setSkills] = useState<string[]>([])

  // Tool input
  const [toolInput, setToolInput] = useState('')
  const [mcpName, setMcpName] = useState('')
  const [mcpCommand, setMcpCommand] = useState('')
  const [mcpArgs, setMcpArgs] = useState('')

  // Error/validation
  const [nameError, setNameError] = useState('')
  const [mcpError, setMcpError] = useState('')
  const nameRef = useRef<HTMLInputElement>(null)
  /**
   * Keys the loaded record carried, so submit can tell "absent because the fetch
   * failed" from "empty because the user cleared it". Empty for a create.
   */
  const loadedFields = useRef<Set<string>>(new Set())
  /**
   * The VALUES the loaded record carried, in the shape submit would send them.
   * Submit compares against these so an edit sends only fields the user actually
   * changed: re-sending a field the dialog merely displayed replaces whatever a
   * concurrent external edit wrote to it, with no way for the backend to tell a
   * stale echo from a deliberate write. Empty for a create.
   */
  const loadedValues = useRef<Record<string, unknown>>({})

  // Skill catalog
  const { data: skillCatalog = [] } = useQuery({
    queryKey: ['skills-catalog'],
    queryFn: async () => {
      const skills = await api.skills()
      return Array.isArray(skills) ? skills.map((s: { key: string; name: string }) => s.key) : []
    },
    enabled: open,
  })

  // Seed form when editing/cloning
  useEffect(() => {
    if (!open) return
    if (editTarget) {
      // Record which fields the loaded record actually CARRIED. The detail fetch
      // can fail, in which case the caller seats a list-only row whose prompt,
      // tools and mcpServers are simply absent -- and a field copied from
      // `undefined` into state is indistinguishable from one the user cleared.
      // Submit consults this set so an unloaded field is omitted rather than
      // sent as empty, which the backend would treat as a deliberate erase.
      const loaded = new Set<string>()
      for (const key of ['description', 'model', 'prompt', 'skills', 'tools', 'allowedTools', 'mcpServers'] as const) {
        if (editTarget[key] !== undefined) loaded.add(key)
      }
      loadedFields.current = loaded
      // Normalised to the same shape submit builds, so the comparison there is
      // between like and like rather than between a spec field and a form value.
      loadedValues.current = {
        description: editTarget.description || '',
        // Normalised exactly as submit does: it sends '' for the `auto` sentinel, so
        // storing the raw 'auto' here would make an untouched model look changed.
        model: editTarget.model && editTarget.model !== 'auto' ? editTarget.model : '',
        prompt: editTarget.prompt || '',
        skills: editTarget.skills ?? [],
        tools: editTarget.tools ?? [],
        allowedTools: editTarget.allowedTools ?? [],
        mcpServers: editTarget.mcpServers ?? {},
      }

      setName(isClone ? '' : editTarget.name)
      setDescription(editTarget.description || '')
      setModel(editTarget.model || 'auto')
      setPrompt(editTarget.prompt || '')
      // Array.isArray, not `|| []`: agent specs live in a user-writable directory
      // shared with other tools, so a hand-edited `"tools": "fs_read"` reaches the
      // detail endpoint as a string. `|| []` keeps the string, and the first
      // `.map` over it throws and blanks the whole dialog.
      setSkills(Array.isArray(editTarget.skills) ? editTarget.skills : [])
      setTools(Array.isArray(editTarget.tools) ? editTarget.tools : [])
      setAllowedTools(Array.isArray(editTarget.allowedTools) ? editTarget.allowedTools : [])
      const servers: McpDraft[] = (
        editTarget.mcpServers
        && typeof editTarget.mcpServers === 'object'
        && !Array.isArray(editTarget.mcpServers)
      )
        ? Object.entries(editTarget.mcpServers).map(([n, v]) => {
            const { args, ...rest } = v || {}
            const original = Array.isArray(args) ? args : undefined
            return {
              name: n,
              args: (original ?? []).join(' '),
              // The display string is space-joined and submit re-splits on
              // whitespace, which silently rewrites any argument that CONTAINS a
              // space: ["--header", "User Agent"] would come back as three
              // arguments and the server would receive a different argv. Keeping
              // the loaded array lets submit send it verbatim unless the user
              // actually edits the field.
              original,
              rest,
            }
          })
        : []
      setMcpServers(servers)
    } else {
      // A create authors every field, so all of them are the user's to send.
      loadedFields.current = new Set()
      loadedValues.current = {}
      setName('')
      setDescription('')
      setModel('auto')
      setPrompt('')
      setSkills([])
      setTools([])
      setAllowedTools([])
      setMcpServers([])
    }
    setToolInput('')
    setMcpName('')
    setMcpCommand('')
    setMcpArgs('')
    setNameError('')
    setMcpError('')
  }, [open, editTarget, isClone])

  // MCP server suggestions
  const mcpSuggestions = useMemo(() => {
    const used = new Set(mcpServers.map(s => s.name))
    return mcpServerNames.filter(n => !used.has(n))
  }, [mcpServerNames, mcpServers])

  // Named on a clone so the user learns the copy will differ BEFORE saving it.
  // Silence here is the same data-loss class the edit path was fixed for.
  const uncopyableKeys = useMemo(
    () =>
      isClone && editTarget
        ? CLONE_UNCOPYABLE_KEYS.filter(k => editTarget[k] !== undefined)
        : [],
    [isClone, editTarget],
  )

  // Validate name
  const validateName = (v: string): string => {
    if (!v.trim()) return i18nT('components.agentTemplateCreator.name_required')
    const slug = slugify(v)
    if (slug !== v) return i18nT('components.agentTemplateCreator.name_must_be_lowercase')
    if (!isEdit && existingNames.includes(v)) return i18nT('components.agentTemplateCreator.name_already_exists')
    return ''
  }

  // Submit
  const createMut = useMutation({
    mutationFn: (payload: object) => api.agentCreate(payload),
    onSuccess: () => { onCreated(name); onClose() },
  })

  const updateMut = useMutation({
    mutationFn: (payload: object) => api.agentUpdate(editTarget!.name, payload),
    onSuccess: () => { onCreated(editTarget!.name); onClose() },
  })

  const handleSubmit = () => {
    const err = validateName(name)
    if (err && !isEdit) {
      setNameError(err)
      nameRef.current?.focus()
      return
    }

    // Commit anything still sitting in the draft inputs. A user who types a tool
    // name and presses Create never pressed the add button, and dropping it
    // silently ships a template missing the tool they asked for. Computed here
    // rather than by calling addTool()/addMcpServer(): those setState calls do not
    // land before this closure reads `tools`/`mcpServers`, so the draft would
    // still be lost.
    const pendingTool = toolInput.trim()
    const effectiveTools = pendingTool && !tools.includes(pendingTool)
      ? [...tools, pendingTool]
      : [...tools]

    const pendingMcp = mcpName.trim()
    // Same transport derivation as addMcpServer: a name typed but not yet added
    // is submitted as a row, so it needs the command/url the entry requires or it
    // is dropped with an error the user cannot act on.
    const pendingTransport = mcpCommand.trim()
    const pendingRest = pendingTransport
      ? (/^https?:\/\//i.test(pendingTransport)
          ? { url: pendingTransport }
          : { command: pendingTransport })
      : {}
    const effectiveMcp = pendingMcp && !mcpServers.some(s => s.name === pendingMcp)
      ? [...mcpServers, {
          name: pendingMcp,
          args: mcpArgs,
          rest: pendingRest as McpDraft['rest'],
        }]
      : [...mcpServers]

    // Build MCP servers object. Each entry is the loaded record with only `args`
    // rewritten, so fields the dialog does not model (command, env, ...) survive
    // an edit or a clone. Clearing the args field drops `args` and nothing else,
    // which is how an args removal is expressed.
    //
    // An entry with no `command` and no `url` is dropped: kiro-cli cannot launch
    // it, and the whole spec is rejected on an unusable server, which silently
    // falls the session back to the default agent. The dialog collects the
    // command (or an http URL) for a hand-added row, so this now only fires when
    // the field was left empty -- an error the user can act on.
    const mcpObj: Record<string, McpServerSpec> = {}
    const droppedServers: string[] = []
    for (const s of effectiveMcp) {
      // Unedited args go back exactly as they arrived. Only a field the user
      // actually changed is re-derived by splitting, which is lossy for any
      // argument containing whitespace.
      const unedited = s.original !== undefined && s.args === s.original.join(' ')
      const args = unedited
        ? s.original
        : s.args.trim()
          ? s.args.trim().split(/\s+/)
          : undefined
      const entry: McpServerSpec = args ? { ...s.rest, args } : { ...s.rest }
      if (!entry.command && !entry.url) {
        droppedServers.push(s.name)
        continue
      }
      mcpObj[s.name] = entry
    }
    if (droppedServers.length > 0) {
      setMcpError(
        i18nT('components.agentTemplateCreator.mcp_needs_command', {
          names: droppedServers.join(', '),
        }),
      )
      return
    }
    setMcpError('')

    const payload: Record<string, unknown> = {
      name: isEdit ? editTarget!.name : name,
    }

    // On an EDIT, send only what the user actually changed. Re-sending a field the
    // dialog merely displayed replaces whatever a concurrent external edit wrote to
    // it: the backend cannot tell a stale echo of the loaded value from a deliberate
    // write, so every unchanged field it receives is an overwrite it must honour.
    // Combined with absent-means-preserve, omitting unchanged fields means an
    // external edit to a field this user never touched survives.
    //
    // The never-loaded guard from before is kept: a field the fetch did not deliver
    // and that is still empty is omitted, because sending it as empty is
    // indistinguishable from asking to erase it. So an edit sends a field when the
    // user CHANGED a loaded one -- a clear included, since clearing is a change --
    // or authored a value into one that never loaded.
    const unchanged = (key: string, value: unknown): boolean =>
      key in loadedValues.current
      && JSON.stringify(loadedValues.current[key]) === JSON.stringify(value)

    const send = (key: string, value: unknown, authored: boolean): void => {
      if (!isEdit) {
        payload[key] = value
        return
      }
      if (unchanged(key, value)) return
      if (loadedFields.current.has(key) || authored) {
        payload[key] = value
      }
    }
    send('description', description, description.trim() !== '')
    send('model', model === 'auto' ? '' : model, model !== 'auto')
    send('prompt', prompt, prompt.trim() !== '')
    send('skills', skills, skills.length > 0)
    send('tools', effectiveTools, effectiveTools.length > 0)
    send('allowedTools', [...allowedTools], allowedTools.length > 0)

    // A clone goes through POST, so there is nothing on disk to preserve it
    // against -- the backend's absent-means-preserve contract protects edits
    // only. Carry the resource globs explicitly, or a cloned template silently
    // loses the steering files the original read.
    if (isClone && editTarget?.resources !== undefined) {
      payload.resources = editTarget.resources
    }

    // mcpServers follows the same rule, with one addition: on an edit that DID
    // load them, the empty object must still be sent, since it is the only way to
    // express removing the last server.
    if (!isEdit) {
      payload.mcpServers = mcpObj
    } else if (!unchanged('mcpServers', mcpObj)) {
      if (Object.keys(mcpObj).length > 0 || loadedFields.current.has('mcpServers')) {
        payload.mcpServers = mcpObj
      }
    }

    if (isEdit) {
      updateMut.mutate(payload)
    } else {
      createMut.mutate(payload)
    }
  }

  const isPending = createMut.isPending || updateMut.isPending
  const mutError = createMut.error || updateMut.error

  const addTool = () => {
    const t = toolInput.trim()
    if (!t || tools.includes(t)) return
    setTools([...tools, t])
    setToolInput('')
  }

  const removeTool = (t: string) => {
    setTools(tools.filter(x => x !== t))
    // Drop the auto-approve grant with the tool. Leaving it behind means removing an
    // approved tool and later re-adding it silently restores confirmation-skipping
    // for it -- the grant outliving the thing it was granted for. `allowedTools` is
    // the auto-approve list, so a stale entry is a live privilege, not dead state.
    setAllowedTools(allowedTools.filter(x => x !== t))
  }

  const toggleApproved = (t: string) => {
    if (allowedTools.includes(t)) {
      setAllowedTools(allowedTools.filter(x => x !== t))
    } else {
      setAllowedTools([...allowedTools, t])
    }
  }



  const addMcpServer = (serverName?: string) => {
    const n = (serverName || mcpName).trim()
    if (!n || mcpServers.some(s => s.name === n)) return
    // The transport goes into `rest`, which is what submit spreads into the
    // entry. Without it every hand-added row failed the launchable-transport
    // check and the Add button could not produce a saveable server at all.
    // `url` when it looks like one, `command` otherwise -- the two are the
    // stdio/http halves of the same required field.
    const transport = serverName ? '' : mcpCommand.trim()
    const rest: Partial<McpServerSpec> = transport
      ? (/^https?:\/\//i.test(transport) ? { url: transport } : { command: transport })
      : {}
    setMcpServers([...mcpServers, {
      name: n,
      args: serverName ? '' : mcpArgs,
      rest: rest as McpDraft['rest'],
    }])
    setMcpName('')
    setMcpCommand('')
    setMcpArgs('')
  }

  const removeMcpServer = (n: string) => setMcpServers(mcpServers.filter(s => s.name !== n))

  const title = isEdit
    ? i18nT('components.agentTemplateCreator.edit_agent_template')
    : isClone
      ? i18nT('components.agentTemplateCreator.clone_agent_template')
      : i18nT('components.agentTemplateCreator.create_agent_template')

  return (
    <Dialog open={open} onOpenChange={v => { if (!v) onClose() }}>
      <DialogContent maxWidth={720}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>

        <DialogBody className="space-y-5">
          {uncopyableKeys.length > 0 && (
            <p className="text-[11.5px] text-warn border border-warn/40 bg-warn/10 rounded-md px-2.5 py-1.5">
              {i18nT('components.agentTemplateCreator.clone_drops_keys', {
                keys: uncopyableKeys.join(', '),
              })}
            </p>
          )}
          {/* Name */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.name')}
            </label>
            <Input
              ref={nameRef}
              value={name}
              onChange={e => { setName(e.target.value); setNameError('') }}
              placeholder="my-agent"
              disabled={isEdit}
              className="w-full font-mono text-[13px]"
              aria-invalid={!!nameError}
            />
            {nameError && <p className="m-0 text-[11px] text-danger">{nameError}</p>}
          </fieldset>

          {/* Description */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.description')}
            </label>
            <Input
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder={i18nT('components.agentTemplateCreator.description_placeholder')}
              className="w-full text-[13px]"
            />
          </fieldset>

          {/* Model */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.model')}
            </label>
            <SimpleSelect
              options={['auto', ...modelOptions.map(m => m.name)]}
              value={model}
              onChange={setModel}
              aria-label={i18nT('components.agentTemplateCreator.model')}
              style={{ width: '100%' }}
            />
          </fieldset>

          {/* Skills */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.skills')}
            </label>
            <div className="flex flex-wrap gap-1.5">
              {/* Skill selection is the value of one field, not a row of actions:
                  rendered as a checkbox group so the on/off state is announced
                  and reachable by keyboard instead of being carried by colour on
                  an unbounded set of peer buttons. */}
              {skillCatalog.map(sk => {
                const on = skills.includes(sk)
                return (
                  <label
                    key={sk}
                    className={`inline-flex items-center gap-1 px-2 py-1 text-[12px] font-mono rounded-full border cursor-pointer ${
                      on
                        ? 'bg-accent/15 border-accent/40 text-accent'
                        : 'bg-bg-elevated border-border text-muted hover:text-text'
                    }`}
                  >
                    <input
                      type="checkbox"
                      className="h-3 w-3 shrink-0 accent-[var(--accent)]"
                      checked={on}
                      onChange={() => {
                        setSkills(on ? skills.filter(s => s !== sk) : [...skills, sk])
                      }}
                    />
                    <Brain className="lucide-inline" aria-hidden /> {sk}
                  </label>
                )
              })}
              {skillCatalog.length === 0 && (
                <span className="text-[12px] text-muted">{i18nT('components.agentTemplateCreator.no_skills_available')}</span>
              )}
            </div>
          </fieldset>

          {/* Tools */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.tools')}
            </label>
            <div className="flex gap-2">
              <Input
                value={toolInput}
                onChange={e => setToolInput(e.target.value)}
                placeholder={i18nT('components.agentTemplateCreator.tool_name_placeholder')}
                className="flex-1 text-[13px] font-mono"
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addTool() } }}
              />
              <Btn
                type="button"
                onClick={addTool}
                aria-label={i18nT('components.agentTemplateCreator.add_tool')}
              >
                <Plus className="lucide-inline" />
              </Btn>
            </div>
            {tools.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mt-1.5">
                {tools.map(t => (
                  <span key={t} className="group inline-flex items-center gap-1 px-2 py-1 rounded-full text-[12px] font-mono bg-bg-elevated border border-border text-text">
                    {t}
                    <button
                      type="button"
                      className={`ml-0.5 p-0.5 rounded transition-colors ${allowedTools.includes(t) ? 'text-ok' : 'text-muted hover:text-ok'}`}
                      onClick={() => toggleApproved(t)}
                      title={i18nT('components.agentTemplateCreator.toggle_auto_approve')}
                      aria-label={i18nT('components.agentTemplateCreator.toggle_auto_approve_for', { name: t })}
                      aria-pressed={allowedTools.includes(t)}
                    >
                      <Shield size={12} aria-hidden />
                    </button>
                    <button
                      type="button"
                      className="text-muted hover:text-danger"
                      onClick={() => removeTool(t)}
                      aria-label={i18nT('components.agentTemplateCreator.remove_tool', { name: t })}
                    >
                      <X size={12} aria-hidden />
                    </button>
                  </span>
                ))}
              </div>
            )}
            {tools.length > 0 && (
              <p className="m-0 text-[11px] text-muted flex items-center gap-1">
                <Shield size={11} className="text-ok" aria-hidden />
                {i18nT('components.agentTemplateCreator.shield_helper')}
              </p>
            )}
          </fieldset>

          {/* MCP Servers */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.mcp_servers')}
            </label>
            {mcpServers.map(s => (
              <div key={s.name} className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-2.5 py-1.5">
                <span className="text-[12px] font-mono text-aim flex-1 min-w-0 truncate">{s.name}</span>
                {s.args && <span className="text-[11px] text-muted font-mono truncate max-w-[140px]">{s.args}</span>}
                <button
                  type="button"
                  className="text-muted hover:text-danger shrink-0"
                  aria-label={i18nT('components.agentTemplateCreator.remove_mcp_server', { name: s.name })}
                  onClick={() => removeMcpServer(s.name)}
                >
                  <X size={14} />
                </button>
              </div>
            ))}
            {/* Stacks on a narrow viewport: four controls on one row leave the
                name input unusably thin at 320px, so the fixed args width only
                applies from the sm breakpoint up. */}
            <div className="flex flex-col gap-2 sm:flex-row">
              <Input
                value={mcpName}
                onChange={e => setMcpName(e.target.value)}
                placeholder={i18nT('components.agentTemplateCreator.server_name_placeholder')}
                className="flex-1 min-w-0 text-[13px] font-mono"
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addMcpServer() } }}
              />
              <Input
                value={mcpCommand}
                onChange={e => setMcpCommand(e.target.value)}
                placeholder={i18nT('components.agentTemplateCreator.command_placeholder')}
                aria-label={i18nT('components.agentTemplateCreator.server_command')}
                className="flex-1 min-w-0 text-[12px] font-mono"
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addMcpServer() } }}
              />
              <Input
                value={mcpArgs}
                onChange={e => setMcpArgs(e.target.value)}
                placeholder={i18nT('components.agentTemplateCreator.args_placeholder')}
                aria-label={i18nT('components.agentTemplateCreator.server_arguments')}
                className="w-full sm:w-[140px] text-[12px] font-mono"
              />
              <Btn
                type="button"
                className="shrink-0"
                onClick={() => addMcpServer()}
                aria-label={i18nT('components.agentTemplateCreator.add_mcp_server')}
              >
                <Plus className="lucide-inline" />
              </Btn>
            </div>
            {mcpSuggestions.length > 0 && (
              <div className="mt-1">
                {/* One control, not a chip per server: the list is as long as the
                    user's configured server count, so peer buttons carry no
                    ranking and wrap unpredictably under width pressure. Picking a
                    name fills the name input rather than adding the server, so the
                    command field is still there to complete. */}
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Btn type="button" className="px-2 py-0.5 text-[11px] text-muted">
                      {i18nT('components.agentTemplateCreator.pick_configured_server')}
                      <ChevronDown className="lucide-inline" />
                    </Btn>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="start" className="min-w-[180px] max-h-[240px] overflow-y-auto">
                    {mcpSuggestions.map(s => (
                      <DropdownMenuItem key={s} onSelect={() => setMcpName(s)}>
                        <span className="font-mono text-[12px] text-aim">{s}</span>
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            )}
            {mcpError && (
              <p className="text-[11.5px] text-danger" role="alert">{mcpError}</p>
            )}
          </fieldset>

          {/* System Prompt */}
          <fieldset className="space-y-1.5">
            <label className="text-[12px] font-semibold uppercase tracking-wider text-muted">
              {i18nT('components.agentTemplateCreator.system_prompt')}
            </label>
            <textarea
              value={prompt}
              onChange={e => setPrompt(e.target.value)}
              placeholder={i18nT('components.agentTemplateCreator.prompt_placeholder')}
              className="w-full min-h-[100px] rounded-md border border-border bg-bg-elevated px-3 py-2 text-[12.5px] font-mono text-text placeholder:text-muted resize-y outline-none focus:border-accent"
              rows={5}
            />
          </fieldset>

          {/* Denied Commands */}
          {/* Mutation error */}
          {mutError && (
            <p className="m-0 text-[12px] text-danger rounded-md border border-danger/30 bg-danger/10 px-3 py-2">
              {mutError instanceof Error ? mutError.message : String(mutError)}
            </p>
          )}
        </DialogBody>

        <DialogFooter>
          <Btn type="button" onClick={onClose}>
            {i18nT('components.agentTemplateCreator.cancel')}
          </Btn>
          <Btn type="button" className="bg-accent text-white hover:bg-accent/90" onClick={handleSubmit} disabled={isPending}>
            {isPending
              ? i18nT('components.agentTemplateCreator.saving')
              : isEdit
                ? i18nT('components.agentTemplateCreator.save_changes')
                : i18nT('components.agentTemplateCreator.create')}
          </Btn>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
