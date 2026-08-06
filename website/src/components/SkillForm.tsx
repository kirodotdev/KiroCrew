import { useState } from 'react'
import { Input } from './ui'

import { i18nT } from '../i18n/t'
export interface SkillFormData {
  name: string
  category: string
  description: string
  triggers: string
  tags: string
  always: boolean
  body: string
  /** Frontmatter keys the form does not model, kept as their VERBATIM source
   *  lines so a round-trip cannot destroy or retype them. */
  extra?: Record<string, string>
  /** Raw markdown content (frontmatter + body). Used in raw editing mode. */
  raw?: string
}

interface SkillFormProps {
  data: SkillFormData
  onChange: (data: SkillFormData) => void
  /** Hide name/category fields (used when editing an existing skill) */
  hideIdentity?: boolean
  /** Show raw mode toggle (default: true) */
  allowRaw?: boolean
}

/** Parse YAML frontmatter from raw skill content. Shared by SkillsTab (display) and SkillForm (edit).
 *
 *  Two views of the same block, deliberately built by DIFFERENT rules:
 *
 *  - `meta` is a flattened SCALAR view, used to read the fields the form models.
 *    Only an INDENTED line continues a value, because that is what makes a value
 *    multi-line in YAML. A top-level `# comment` after `always: true` must not
 *    become part of `always`, or the form reads the flag as unset and drops it.
 *  - `rawFields` keeps each key's ORIGINAL source lines verbatim, for re-emitting
 *    a field nobody modelled (see `assembleSkillContent`). Here a field's block is
 *    everything up to the next top-level key — the inverse of the key test, so no
 *    continuation shape (indented, blank, indentless `- item`, comment) is missed.
 *
 *  A comment attached to a MODELLED key is not preserved: the form owns those five
 *  fields and re-emits them from its own state, the same reason their original
 *  spacing is not preserved either. */
export function parseFrontmatter(raw: string): {
  meta: Record<string, string>
  rawFields: Record<string, string>
  body: string
} {
  if (!raw.startsWith('---')) return { meta: {}, rawFields: {}, body: raw }
  const end = raw.indexOf('\n---', 3)
  if (end === -1) return { meta: {}, rawFields: {}, body: raw }
  const yamlBlock = raw.slice(4, end)
  const meta: Record<string, string> = {}
  const rawFields: Record<string, string> = {}
  let currentKey = ''
  /* A blank line inside a field belongs to it, but a blank line before the next
     key (or before the closing `---`) does not. Hold them until more of the same
     field arrives, so an interior blank survives and a trailing one is not
     invented. */
  let pendingBlanks: string[] = []
  for (const line of yamlBlock.split('\n')) {
    const match = line.match(/^(\w[\w-]*):\s*(.*)$/)
    if (match) {
      pendingBlanks = []
      currentKey = match[1]
      const val = match[2].trim()
      // Handle YAML block scalar indicators (| and >)
      meta[currentKey] = (val === '|' || val === '>') ? '' : val
      rawFields[currentKey] = line
      continue
    }
    if (!currentKey) continue
    if (line.trim() === '') {
      pendingBlanks.push(line)
      continue
    }
    const indented = line.startsWith('  ') || line.startsWith('\t')
    for (const blank of pendingBlanks) {
      rawFields[currentKey] += '\n' + blank
      if (indented) meta[currentKey] += '\n'
    }
    pendingBlanks = []
    rawFields[currentKey] += '\n' + line
    if (indented) meta[currentKey] += (meta[currentKey] ? '\n' : '') + line.trim()
  }
  return { meta, rawFields, body: raw.slice(end + 4).trim() }
}

/** Frontmatter keys the structured form owns. Everything else is carried
 *  through untouched — see `extra` on SkillFormData. */
const MANAGED_KEYS = new Set(['name', 'description', 'always', 'triggers', 'tags'])

