/**
 * Notes — a markdown notebook backed by a git repository.
 *
 * Single-note view, no tabs: the left panel is the only navigation surface.
 * Search swaps the tree for flat ranked results; clearing restores the tree.
 *
 * This file is the route's import path and renders the page. Its state, effects
 * and actions live in `useMdNotebookPageState.ts`, which the component calls as
 * its only hook. New state or behaviour goes there (or in a sibling
 * `useMdNotebookPage<Part>.ts` it calls); new markup goes here.
 */
import { motion } from 'framer-motion'
import type { CSSProperties } from 'react'
import {
  Check,
  ChevronDown,
  Code,
  FileText,
  ListFilter,
  Plus,
  RefreshCw,
} from 'lucide-react'
// Same collapse glyphs the sessions sidebar uses (SessionsSidebarChrome), so the
// Notes panel's hide/show control reads as the identical affordance.
import { PanelLeftLight, PanelLeftSolid } from '../../components/icons/panels'
import { Trans } from 'react-i18next'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
import {
  ACCENT,
  COLUMN_MAX_WIDTH,
  COLUMN_PAD_X,
  DOC_BODY_LINE_HEIGHT,
  DOC_BODY_PX,
  DOC_H1_PX,
  DOC_HEADING_WEIGHTS,
  FONT_BODY,
  FONT_MONO,
  LS,
  MENU_SECTION_LABEL,
  RAIL_TYPE,
  SORTS,
} from './constants'
import { MDNB_CSS } from './styles'
import { listViewLabel, paneViewLabel, sortLabel, syncedAgoLabel } from './labels'
import { InlineTitle } from './BlockEditor'
import { Preview } from './Preview'
import { ConnectVault } from './ConnectVault'
import { ConfirmDialog } from './ConfirmDialog'
import { InlineLink } from './bits'
import { SettingsBar, SettingsPage } from './SettingsPage'
import Clickable from '../../components/Clickable'
import ReadingWidthToggle from '../../components/ReadingWidthToggle'
import { NoteRow, orderNotes, renderTree } from './NoteRow'
import {
  agoBucket,
  noteDirPath,
  savePref,
  shiftListItem,
  vaultContentPath,
} from './utils'
import { useMdNotebookPageState } from './useMdNotebookPageState'

const iconBtn: CSSProperties = {
  width: '28px',
  height: '28px',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  borderRadius: '8px',
  background: 'transparent',
  border: 'none',
  color: 'var(--muted)',
  cursor: 'pointer',
  flexShrink: 0,
}

