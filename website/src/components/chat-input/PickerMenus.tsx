import SlashCommandMenu from '../SlashCommandMenu'
import FilePickerMenu from '../FilePickerMenu'
import SkillPickerMenu from '../SkillPickerMenu'
import ProjectSkillsTrustDialog from '../ProjectSkillsTrustDialog'
import { PATH_TOKEN_RE } from '../composerTokens'
import type { SendMode } from '../../pages/chat/ChatSettings'
import type { useComposerPickers } from './pickers'
import type { ChatInputProps } from './props'

/** The trigger menus anchored to the composer, and the consent dialog a
 *  project skill asks for before its token can be inserted. */
export function ComposerPickerMenus({ pickers, value, onChange, composerAnchorRef, sendOnEnter, typedCommandMenus, project, agentName, onFileSelect, onFileOpen }: {
  pickers: ReturnType<typeof useComposerPickers>
  value: string
  onChange: (v: string) => void
  composerAnchorRef: React.RefObject<HTMLElement>
  sendOnEnter: SendMode
  typedCommandMenus: boolean
  project?: string
  agentName?: string
  onFileSelect: ChatInputProps['onFileSelect']
  onFileOpen: ChatInputProps['onFileOpen']
}) {
  const {
    slashMenuOpen, setSlashMenuOpen, filePickerOpen, setFilePickerOpen, fileQuery, setFileQuery,
    pathPickerOpen, setPathPickerOpen, pathQuery, setPathQuery, skillPickerOpen, setSkillPickerOpen, skillQuery, setSkillQuery,
    nextTrustRequestIdRef, activeTrustRequestIdRef, trustPrompt, setTrustPrompt, skillSlotKey, skillSlotKeyRef, skillProjectRef,
    applyPickedToken,
  } = pickers
  return (
    <>
      {typedCommandMenus && <SlashCommandMenu input={value} anchorRef={composerAnchorRef} open={slashMenuOpen} sendOnEnter={sendOnEnter} onSelect={cmd => { onChange(cmd); setSlashMenuOpen(false) }} onClose={() => setSlashMenuOpen(false)} />}

      {onFileSelect && (
        <FilePickerMenu
          query={fileQuery}
          anchorRef={composerAnchorRef}
          open={filePickerOpen}
          project={project}
          sendOnEnter={sendOnEnter}
          onFileOpen={onFileOpen}
          onSelect={({ path, relativePath, kind }) => {
            // relativePath already carries a trailing slash for directories
            // (see selectionFor in FilePickerMenu), so the inserted token reads
            // as e.g. "@src/pages/ " and is unambiguously a folder.
            applyPickedToken(/(^|[\s])@\S*$/, `@${relativePath} `)
            setFilePickerOpen(false); setFileQuery('')
            onFileSelect(path, kind, `@${relativePath}`)
          }}
          onClose={() => { setFilePickerOpen(false); setFileQuery('') }}
        />
      )}

      {/* Path completion is not gated on `onFileSelect`: a completed `./path`
          is text the user typed, not a staged attachment, so there is nothing to
          hand to the host. It IS gated on a project dir, which is the root every
          `./` resolves against. */}
      <FilePickerMenu
        pathMode
        query={pathQuery}
        anchorRef={composerAnchorRef}
        open={pathPickerOpen}
        project={project}
        sendOnEnter={sendOnEnter}
        onSelect={({ relativePath, kind }) => {
          // A shell completes a directory to `dir/` and waits for the next
          // segment; a file completion is finished, so it gets the trailing
          // space. Re-seeding the query on a directory keeps the menu open on
          // the new level — the programmatic insert never reaches the composer's
          // own onChange, so the token has to be handed over here.
          applyPickedToken(PATH_TOKEN_RE, kind === 'dir' ? relativePath : `${relativePath} `)
          if (kind === 'dir') setPathQuery(relativePath)
          else { setPathPickerOpen(false); setPathQuery('') }
        }}
        onClose={() => { setPathPickerOpen(false); setPathQuery('') }}
      />

      {typedCommandMenus && <SkillPickerMenu
        query={skillQuery}
        anchorRef={composerAnchorRef}
        open={skillPickerOpen}
        sendOnEnter={sendOnEnter}
        slotKey={skillSlotKey}
        project={project}
        agent={agentName}
        onSelect={({ leaf }) => {
          // Token left literal — backend appends the skill body; the user still
          // sees their $token marker. Caret-relative replace via shared helper.
          applyPickedToken(/(^|[\s])\$[a-z0-9/_-]*$/, `$${leaf} `)
          setSkillPickerOpen(false); setSkillQuery('')
        }}
        onTrustRequest={({ leaf }) => {
          // An unconsented project skill: close the menu and ask, rather than
          // inserting a token that would resolve to nothing.
          setSkillPickerOpen(false); setSkillQuery('')
          const requestId = nextTrustRequestIdRef.current + 1
          nextTrustRequestIdRef.current = requestId
          activeTrustRequestIdRef.current = requestId
          setTrustPrompt({ requestId, leaf, slotKey: skillSlotKey, project })
        }}
        onClose={() => { setSkillPickerOpen(false); setSkillQuery('') }}
      />}
      <ProjectSkillsTrustDialog
        key={trustPrompt?.requestId ?? 0}
        open={trustPrompt !== null}
        skillLeaf={trustPrompt?.leaf ?? ''}
        slotKey={trustPrompt?.slotKey}
        onClose={() => {
          activeTrustRequestIdRef.current = null
          setTrustPrompt(null)
        }}
        onTrusted={leaf => {
          const completedPrompt = trustPrompt
          if (
            !completedPrompt
            || completedPrompt.requestId !== activeTrustRequestIdRef.current
          ) return
          if (
            completedPrompt.slotKey !== skillSlotKeyRef.current
            || completedPrompt.project !== skillProjectRef.current
            || completedPrompt.leaf !== leaf
          ) {
            // Retire this prompt only if it is still current. A superseding
            // request has a different id and must remain open.
            activeTrustRequestIdRef.current = null
            setTrustPrompt(current =>
              current?.requestId === completedPrompt.requestId ? null : current)
            return
          }
          activeTrustRequestIdRef.current = null
          setTrustPrompt(null)
          // The grant makes the token resolvable, so insert it now — the user
          // asked for this skill and has just consented to its directory.
          applyPickedToken(/(^|[\s])\$[a-z0-9/_-]*$/, `$${completedPrompt.leaf} `)
        }}
      />
    </>
  )
}