/** Assemble YAML frontmatter + body from structured fields */
export function assembleSkillContent(data: SkillFormData): string {
  // If raw mode was used, return raw content directly
  if (data.raw !== undefined) return data.raw

  const lines = ['---']
  lines.push(`name: ${data.name}`)
  if (data.description) {
    if (data.description.includes('\n')) {
      lines.push('description: |')
      for (const l of data.description.split('\n')) lines.push(`  ${l}`)
    } else {
      lines.push(`description: ${data.description}`)
    }
  }
  if (data.always) lines.push('always: true')
  if (data.triggers) lines.push(`triggers: ${data.triggers}`)
  if (data.tags) lines.push(`tags: [${data.tags}]`)
  // Carry through every key the form does not model. Without this, saving a
  // skill from the structured editor destroys frontmatter the runtime reads —
  // `repo_scope` (the matcher's repo guard) and `inject_on_trigger` (the
  // full-body opt-out) among them — because the form rebuilds the block from
  // its own fields rather than editing the original.
  //
  // The carried value is the field's ORIGINAL source lines, re-emitted verbatim.
  // Reserializing from a parsed value cannot be done safely here: the form does
  // not know a field's YAML type, so a list or nested map would come back as a
  // `|` block scalar — i.e. a string — and a folded `>` scalar would change
  // semantics. Verbatim is the only lossless option for a field we do not model.
  for (const [key, block] of Object.entries(data.extra || {})) {
    if (MANAGED_KEYS.has(key)) continue
    for (const line of block.split('\n')) lines.push(line)
  }
  lines.push('---')
  lines.push('')
  lines.push(data.body || `# ${data.name}\n`)
  return lines.join('\n')
}

/** Parse raw skill content into structured form data */
export function parseSkillContent(raw: string, key: string): SkillFormData {
  const slash = key.indexOf('/')
  const name = slash > 0 ? key.slice(slash + 1) : key
  const category = slash > 0 ? key.slice(0, slash) : ''

  const { meta, rawFields, body } = parseFrontmatter(raw)
  if (!Object.keys(meta).length && !raw.startsWith('---')) {
    return { name, category, description: '', triggers: '', tags: '', always: false, body: raw }
  }

  // Clean up tags — strip brackets
  const tagsRaw = meta.tags || ''
  const tags = tagsRaw.replace(/[\[\]]/g, '').trim()

  const extra: Record<string, string> = {}
  for (const k of Object.keys(meta)) if (!MANAGED_KEYS.has(k)) extra[k] = rawFields[k]

  return {
    name: meta.name || name,
    category,
    description: meta.description || '',
    triggers: meta.triggers || '',
    tags,
    always: meta.always === 'true',
    body,
    extra,
  }
}

