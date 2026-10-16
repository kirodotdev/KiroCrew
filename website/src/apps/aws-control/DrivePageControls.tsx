/**
 * AWS Control drive - the controls more than one pane of the drive page draws.
 *
 * The `</>` CLI drawer under a listing, the grid/list toggle together with the
 * per-section choice it persists (`useViewMode`), and the inline confirm strip a
 * grid tile or a list row opens for a destructive action (`TileConfirm`). The
 * Library and Files panes both render these, so each is defined once, here.
 * `DrivePage.tsx` re-exports `TileConfirm` for `AwsControlPage.tsx`, which also
 * renders it, as an account row's remove-account confirm.
 */
import { useState } from 'react'
import { ChevronDown, Trash2, Code, LayoutGrid, List } from 'lucide-react'
import { Btn } from '../../components/ui'
import SegmentedControl from '../../components/SegmentedControl'
import { usePersistedString } from '../../hooks/usePersistedString'
import { i18nT } from '../../i18n/t'
import { CopyBtn, AwsErrorNotice } from './shared'

/** A collapsible `</>` drawer: the bucket, a prefix, and a generic CLI line. */
export function CliDrawer({ bucket, prefix }: { bucket: string; prefix: string }) {
  const [open, setOpen] = useState(false)
  const line = `aws s3 ls s3://${bucket}/${prefix}`
  return (
    <div className="mt-2" data-testid="cli-drawer">
      <button
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-text cursor-pointer bg-transparent border-none p-0"
        aria-expanded={open}
        data-testid="cli-drawer-toggle"
      >
        <Code size={12} />
        {i18nT('apps.awsControl.console.cli_drawer_label')}
        <ChevronDown size={12} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="mt-1.5 rounded-md border border-border bg-bg-elevated p-2.5 text-[12px]" data-testid="cli-drawer-body">
          <div className="text-muted mb-1">
            {i18nT('apps.awsControl.console.cli_drawer_hint', { bucket, prefix })}
          </div>
          <div className="flex items-center gap-2">
            <code className="flex-1 min-w-0 break-all rounded bg-bg px-2 py-1.5 font-mono text-[12px] text-text">
              {line}
            </code>
            <CopyBtn text={line} />
          </div>
        </div>
      )}
    </div>
  )
}

/**
 * How a listing is drawn: as thumbnail cards, or as table rows.
 *
 * Persisted PER SECTION, with a different default for each, because the two
 * folders hold different things. Library holds artifacts that have a real
 * rendered preview, so a grid of thumbnails is what makes it readable at a
 * glance. Files holds arbitrary uploads with no preview but with a size, a type
 * and a modified time worth comparing down a column, so it opens as a table --
 * the same split a file manager makes between a photo folder and a documents
 * folder. Once a reader chooses, that choice is remembered for that section.
 */
type ViewMode = 'grid' | 'list'

/* Literal keys, not an interpolated one. Same discipline as the panes' catalog-key maps
 * (`KIND_LABEL_KEY`, `START_ERROR_KEYS`): a key assembled at the call site is invisible to any tool that greps for
 * it, and the i18n added-lines gate reads a template literal in this position as a
 * built string rather than a constant. Spelling the three out costs two lines. */
const VIEW_MODE_STORAGE_KEY = {
  drive: 'awsControl.drive.viewMode.drive',
  library: 'awsControl.drive.viewMode.library',
} as const

export function useViewMode(section: keyof typeof VIEW_MODE_STORAGE_KEY, fallback: ViewMode): readonly [ViewMode, (v: ViewMode) => void] {
  const [raw, setRaw] = usePersistedString(VIEW_MODE_STORAGE_KEY[section], fallback)
  // Anything other than the two known words reads as the section's own default
  // rather than rendering nothing: localStorage is writable by hand and survives
  // a rename of these values, so an unknown string must not be able to blank a
  // folder the reader can no longer get back.
  const mode: ViewMode = raw === 'list' ? 'list' : raw === 'grid' ? 'grid' : fallback
  return [mode, (v: ViewMode) => setRaw(v)] as const
}

