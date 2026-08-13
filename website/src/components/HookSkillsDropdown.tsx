/**
 * Dropdown panel for HookSkillsSelect — renders inside a portal.
 * Separated for coverage isolation (createPortal doesn't render in happy-dom).
 */
import { Brain, Minus } from 'lucide-react'
import { createPortal } from 'react-dom'
import { Input } from './ui'
import { i18nT } from '../i18n/t'

interface CatalogSkill {
  key: string
  name: string
  description?: string
}

interface Props {
  anchorRef: React.RefObject<HTMLElement>
  dropdownRef: React.Ref<HTMLDivElement>
  inputRef: React.Ref<HTMLInputElement>
  filter: string
  setFilter: (v: string) => void
  onClose: () => void
  selected: string[]
  filtered: CatalogSkill[]
  byKey: Map<string, CatalogSkill>
  onAdd: (key: string) => void
  onRemove: (key: string) => void
}

export default function HookSkillsDropdown({
  anchorRef, dropdownRef, inputRef, filter, setFilter,
  onClose, selected, filtered, byKey, onAdd, onRemove,
}: Props) {
  if (!anchorRef.current) return null
  return createPortal(
    <div
      ref={dropdownRef}
      className="fixed z-50 bg-bg-elevated border border-border rounded-lg shadow-xl p-1 w-72 max-h-60 overflow-y-auto"
      style={{
        top: anchorRef.current.getBoundingClientRect().bottom + 4,
        left: anchorRef.current.getBoundingClientRect().left,
      }}
      onKeyDown={e => { if (e.key === 'Escape') { onClose(); anchorRef.current?.focus() } }}
    >
      <Input
        ref={inputRef}
        placeholder={i18nT('components.skillsMultiSelect.filter_skills')}
        value={filter}
        onChange={e => setFilter(e.target.value)}
        className="mb-1 text-[12px]"
        autoFocus
      />
      {selected.length > 0 && (
        <>
          <p className="text-[11px] text-muted px-2 pt-1 pb-0.5 font-medium uppercase tracking-wide">{i18nT('components.skillsMultiSelect.selected')}</p>
          {selected.map(key => {
            const skill = byKey.get(key)
            return (
              <button
                key={key}
                className="w-full text-left px-2 py-1.5 rounded text-[12px] hover:bg-danger-subtle transition-colors flex items-center gap-2"
                onClick={() => onRemove(key)}
                aria-label={i18nT('components.skillsMultiSelect.remove_skill', { name: skill?.name || key })}
              >
                <Minus className="lucide-inline shrink-0 text-danger" />
                <span className="flex flex-col min-w-0">
                  <span className="font-medium truncate">{skill?.name || key.split('/').pop()}</span>
                  <span className="text-muted text-[11px] font-mono truncate">{key}</span>
                </span>
              </button>
            )
          })}
          {filtered.length > 0 && <hr className="my-1 border-border" />}
        </>
      )}
      {filtered.length === 0 && selected.length === 0 && (
        <p className="text-[12px] text-muted px-2 py-1">{i18nT('components.skillsMultiSelect.no_matching_skills')}</p>
      )}
      {filtered.map(s => (
        <button
          key={s.key}
          className="w-full text-left px-2 py-1.5 rounded text-[12px] hover:bg-accent-subtle transition-colors flex items-center gap-2"
          onClick={() => onAdd(s.key)}
        >
          <Brain className="lucide-inline shrink-0 text-accent" />
          <span className="flex flex-col min-w-0">
            <span className="font-medium truncate">{s.name}</span>
            <span className="text-muted text-[11px] font-mono truncate">{s.key}</span>
          </span>
        </button>
      ))}
    </div>,
    document.body,
  )
}