export default function SkillForm({ data, onChange, hideIdentity, allowRaw = true }: SkillFormProps) {
  const [rawMode, setRawMode] = useState(false)

  const set = <K extends keyof SkillFormData>(key: K, value: SkillFormData[K]) =>
    onChange({ ...data, [key]: value })

  const switchToRaw = () => {
    const assembled = assembleSkillContent({ ...data, raw: undefined })
    onChange({ ...data, raw: assembled })
    setRawMode(true)
  }

  const switchToStructured = () => {
    if (data.raw !== undefined) {
      const parsed = parseSkillContent(data.raw, data.category ? `${data.category}/${data.name}` : data.name)
      onChange({ ...parsed, raw: undefined })
    }
    setRawMode(false)
  }

  if (rawMode) {
    return (
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <span className="text-[12px] text-muted font-mono">{i18nT('components.skillForm.raw_yaml_markdown')}</span>
          <button className="text-[12px] text-accent hover:text-accent-hover cursor-pointer transition-colors" onClick={switchToStructured}>{i18nT('components.skillForm.switch_to_structured_editor')}</button>
        </div>
        <textarea
          aria-label={i18nT('components.skillForm.raw_yaml_and_markdown')}
          className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text font-mono text-[13px] outline-none resize-y leading-normal transition-colors focus-ring"
          rows={20}
          value={data.raw || ''}
          onChange={e => onChange({ ...data, raw: e.target.value })}
        />
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      {allowRaw && (
        <div className="flex justify-end">
          <button className="text-[12px] text-accent hover:text-accent-hover cursor-pointer transition-colors" onClick={switchToRaw}>{i18nT('components.skillForm.edit_raw_markdown')}</button>
        </div>
      )}
      {!hideIdentity && <>
        <div>
          {/* label-has-for can't resolve the control through the custom <Input>
              component; the runtime association via htmlFor + id + aria-label is correct. */}
          {/* eslint-disable-next-line jsx-a11y/label-has-for */}
          <label htmlFor="skill-name" className="text-[13px] font-semibold text-text mb-1 block">{i18nT('components.skillForm.name')}</label>
          <Input id="skill-name" aria-label={i18nT('components.skillForm.name')} placeholder={i18nT('components.skillForm.e_g_my_tool')} value={data.name} onChange={e => set('name', e.target.value)} className="w-full" />
        </div>
        <div>
          {/* eslint-disable-next-line jsx-a11y/label-has-for -- control resolved at runtime via htmlFor + id */}
          <label htmlFor="skill-category" className="text-[13px] font-semibold text-text mb-1 block">{i18nT('components.skillForm.category')} <span className="text-muted font-normal">{i18nT('components.skillForm.optional')}</span></label>
          <Input id="skill-category" aria-label={i18nT('components.skillForm.category')} placeholder={i18nT('components.skillForm.e_g_utils_code')} value={data.category} onChange={e => set('category', e.target.value)} className="w-full" />
          <div className="text-[11px] text-muted mt-1">{i18nT('components.skillForm.groups_the_skill_in_the_list_leave_empty_for_the')}</div>
        </div>
      </>}
      <div>
        <label htmlFor="skill-description" className="text-[13px] font-semibold text-text mb-1 block">
          <span className="block mb-1">{i18nT('components.skillForm.description')}</span>
          <textarea id="skill-description" aria-label={i18nT('components.skillForm.description')} className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none resize-y leading-relaxed transition-colors focus-ring" rows={3} placeholder={i18nT('components.skillForm.what_this_skill_does_and_when_the_agent_should_u')} value={data.description} onChange={e => set('description', e.target.value)} />
        </label>
      </div>
      <div>
        {/* label-has-for can't resolve the control through the custom <Input>
            component; the runtime association via htmlFor + id + aria-label is correct. */}
        {/* eslint-disable-next-line jsx-a11y/label-has-for */}
        <label htmlFor="skill-triggers" className="text-[13px] font-semibold text-text mb-1 block">{i18nT('components.skillForm.triggers')}</label>
        <Input id="skill-triggers" aria-label={i18nT('components.skillForm.triggers')} placeholder={i18nT('components.skillForm.keyword1_keyword2_keyword3')} value={data.triggers} onChange={e => set('triggers', e.target.value)} className="w-full" />
        <div className="text-[11px] text-muted mt-1">{i18nT('components.skillForm.comma_separated_keywords_that_activate_this_skil')}</div>
      </div>
      <div>
        {/* eslint-disable-next-line jsx-a11y/label-has-for -- control resolved at runtime via htmlFor + id */}
        <label htmlFor="skill-tags" className="text-[13px] font-semibold text-text mb-1 block">{i18nT('components.skillForm.tags')} <span className="text-muted font-normal">{i18nT('components.skillForm.optional')}</span></label>
        <Input id="skill-tags" aria-label={i18nT('components.skillForm.tags')} placeholder={i18nT('components.skillForm.skill_tool_aws')} value={data.tags} onChange={e => set('tags', e.target.value)} className="w-full" />
        <div className="text-[11px] text-muted mt-1">{i18nT('components.skillForm.comma_separated_labels_for_categorization_metada')}</div>
      </div>
      <div className="flex items-center gap-2">
        <label htmlFor="skill-always" className="flex items-center gap-2 text-[13px] text-text cursor-pointer">
          <input type="checkbox" id="skill-always" aria-label={i18nT('components.skillForm.always_loaded')} checked={data.always} onChange={e => set('always', e.target.checked)} className="accent-accent" />
          <span>{i18nT('components.skillForm.always_loaded')} <span className="text-muted">{i18nT('components.skillForm.inject_full_content_into_every_session')}</span></span>
        </label>
      </div>
      <div>
        <label htmlFor="skill-instructions" className="text-[13px] font-semibold text-text mb-1 block">
          <span className="block mb-1">{i18nT('components.skillForm.instructions')}</span>
          <textarea id="skill-instructions" aria-label={i18nT('components.skillForm.instructions')} className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text font-mono text-[13px] outline-none resize-y leading-normal transition-colors focus-ring" rows={10} placeholder={i18nT('components.skillForm.my_skill_step_by_step_instructions_for_the_agent')} value={data.body} onChange={e => set('body', e.target.value)} />
        </label>
      </div>
    </div>
  )
}