/**
 * The grid/list pair.
 *
 * This is `SegmentedControl`, not a hand-rolled pair of buttons: the Artifacts
 * gallery already drives the IDENTICAL grid-vs-table choice through it, and a
 * second spelling of one control is how the two drift apart. `collapse={false}`
 * because this sits in a content-hugging header group rather than a measured
 * column, which is the same reason the gallery passes it. Each section owns its
 * own `layoutId` -- the indicator is a framer shared-layout animation, and two
 * live controls sharing one id fight over it.
 *
 * `iconOnly`, where the gallery shows labels: this toggle shares a pane header
 * with two more actions (New folder, Upload) that the gallery's toolbar does
 * not carry, and the header sits in a pane the rail has already narrowed. Two
 * labelled segments plus two labelled buttons wrap to a second toolbar row at
 * ordinary desktop widths; the glyphs are the same ones the gallery draws, so
 * the control is recognised from there rather than re-learned.
 */
export function ViewModeToggle({ section, mode, onChange }: {
  section: keyof typeof VIEW_MODE_STORAGE_KEY
  mode: ViewMode
  onChange: (v: ViewMode) => void
}) {
  return (
    <SegmentedControl<ViewMode>
      segments={[
        { key: 'grid', label: i18nT('apps.awsControl.console.view_grid'), icon: <LayoutGrid size={13} /> },
        { key: 'list', label: i18nT('apps.awsControl.console.view_list'), icon: <List size={13} /> },
      ]}
      value={mode}
      onChange={onChange}
      layoutId={`aws-drive-view-${section}`}
      collapse={false}
      iconOnly
    />
  )
}

/**
 * The confirm for one grid tile, rendered by that tile.
 *
 * Not one strip above the grid: that names a single item while sitting next to
 * every other one -- the same trap the table rows avoid by making the confirm
 * their own next row -- and in a scrolled folder it paints off-screen, so the
 * menu click reads as a no-op while a live destructive control sits parked out
 * of sight.
 */
export function TileConfirm({ label, error, errorSource, askAgent, pending, onCancel, onConfirm, action, testId = 'drive-grid-confirm' }: {
  /* A NODE, not a string: the library's removal names the cloud folder it will
     empty, and a bucket path belongs in a <code> chip inside the sentence rather
     than flattened into it. Every existing caller passes a string, which is a
     ReactNode. */
  label: React.ReactNode
  error: string
  /** The value the failed request rejected with, so the notice can carry it to the agent. */
  errorSource?: unknown
  /** Whether the failure notice offers the hand-off. The CALLER knows whether
      anything typed is on screen around this strip; the strip does not. */
  askAgent: boolean
  pending: boolean
  onCancel: () => void
  onConfirm: () => void
  action: string
  /* The base for this strip's test ids, so a second caller is addressable as
     itself. Defaults to the drive's, which keeps every existing id unmoved. */
  testId?: string
}) {
  return (
    <div className="mt-1 w-full border-t border-border pt-2" data-testid={testId}>
      <p className="mb-2 text-[12px] leading-snug text-text">{label}</p>
      <AwsErrorNotice askAgent={askAgent} error={errorSource} message={error} variant="inline" className="mb-2" testId={`${testId}-error`} />
      <div className="flex flex-wrap items-center gap-2">
        {/* Disabled while the request runs, and that is a correctness rule rather
            than polish: this strip is the ONLY place the outcome can render, so
            dismissing it mid-flight throws away the answer to a destructive
            request that is still in progress -- the reader is told nothing, and a
            failure that left the copy in place looks identical to a success. */}
        <Btn onClick={onCancel} disabled={pending} data-testid={`${testId}-cancel`}>
          {i18nT('apps.awsControl.console.cancel')}
        </Btn>
        <Btn danger disabled={pending} onClick={onConfirm} data-testid={`${testId}-action`}>
          <Trash2 size={13} />{action}
        </Btn>
      </div>
    </div>
  )
}
