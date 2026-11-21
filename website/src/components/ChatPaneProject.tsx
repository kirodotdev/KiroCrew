import { useCallback, useState } from 'react'
import ProjectPicker from './ProjectPicker'
import { ProjectAwsProfileSection } from './ProjectAwsProfile'
import { clearSlotWriteFailure, reportSlotWriteFailure } from './slotWriteFailure'
import { useAnchoredTriggerRect } from '../hooks/useAnchoredTriggerRect'
import { useProjectGit } from '../pages/chat/page/composerChips'
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
  // Counted, so overlapping picks clear the spinner only when the last one ends.
  const [switches, setSwitches] = useState(0)
  const setProject = useCallback(async (path: string) => {
    clearSlotWriteFailure(dispatch, setSwitchError)
    setSwitches(n => n + 1)
    try {
      await performProjectSlotSwitch(slotKey, path, dispatch)
    } catch (e) {
      reportSlotWriteFailure(agentSwitchErrorNotice(e), dispatch, setSwitchError)
    } finally {
      setSwitches(n => n - 1)
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
    projectSwitching: switches > 0,
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
