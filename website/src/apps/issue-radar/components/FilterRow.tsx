/** A single toggle row in the Filters section (Open / Closed / Requested by
 * me / Assigned to me). Darkens when active; disabled when it needs a signed-in
 * user we don't have. */
export default function FilterRow({
  label, active, disabled, onToggle, disabledHint,
}: {
  label: string
  active: boolean
  disabled?: boolean
  onToggle: () => void
  /** Tooltip shown when disabled. Defaults to the gh-CLI sign-in hint used by
   * the "requested/assigned to me" rows. */
  disabledHint?: string
}) {
  return (
    <button
      onClick={onToggle}
      disabled={disabled}
      title={disabled ? (disabledHint ?? 'Sign in with the gh CLI to use this filter') : label}
      className={`w-full flex items-center px-2 py-1.5 rounded-md text-[13px] text-left cursor-pointer transition-colors disabled:opacity-40 disabled:cursor-default ${active ? 'bg-accent-subtle text-text font-medium' : 'text-muted hover:bg-bg-hover'}`}
    >
      {label}
    </button>
  )
}
