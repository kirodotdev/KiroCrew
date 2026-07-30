import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, EyeOff, ShieldAlert } from 'lucide-react'
import { knowledgeApi } from './api'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'

/** One rule group from GET /api/knowledge/exclusions.
 *
 * `entries` is always present (possibly empty); `kind` says how to present it.
 * The backend deliberately sends no labels -- ids are the contract, so exclusion
 * copy stays translatable here. */
export interface ExclusionRule {
  id: string
  kind: string
  entries: string[]
  value?: number
  accepts_no_extension?: boolean
  entries_by_source_type?: Record<string, string[]>
}

interface ExclusionsResponse {
  rules: ExclusionRule[]
}

/** Presentation for each rule id. Unknown ids still render (with the id as a
 * fallback heading) so a backend that grows a rule group is never silently
 * dropped from the UI. */
function ruleCopy(rule: ExclusionRule): { title: string; detail: string } {
  switch (rule.id) {
    case 'hidden_dirs':
      return {
        title: i18nT('pages.knowledge.exclusionsDisclosure.hidden_folders'),
        detail: i18nT('pages.knowledge.exclusionsDisclosure.any_folder_whose_name_starts_with_a_dot_is_skipp'),
      }
    case 'hard_skip_dirs':
      return {
        // Not "dependency and build folders": this group is derived from
        // HARD_SKIP_DIRS, which also carries .git and .venv/venv. Naming it
        // after only part of its contents made the heading contradict its own
        // chips.
        title: i18nT('pages.knowledge.exclusionsDisclosure.never_scanned_folders'),
        detail: i18nT('pages.knowledge.exclusionsDisclosure.dependency_build_and_version_control_directories'),
      }
    case 'source_type_dirs':
      return {
        title: i18nT('pages.knowledge.exclusionsDisclosure.vault_metadata'),
        detail: i18nT('pages.knowledge.exclusionsDisclosure.skipped_for_specific_source_types_only'),
      }
    case 'junk_files':
      return {
        title: i18nT('pages.knowledge.exclusionsDisclosure.system_and_temporary_files'),
        detail: i18nT('pages.knowledge.exclusionsDisclosure.matched_against_the_filename_case_insensitively'),
      }
    case 'extension_allowlist':
      return {
        title: i18nT('pages.knowledge.exclusionsDisclosure.only_these_file_types_are_read'),
        detail: i18nT('pages.knowledge.exclusionsDisclosure.anything_else_in_the_folder_is_ignored_including'),
      }
    case 'sensitive_paths':
      return {
        title: i18nT('pages.knowledge.exclusionsDisclosure.credential_paths'),
        detail: i18nT('pages.knowledge.exclusionsDisclosure.files_resolving_inside_protected_locations_such'),
      }
    case 'file_cap':
      return {
        title: i18nT('pages.knowledge.exclusionsDisclosure.file_limit'),
        detail: i18nT('pages.knowledge.exclusionsDisclosure.the_newest_files_up_to_this_limit_are_indexed_ol'),
      }
    default:
      return { title: rule.id, detail: '' }
  }
}

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span className="text-[10px] px-1.5 py-0.5 rounded bg-bg border border-border text-muted font-mono whitespace-nowrap">
      {children}
    </span>
  )
}

function RuleGroup({ rule }: { rule: ExclusionRule }) {
  const { title, detail } = ruleCopy(rule)
  const byType = rule.entries_by_source_type ?? {}
  return (
    <div className="space-y-1">
      <div className="text-[12px] text-text-strong">{title}</div>
      {detail && <div className="text-[11px] text-muted">{detail}</div>}

      {rule.entries.length > 0 && (
        <div className="flex gap-1 flex-wrap">
          {rule.entries.map(e => <Chip key={e}>{e}</Chip>)}
        </div>
      )}

      {Object.entries(byType).map(([sourceType, dirs]) => (
        <div key={sourceType} className="flex gap-1 flex-wrap items-center">
          <span className="text-[10px] text-muted/70">{sourceType}</span>
          {dirs.map(d => <Chip key={d}>{d}</Chip>)}
        </div>
      ))}

      {rule.kind === 'limit' && rule.value !== undefined && (
        <div className="flex gap-1 flex-wrap"><Chip>{fmtNumber(rule.value)} {i18nT('pages.knowledge.exclusionsDisclosure.files')}</Chip></div>
      )}

      {rule.id === 'extension_allowlist' && rule.accepts_no_extension && (
        <div className="text-[11px] text-muted">
          {i18nT('pages.knowledge.exclusionsDisclosure.files_with_no_extension_at_all_are_also_read_as')}
        </div>
      )}
    </div>
  )
}

/** Read-only disclosure of what folder scanning skips by default.
 *
 * Folder ingestion drops a lot silently -- hidden folders, dependency trees, OS
 * junk, anything outside the extension allowlist, and everything past the file
 * cap. Before this panel the only symptom was a file count that didn't match the
 * folder, with no way to find out why. Purely informational: none of it is
 * configurable here, and the component never sends anything. */
export default function ExclusionsDisclosure() {
  const [open, setOpen] = useState(false)
  const { data, isError } = useQuery({
    queryKey: ['knowledge-exclusions'],
    queryFn: () => knowledgeApi<ExclusionsResponse>('/exclusions'),
    staleTime: 5 * 60_000,
  })

  // An older gateway has no /exclusions route. Render nothing rather than an
  // error: this is supplementary information, not part of the add flow.
  if (isError || !data?.rules?.length) return null

  return (
    <div className="border-t border-border pt-3">
      <button type="button" onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="flex items-center gap-1.5 text-[12px] text-muted hover:text-text">
        {open ? <ChevronDown size={12} className="lucide-inline" /> : <ChevronRight size={12} className="lucide-inline" />}
        <EyeOff size={12} className="lucide-inline" />
        {i18nT('pages.knowledge.exclusionsDisclosure.what_gets_skipped_automatically')}
      </button>

      {open && (
        <div className="mt-2 space-y-3 pl-4">
          <div className="text-[11px] text-muted flex items-start gap-1.5">
            <ShieldAlert size={12} className="lucide-inline mt-0.5 shrink-0" />
            <span>{i18nT('pages.knowledge.exclusionsDisclosure.these_rules_always_apply_and_are_not_configurabl')}</span>
          </div>
          {data.rules.map(rule => <RuleGroup key={rule.id} rule={rule} />)}
        </div>
      )}
    </div>
  )
}
