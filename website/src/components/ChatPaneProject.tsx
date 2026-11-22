import { useCallback, useState } from 'react'
import ProjectPicker from './ProjectPicker'
import { ProjectAwsProfileSection } from './ProjectAwsProfile'
import { clearSlotWriteFailure, reportSlotWriteFailure } from './slotWriteFailure'
import { useAnchoredTriggerRect } from '../hooks/useAnchoredTriggerRect'
import { useProjectGit } from '../hooks/useProjectGit'
import { performProjectSlotSwitch } from '../lib/projectSwitch'
import { useAppSelector, type AppDispatch } from '../store'
import { selectProjectChipLocked } from '../store/chatSlice'
import { agentSwitchErrorNotice } from '../utils/agentSwitchFeedback'

/**
 * `ChatPane`'s project chip, bound to the pane's own slot: the folder's branch,
 * the lock and the switch the main chat's composer gets, and the picker the
 * chip opens. `useChatPaneProject` is what the composer is given;
 * `ChatPaneProjectPicker` draws the picker from it.
 */
export function useChatPaneProject({ slotKey, project, dispatch, setSwitchError }: {
  slotKey: string
  project: string
  dispatch: AppDispatch
  setSwitchError: (message: string) => void
}) {
  const { projectGit, projectGitError, projectGitFailure, projectBranch } = useProjectGit(project)
  const projectLocked = useAppSelector(s => selectProjectChipLocked(s, slotKey))
  const [pickerOpen, setPickerOpen] = useState(false)
  const { rect: projectBtnRect, anchorTo: anchorProjectBtn } = useAnchoredTriggerRect(pickerOpen)
  // Counted per slot, so overlapping picks clear the spinner only when the last
  // one ends, and a pane moved to another slot does not carry the spinner along.
  const [switches, setSwitches] = useState<Record<string, number>>({})
  const setProject = useCallback(async (path: string) => {
    const slot = slotKey
    clearSlotWriteFailure(dispatch, setSwitchError)
    setSwitches(prev => ({ ...prev, [slot]: (prev[slot] ?? 0) + 1 }))
    try {
      await performProjectSlotSwitch(slot, path, dispatch)
    } catch (e) {
      reportSlotWriteFailure(agentSwitchErrorNotice(e), dispatch, setSwitchError)
    } finally {
      setSwitches(prev => {
        const { [slot]: n = 0, ...rest } = prev
        return n > 1 ? { ...rest, [slot]: n - 1 } : rest
      })
    }
  }, [dispatch, setSwitchError, slotKey])
  return {
    project,
    projectBranch,
    projectDetached: !projectGitError && !!projectGit?.detached,
    // The chip falls back to the folder name alone; the pane names why.
    projectGitFailureMessage: projectGitError
      ? (projectGitFailure instanceof Error ? projectGitFailure.message : String(projectGitFailure ?? ''))
      : '',
    projectLocked,
    projectSwitching: (switches[slotKey] ?? 0) > 0,
    pickerOpen,
    setPickerOpen,
    projectBtnRect,
    setProject,
    onProjectClick: (rect: DOMRect, trigger?: HTMLElement) => {
      anchorProjectBtn(rect, trigger)
      setPickerOpen(o => !o)
    },
  }
}

export function ChatPaneProjectPicker({ project }: { project: ReturnType<typeof useChatPaneProject> }) {
  const { pickerOpen, setPickerOpen, projectBtnRect, setProject } = project
  // No `errorHandoff`: the hand-off navigates away from the split this pane lives in.
  return (
    <ProjectPicker
      open={pickerOpen}
      onOpenChange={setPickerOpen}
      anchorRect={projectBtnRect}
      onSelect={path => { void setProject(path); setPickerOpen(false) }}
      startPath={project.project}
      footer={project.project ? <ProjectAwsProfileSection project={project.project} /> : undefined}
    />
  )
}