export default function MdNotebookPage() {
  const {
    vaults, setVaults, notes, activePath, content, setContent, backlinks, setDirty, query, setQuery,
    results, collapsed, syncing, error, editBlock, showConnect, setShowConnect, vaultSelOpen,
    setVaultSelOpen, sortOpen, setSortOpen, fileConflict, setFileConflict, settingsOpen,
    setSettingsOpen, auth, confirmDelete, setConfirmDelete, mode, sortKey, setSortKey, view,
    setView, isMobile, ime, setNarrowPanelOpen, panelShown, panelW, activeVaultId, autoSync,
    autoSyncMins, autoCommit, syncShortcut, fullWidth, recordingShortcutRef, setAutoSyncPref,
    setAutoSyncMinsPref, toggleFullWidth, columnMaxWidth, headerControls, headerBandWidth,
    headerControlsRef, headerBandRef, titleClearance, stackTitle, setSyncShortcutPref,
    setAutoCommitPref, trashMsg, setTrashMsg, openTrash, lastSyncByVault, contentRef, mtimeRef,
    dirtyRef, staleBackend, settingsError, flushSave, openNote, edit, markDirty, startBlockEdit,
    cancelBlockEdit, commitBlockEdit, splitBlockEdit, toggleCheckbox, switchVault, newNote,
    isPinned, tree, moveNote, renameNote, removeNote, forgetVault, savePat, setVaultKnowledge,
    runSync, noteActions, toggle, togglePanel, startResize, switchMode,
  } = useMdNotebookPageState()

  // ---- render -----------------------------------------------------------

  if (vaults === null) {
    return (
      <div
        style={{
          padding: '24px',
          fontSize: '12px',
          fontFamily: FONT_BODY,
          color: error ? 'var(--danger)' : 'var(--muted)',
        }}
      >
        {error ? i18nT('apps.mdNotebook.boot.unreachable', { message: error }) : i18nT('apps.mdNotebook.boot.loading')}
      </div>
    )
  }

  if (!vaults.length || showConnect) {
    return (
      <ConnectVault
        onCancel={vaults.length ? () => setShowConnect(false) : null}
        onConnected={v => {
          setVaults(prev => [...(prev ?? []), v])
          setShowConnect(false)
          void switchVault(v.id)
        }}
      />
    )
  }

  const activeVault = vaults.find(v => v.id === activeVaultId) ?? vaults[0]
  // Directory of the open note, which is what resolves its relative image
  // sources. Derived here rather than in `Preview` because the vault's local
  // path lives in this page's state, not in the note body.
  const noteDir = noteDirPath(activeVault, activePath)
  // Root of the vault's content, which a root-relative image (`/images/a.png`)
  // falls back to when it is not a real absolute path.
  const vaultRoot = activeVault ? vaultContentPath(activeVault) : undefined
  // `pending` means "differs from the last commit", which is only actionable when
  // there is a remote the note has not reached yet. On a local-only vault it has
  // no destination, clears itself on the next autosave, and reads as "not saved"
  // — so the row shows nothing.
  const showSyncBadge = !activeVault?.localOnly
  const lastSync = activeVaultId ? (lastSyncByVault[activeVaultId] ?? null) : null
  const ago = lastSync ? agoBucket(lastSync) : null
  // A local-only vault has no remote, so the button commits to local git history
  // and nothing else — label it for what it does, and drop the "Synced N ago"
  // reading, which would claim a backup that never happened.
  const syncLabel = syncing
    ? i18nT('apps.mdNotebook.sync.syncing')
    : activeVault?.localOnly
      ? i18nT('apps.mdNotebook.sync.localAction')
      : ago === null
        ? i18nT('apps.mdNotebook.sync.action')
        : syncedAgoLabel(ago.unit, ago.value)

  return (
    <div
      style={{
        display: 'flex',
        height: '100%',
        minHeight: '520px',
        position: 'relative',
        fontFamily: FONT_BODY,
        color: 'var(--text)',
        background: 'var(--bg)',
      }}
    >
      <style>{MDNB_CSS}</style>

      {confirmDelete && (
        <ConfirmDialog
          title={i18nT('apps.mdNotebook.row.deleteTitle', { title: confirmDelete.title })}
          // `<Trans>` rather than a plain string: `.trash` inside the sentence is a
          // link that opens the folder, and the app hides that folder from its own
          // listing, so this is the user's only way in at the moment they need it.
          // One catalog key with a `<trash>` tag keeps the sentence whole for
          // translators instead of splitting it around the link.
          body={
            <Trans
              i18nKey="apps.mdNotebook.row.deleteConfirm"
              components={{
                trash: (
                  <InlineLink
                    onClick={() => void openTrash()}
                    title={i18nT('apps.mdNotebook.trash.open')}
                  />
                ),
              }}
            />
          }
          // Only messages live here now: the two cases where clicking the link
          // produces nothing visible (no folder yet, no file manager on the host).
          footer={
            trashMsg && (
              <div style={{ fontSize: '11px', color: 'var(--muted)' }}>{trashMsg}</div>
            )
          }
          confirmLabel={i18nT('apps.mdNotebook.row.deleteAction')}
          cancelLabel={i18nT('apps.mdNotebook.connect.cancel')}
          onCancel={() => {
            setTrashMsg(null)
            setConfirmDelete(null)
          }}
          onConfirm={() => {
            const target = confirmDelete.path
            setTrashMsg(null)
            setConfirmDelete(null)
            void removeNote(target)
          }}
        />
      )}

      {/* Floating panel toggle: stays put when the panel hides. */}
      <button
        type="button"
        className="pi-morph mdnb-collapse"
        onClick={togglePanel}
        aria-label={
          panelShown
            ? i18nT('apps.mdNotebook.panel.hide')
            : i18nT('apps.mdNotebook.panel.show')
        }
        style={{
          position: 'absolute',
          // Same rect as the sessions sidebar's floating collapse button
          // (SessionsSidebarChrome: top-[9px] left-2 w-7 h-7): a 28px control whose
          // center lands on the 23px shared control baseline, so this button, the
          // sessions toggle, and the left-nav collapse arrow all line up.
          top: '9px',
          left: '8px',
          zIndex: 10,
          width: '28px',
          height: '28px',
          borderRadius: '8px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          cursor: 'pointer',
          // No inline `background`: `.mdnb-collapse` (styles.ts) owns rest +
          // hover painting. An inline `background: transparent` would outrank
          // the `:hover` rule (inline beats author stylesheet), leaving the
          // hover tint dead — the sessions toggle gets its tint from a
          // stylesheet class for the same reason.
          border: 'none',
          transition: 'color .15s, background .15s',
        }}
      >
        {panelShown ? <PanelLeftLight size={16} /> : <PanelLeftSolid size={16} />}
      </button>

      {panelShown && (
        <div
          style={{
            width: isMobile ? '100%' : `${panelW}px`,
            flexShrink: 0,
            position: 'relative',
            display: 'flex',
            flexDirection: 'column',
            // Top edge flush with the pane, mirroring the dashboard's left-nav
            // card (`mt-0 mb-2`): both cards sit in the same grid row, so a top
            // margin here would make this panel start lower than the nav beside it.
            margin: '0 0 8px',
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border)',
            borderRadius: '16px',
            boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
          }}
        >
          {/* Header: the vault selector doubles as the panel title. Mirrors the
              sessions sidebar header (mt-0.5 h-10 → 40px row, its content on the
              23px baseline); the vault trigger starts at 36px to clear the
              floating collapse button (left 8px + 28px), same as the sidebar's
              pl-9 content inset. */}
          <div
            style={{
              height: '40px',
              marginTop: '2px',
              display: 'flex',
              alignItems: 'center',
              padding: '0 14px 0 8px',
              flexShrink: 0,
              position: 'relative',
            }}
          >
            <button
              type="button"
              className="mdnb-vault-trigger"
              onClick={() => setVaultSelOpen(o => !o)}
              aria-expanded={vaultSelOpen}
              aria-haspopup="listbox"
              aria-label={i18nT('apps.mdNotebook.panel.switchVault')}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '4px',
                marginLeft: '28px',
                minWidth: 0,
                background: 'transparent',
                border: 'none',
                padding: '4px 6px',
                borderRadius: '8px',
                cursor: 'pointer',
                fontFamily: FONT_BODY,
              }}
            >
              <span
                style={{
                  ...RAIL_TYPE.panelTitle,
                  fontWeight: 500,
                  color: 'var(--muted)',
                  letterSpacing: '.04em',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {activeVault?.name ?? i18nT('apps.mdNotebook.title')}
              </span>
              <ChevronDown
                size={13}
                style={{
                  flexShrink: 0,
                  color: 'var(--muted)',
                  transform: vaultSelOpen ? 'rotate(180deg)' : 'none',
                  transition: 'transform .15s',
                }}
              />
            </button>
            {/* New note, at the top level of the vault — outside every folder.
                It lives here rather than beside the document controls because
                creating a note is a navigation act on this list, and "outside
                all folders" only reads as a location next to the tree. */}
            <button
              type="button"
              onClick={() => void newNote()}
              aria-label={i18nT('apps.mdNotebook.panel.newRootNote')}
              title={i18nT('apps.mdNotebook.panel.newRootNote')}
              style={{ ...iconBtn, marginLeft: 'auto' }}
              className="mdnb-act"
            >
              <Plus size={16} />
            </button>
            {vaultSelOpen && (
              <div
                role="listbox"
                aria-label={i18nT('apps.mdNotebook.panel.vaults')}
                style={{
                  position: 'absolute',
                  top: '38px',
                  left: '38px',
                  minWidth: '180px',
                  maxWidth: 'calc(100% - 46px)',
                  maxHeight: '112px',
                  overflowY: 'auto',
                  background: 'var(--bg-elevated)',
                  border: '1px solid var(--border)',
                  borderRadius: '8px',
                  boxShadow: '0 4px 14px rgba(0,0,0,0.25)',
                  padding: '4px',
                  zIndex: 20,
                }}
              >
                {vaults.map(v => (
                  <div
                    key={v.id}
                    className="mdnb-row"
                    role="option"
                    aria-selected={v.id === activeVaultId}
                    tabIndex={0}
                    onKeyDown={e => {
                      if (e.key !== 'Enter' && e.key !== ' ') return
                      e.preventDefault()
                      void switchVault(v.id)
                    }}
                    onClick={() => void switchVault(v.id)}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '8px',
                      padding: '5px 8px',
                      borderRadius: '6px',
                      cursor: 'pointer',
                      ...RAIL_TYPE.row,
                      color: v.id === activeVaultId ? 'var(--text)' : 'var(--muted)',
                    }}
                  >
                    <span
                      style={{
                        flex: 1,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {v.name}
                    </span>
                    {v.id === activeVaultId && (
                      <Check size={14} style={{ flexShrink: 0, color: ACCENT }} />
                    )}
                  </div>
                ))}
                <div style={{ height: '1px', background: 'var(--border)', margin: '4px 0' }} />
                <Clickable
                  className="mdnb-row"
                  onClick={() => {
                    setVaultSelOpen(false)
                    setShowConnect(true)
                  }}
                  style={{
                    padding: '5px 8px',
                    borderRadius: '6px',
                    cursor: 'pointer',
                    ...RAIL_TYPE.row,
                    color: 'var(--muted)',
                  }}
                >
                  {i18nT('apps.mdNotebook.panel.connectVault')}
                </Clickable>
              </div>
            )}
          </div>

          {/* Search + sort */}
          <div
            style={{
              display: 'flex',
              gap: '6px',
              alignItems: 'center',
              padding: '0 12px 8px',
              flexShrink: 0,
            }}
          >
            <input
              className="mdnb-search"
              aria-label={i18nT('apps.mdNotebook.panel.searchPlaceholder')}
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder={i18nT('apps.mdNotebook.panel.searchPlaceholder')}
              style={{
                flex: 1,
                height: '30px',
                minWidth: 0,
                boxSizing: 'border-box',
                background: 'var(--card)',
                border: '1px solid var(--border)',
                borderRadius: '8px',
                padding: '0 10px',
                ...RAIL_TYPE.row,
                color: 'var(--text)',
                fontFamily: FONT_BODY,
              }}
            />
            <div style={{ position: 'relative', flexShrink: 0 }}>
              <button
                type="button"
                onClick={() => setSortOpen(o => !o)}
                aria-label={i18nT('apps.mdNotebook.panel.sortAndView')}
                style={{
                  ...iconBtn,
                  width: '30px',
                  height: '30px',
                  border: '1px solid var(--border)',
                }}
              >
                <ListFilter size={14} />
              </button>
              {sortOpen && (
                <div
                  style={{
                    position: 'absolute',
                    top: '34px',
                    right: 0,
                    minWidth: '200px',
                    background: 'var(--bg-elevated)',
                    border: '1px solid var(--border)',
                    borderRadius: '8px',
                    boxShadow: '0 4px 14px rgba(0,0,0,0.25)',
                    padding: '4px',
                    zIndex: 20,
                  }}
                >
                  <div
                    style={{
                      ...MENU_SECTION_LABEL,
                      textTransform: 'uppercase',
                      letterSpacing: '.04em',
                      color: 'var(--muted)',
                      padding: '6px 8px 4px',
                    }}
                  >
                    {i18nT('apps.mdNotebook.panel.viewSection')}
                  </div>
                  {(['folders', 'list'] as const).map(v => (
                    <Clickable
                      key={v}
                      className="mdnb-row"
                      aria-label={listViewLabel(v)}
                      onClick={() => {
                        setView(v)
                        savePref('mdnb-list-view', v)
                        setSortOpen(false)
                      }}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '8px',
                        padding: '5px 8px',
                        borderRadius: '6px',
                        cursor: 'pointer',
                        ...RAIL_TYPE.row,
                        color: view === v ? 'var(--text)' : 'var(--muted)',
                      }}
                    >
                      <span style={{ flex: 1 }}>
                        {listViewLabel(v)}
                      </span>
                      {view === v && <Check size={14} style={{ color: ACCENT }} />}
                    </Clickable>
                  ))}
                  <div style={{ height: '1px', background: 'var(--border)', margin: '4px 0' }} />
                  <div
                    style={{
                      ...MENU_SECTION_LABEL,
                      textTransform: 'uppercase',
                      letterSpacing: '.04em',
                      color: 'var(--muted)',
                      padding: '6px 8px 4px',
                    }}
                  >
                    {i18nT('apps.mdNotebook.panel.sortSection')}
                  </div>
                  {Object.keys(SORTS).map((key) => (
                    <Clickable
                      key={key}
                      className="mdnb-row"
                      aria-label={sortLabel(key)}
                      onClick={() => {
                        setSortKey(key)
                        savePref(LS.sort, key)
                        setSortOpen(false)
                      }}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '8px',
                        padding: '5px 8px',
                        borderRadius: '6px',
                        cursor: 'pointer',
                        ...RAIL_TYPE.row,
                        color: sortKey === key ? 'var(--text)' : 'var(--muted)',
                      }}
                    >
                      <span style={{ flex: 1, whiteSpace: 'nowrap' }}>
                        {sortLabel(key)}
                      </span>
                      {sortKey === key && <Check size={14} style={{ color: ACCENT }} />}
                    </Clickable>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Note list. Dropping on the background files a note at the root. */}
          {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions -- drag-drop only: this is the list's scroll background, which has no activation of its own, so a role and tab stop would add a focus stop that does nothing */}
          <div
            style={{ flex: 1, overflowY: 'auto', padding: '8px' }}
            onDragOver={e => {
              e.preventDefault()
              e.dataTransfer.dropEffect = 'move'
            }}
            onDrop={e => {
              e.preventDefault()
              const from = e.dataTransfer.getData('text/plain')
              if (from) moveNote(from, '')
            }}
          >
            {query.trim()
              ? results.length
                ? results.map(r => (
                    <NoteRow
                      key={r.path}
                      note={{
                        path: r.path,
                        title: r.title,
                        modifiedAt:
                          notes.find(n => n.path === r.path)?.modifiedAt ?? Date.now(),
                        syncStatus: 'synced',
                      }}
                      active={!settingsOpen && r.path === activePath}
                      onOpen={p => void openNote(p)}
                      actions={noteActions}
                    />
                  ))
                : (
                    <div style={{ padding: '10px', ...RAIL_TYPE.secondary, color: 'var(--muted)' }}>
                      {i18nT('apps.mdNotebook.panel.noMatches')}
                    </div>
                  )
              : view === 'list'
                ? orderNotes(notes, SORTS[sortKey].cmp, isPinned).map(n => (
                      <NoteRow
                        key={n.path}
                        note={n}
                        active={!settingsOpen && n.path === activePath}
                        onOpen={p => void openNote(p)}
                        showSyncBadge={showSyncBadge}
                        actions={noteActions}
                        showFolder
                      />
                    ))
                : renderTree(tree, 0, '', {
                    activePath: settingsOpen ? null : activePath,
                    onOpen: p => void openNote(p),
                    collapsed,
                    toggle,
                    showSyncBadge,
                    cmp: SORTS[sortKey].cmp,
                    actions: noteActions,
                  })}
          </div>

          <SettingsBar
            open={settingsOpen}
            onOpen={() => {
              setSettingsOpen(true)
              // Same gesture as picking a note: the drawer owns the pane while it
              // is open, so it has to step aside for the page it just asked for.
              if (isMobile) setNarrowPanelOpen(false)
            }}
          />

          {/* Drag handle */}
          <div
            onPointerDown={startResize}
            style={{
              position: 'absolute',
              top: 0,
              bottom: 0,
              right: '-3px',
              width: '5px',
              cursor: 'col-resize',
            }}
          />
        </div>
      )}

      {/* ---- main column: Settings page, or the open note ----
          While narrow the drawer is `width: 100%`, which leaves this flex
          sibling 0px wide -- and its header controls are `position: absolute`
          at `right: COLUMN_PAD_X`, so they were painted leftward OVER the
          drawer: at 390px the Commit / Save-locally pill landed on the first
          tree row, and the Settings close button did the same. The column is
          hidden rather than unmounted (the file-explorer drawer makes the same
          call) so the editor keeps its scroll, selection and pending-save
          state across a drawer round trip; `contents` puts both branches
          back in the row's flex flow when shown. */}
      <div style={{ display: isMobile && panelShown ? 'none' : 'contents' }}>
      {settingsOpen ? (
        <SettingsPage
          vaults={vaults}
          activeVaultId={activeVaultId}
          hasPat={auth.hasPat}
          hasGhAuth={auth.hasGhAuth}
          autoSync={autoSync}
          autoSyncMins={autoSyncMins}
          autoCommit={autoCommit}
          syncPrefsError={settingsError}
          shortcut={syncShortcut}
          onClose={() => setSettingsOpen(false)}
          onSwitchVault={id => {
            void switchVault(id)
            setSettingsOpen(false)
          }}
          onConnect={() => {
            setSettingsOpen(false)
            setShowConnect(true)
          }}
          onForget={forgetVault}
          onSetPat={savePat}
          onSetKnowledge={setVaultKnowledge}
          onAutoSync={setAutoSyncPref}
          onAutoSyncMins={setAutoSyncMinsPref}
          onAutoCommit={setAutoCommitPref}
          onSetShortcut={setSyncShortcutPref}
          onRecordingChange={on => {
            recordingShortcutRef.current = on
          }}
        />
      ) : (
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <div ref={headerBandRef} style={{ position: 'relative' }}>
          <div
            ref={headerControlsRef}
            style={{
              position: 'absolute',
              top: '24px',
              right: `${COLUMN_PAD_X}px`,
              zIndex: 2,
              display: 'flex',
              gap: '4px',
              alignItems: 'center',
              // The cluster's width is content-driven (the sync label varies by
              // locale and state), so on a pane too narrow to seat it in one
              // row it would poke past the left edge and `overflow-x-hidden`
              // would clip the view controls. Cap it to the measured band and
              // wrap instead: every control stays reachable, and the wrapped
              // height flows into useMdNotebookPageState's stacking arithmetic, which already
              // drops the title below whatever the cluster occupies.
              maxWidth: headerBandWidth
                ? `${Math.max(0, headerBandWidth - COLUMN_PAD_X * 2)}px`
                : undefined,
              flexWrap: 'wrap',
              justifyContent: 'flex-end',
            }}
          >
            {/* Rendered / raw switch */}
            <div
              style={{
                display: 'flex',
                gap: '4px',
                padding: '4px',
                background: 'var(--card)',
                borderRadius: '8px',
              }}
            >
              {(
                [
                  ['rendered', FileText] as const,
                  ['raw', Code] as const,
                ]
              ).map(([m, Icon]) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => switchMode(m)}
                  aria-label={paneViewLabel(m)}
                  title={paneViewLabel(m)}
                  style={{
                    width: '26px',
                    height: '22px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    border: 'none',
                    borderRadius: '6px',
                    cursor: 'pointer',
                    background: mode === m ? ACCENT : 'transparent',
                    color: mode === m ? 'var(--accent-fg)' : 'var(--muted)',
                  }}
                >
                  <Icon size={16} />
                </button>
              ))}
            </div>
            {/* Reading column / full width. A sibling of the rendered-raw switch
                because both answer "how do I want to read this note". The
                control itself is `ReadingWidthToggle`, the component an artifact
                already uses for this exact choice, so the two views cannot drift
                apart on label, pressed state or accent. Only the stored
                preference is this view's own (`LS.fullWidth`): a note and an
                artifact are different reading surfaces, often open at the same
                time in two panes, and one shared key would silently reflow the
                other. */}
            <ReadingWidthToggle value={fullWidth ? 'full' : 'md'} onToggle={toggleFullWidth} />
            <button
              type="button"
              onClick={() => void runSync()}
              disabled={syncing}
              title={
                activeVault?.localOnly
                  ? i18nT('apps.mdNotebook.sync.localTitle')
                  : undefined
              }
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                height: '28px',
                padding: '0 12px',
                borderRadius: '12px',
                border: 'none',
                background: ACCENT,
                color: 'var(--accent-fg)',
                fontSize: '11px',
                fontWeight: 500,
                cursor: syncing ? 'default' : 'pointer',
                fontFamily: FONT_BODY,
              }}
            >
              <RefreshCw size={13} />
              {syncLabel}
              {syncing && (
                <span aria-hidden>
                  {[0, 0.2, 0.4].map(delay => (
                    <motion.span
                      key={delay}
                      style={{ display: 'inline-block' }}
                      animate={{ opacity: [0.25, 1, 0.25] }}
                      transition={{
                        duration: 1.4,
                        times: [0, 0.4, 0.8],
                        repeat: Infinity,
                        ease: 'easeInOut',
                        delay,
                      }}
                    >
                      .
                    </motion.span>
                  ))}
                </span>
              )}
            </button>
          </div>

          <div
            style={{
              margin: '0 auto',
              width: '100%',
              maxWidth: columnMaxWidth,
              boxSizing: 'border-box',
              // Stacked: the title clears the cluster from below, so the top
              // pad carries the cluster's own height and the row is free.
              padding: stackTitle
                ? `${24 + headerControls.height + 8}px ${COLUMN_PAD_X}px 14px`
                : `24px ${COLUMN_PAD_X + titleClearance}px 14px ${COLUMN_PAD_X}px`,
            }}
          >
            {activePath ? (
              <InlineTitle
                path={activePath}
                onRename={n => renameNote(activePath, n)}
                mb="0"
              />
            ) : (
              <div
                style={{
                  // The placeholder alternates with `InlineTitle` in this exact
                  // slot, so it takes the same derived size: a literal here is
                  // the same drift, one branch away.
                  fontSize: `${DOC_H1_PX}px`,
                  fontWeight: DOC_HEADING_WEIGHTS[0],
                  color: 'var(--muted)',
                }}
              >
                {i18nT('apps.mdNotebook.title')}
              </div>
            )}
            {activePath && (
              <div
                title={`${activeVault?.name ?? ''}/${activePath}`}
                style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '2px' }}
              >
                {`${activeVault?.name ?? ''}/${
                  activePath.includes('/')
                    ? `${activePath.slice(0, activePath.lastIndexOf('/'))}/`
                    : ''
                }`}
              </div>
            )}
          </div>
          <div style={{ borderTop: '1px solid var(--border)', margin: `0 ${COLUMN_PAD_X}px` }} />
        </div>

        {/* Banners */}
        {staleBackend && (
          <div
            role="alert"
            style={{
              margin: `8px ${COLUMN_PAD_X}px 0`,
              padding: '8px 10px',
              borderRadius: '8px',
              background: 'var(--warn-subtle)',
              color: 'var(--warn)',
              fontSize: '11px',
            }}
          >
            {i18nT('apps.mdNotebook.banner.staleBackend', {
              features: staleBackend.join(', '),
            })}
          </div>
        )}
        {fileConflict && (
          <div
            role="alert"
            style={{
              margin: `8px ${COLUMN_PAD_X}px 0`,
              padding: '8px 10px',
              borderRadius: '8px',
              background: 'var(--danger-subtle)',
              color: 'var(--danger)',
              fontSize: '11px',
              display: 'flex',
              gap: '10px',
              alignItems: 'center',
              flexWrap: 'wrap',
            }}
          >
            <span style={{ flex: 1, minWidth: '200px' }}>
              {i18nT('apps.mdNotebook.banner.diskConflict')}
            </span>
            <button
              type="button"
              onClick={() => {
                // Take what is on disk, discarding the in-memory edit.
                setContent(fileConflict.disk)
                contentRef.current = fileConflict.disk
                mtimeRef.current = fileConflict.mtime
                setDirty(false)
                dirtyRef.current = false
                setFileConflict(null)
              }}
              style={{
                background: 'transparent',
                color: 'inherit',
                border: '1px solid currentColor',
                padding: '3px 10px',
                borderRadius: '8px',
                fontSize: '11px',
                cursor: 'pointer',
              }}
            >
              {i18nT('apps.mdNotebook.banner.useDisk')}
            </button>
            <button
              type="button"
              onClick={() => {
                // Keep the editor's version: adopt the fresh mtime so the next
                // save is accepted, then save.
                mtimeRef.current = fileConflict.mtime
                setFileConflict(null)
                void flushSave()
              }}
              style={{
                background: 'transparent',
                color: 'inherit',
                border: '1px solid currentColor',
                padding: '3px 10px',
                borderRadius: '8px',
                fontSize: '11px',
                cursor: 'pointer',
              }}
            >
              {i18nT('apps.mdNotebook.banner.keepMine')}
            </button>
          </div>
        )}
        {/* No hand-off: the open note's editor buffer below is unsaved local
            state — a failed save is exactly when it holds text nowhere else. */}
        {error && (
          <div style={{ margin: `8px ${COLUMN_PAD_X}px 0` }}>
            <ErrorNotice message={error} />
          </div>
        )}

        {/* Body */}
        {!activePath ? (
          <div
            style={{
              flex: 1,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--muted)',
              fontSize: '12px',
            }}
          >
            {i18nT('apps.mdNotebook.body.empty')}
          </div>
        ) : mode === 'raw' ? (
          // Full-width textarea so its scrollbar sits at the pane's far right;
          // the text is centred by padding instead.
          <div style={{ flex: 1, position: 'relative', display: 'flex', minHeight: 0 }}>
            <textarea
              value={content}
              spellCheck={false}
              aria-label={i18nT('apps.mdNotebook.header.view_raw')}
              onChange={e => edit(e.target.value)}
              {...ime.bindComposition<HTMLTextAreaElement>()}
              onKeyDown={e => {
                if (e.key !== 'Tab') return
                const ta = e.currentTarget
                const next = shiftListItem(content, ta.selectionStart, e.shiftKey)
                if (!next) return
                // Claim BEFORE the rewrite: IMEs use Tab to cycle the candidate
                // list, and on WebKit the keydown that commits a candidate
                // arrives after `compositionend` with `isComposing` already
                // false — so an unclaimed Tab would re-indent the list item and
                // overwrite the text still being composed.
                if (!ime.claimKey(e)) return
                e.preventDefault()
                ta.value = next.text
                ta.setSelectionRange(next.pos, next.pos)
                edit(next.text)
              }}
              style={{
                flex: 1,
                width: '100%',
                minHeight: 0,
                boxSizing: 'border-box',
                resize: 'none',
                border: 'none',
                outline: 'none',
                background: 'var(--bg)',
                color: 'var(--text)',
                paddingTop: '14px',
                paddingBottom: '14px',
                paddingLeft: `max(${COLUMN_PAD_X}px, calc((100% - ${
                  COLUMN_MAX_WIDTH - COLUMN_PAD_X * 2
                }px) / 2))`,
                paddingRight: `max(${COLUMN_PAD_X}px, calc((100% - ${
                  COLUMN_MAX_WIDTH - COLUMN_PAD_X * 2
                }px) / 2))`,
                // Full width drops the centring measure. Declared after the two
                // above so it overrides them, leaving their expression untouched.
                ...(fullWidth
                  ? { paddingLeft: `${COLUMN_PAD_X}px`, paddingRight: `${COLUMN_PAD_X}px` }
                  : {}),
                fontSize: `${DOC_BODY_PX}px`,
                lineHeight: DOC_BODY_LINE_HEIGHT,
                fontFamily: FONT_MONO,
              }}
            />
          </div>
        ) : (
          <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
            <div
              style={{
                margin: '0 auto',
                width: '100%',
                maxWidth: columnMaxWidth,
                boxSizing: 'border-box',
                padding: `14px ${COLUMN_PAD_X}px 32px`,
              }}
            >
              <Preview
                content={content}
                noteDir={noteDir}
                vaultRoot={vaultRoot}
                onToggleCheckbox={toggleCheckbox}
                editRange={editBlock}
                onStartEdit={startBlockEdit}
                onCommitEdit={commitBlockEdit}
                onCancelEdit={cancelBlockEdit}
                onSplitEdit={splitBlockEdit}
                onDirtyEdit={markDirty}
              />
              {backlinks.length > 0 && (
                <div style={{ marginTop: '24px', borderTop: '1px solid var(--border)', paddingTop: '12px' }}>
                  <div
                    style={{
                      fontSize: '10px',
                      textTransform: 'uppercase',
                      letterSpacing: '.04em',
                      color: 'var(--muted)',
                      marginBottom: '6px',
                    }}
                  >
                    {i18nT('apps.mdNotebook.body.backlinks', { n: backlinks.length })}
                  </div>
                  {backlinks.map(b => (
                    <Clickable
                      key={`${b.sourcePath}-${b.line}`}
                      className="mdnb-row"
                      aria-label={b.sourcePath}
                      onClick={() => void openNote(b.sourcePath)}
                      style={{
                        padding: '6px 8px',
                        borderRadius: '6px',
                        cursor: 'pointer',
                        fontSize: '11px',
                      }}
                    >
                      <span style={{ color: ACCENT }}>{b.sourcePath}</span>
                      <span style={{ color: 'var(--muted)' }}>{` — ${b.context}`}</span>
                    </Clickable>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
      )}
      </div>
    </div>
  )
}
