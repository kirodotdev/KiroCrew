/**
 * Launch a KAS workflow into a VISIBLE dashboard chat.
 *
 * A KAS run's parent is a chat the user can read: KAS posts the run's summary
 * there when it ends. So a launch is four steps, in this order:
 *
 *   1. find or create the "Workflows" chat folder (`/api/chat/folders`);
 *   2. create the chat, filed into it and titled "Workflow: <recipe>"
 *      (`POST /api/chat/slots`);
 *   3. set the chat's project to the run's project (the chat's project IS the
 *      run's project on the backend) — the same `POST …/project` the chat page
 *      uses, applied by the `createSlot` thunk before the slot is published;
 *   4. ask the backend to launch the run in that chat (`POST /api/workflows/kas/runs`).
 *
 * The orchestration is a plain function over injected steps so its order and
 * its rollback are tested without a store (`launchKasWorkflow.test.ts`).
 */
import { useCallback } from 'react'

import { api, type KasRecipe } from '../../../api/client'
import { useAppDispatch } from '../../../store'
import { createSlot, deleteSlot } from '../../../store/chatSlice'
import { i18nT } from '../../../i18n/t'
import { ensureChatFolder } from '../../../utils/ensureChatFolder'
import { KAS_CHAT_TITLE_PREFIX, KAS_WORKFLOWS_FOLDER } from './wireNames'

/** The folder every KAS run's chat is filed into, and the chat title prefix.
 *  Both are durable server-side names, not catalog copy (see `wireNames.ts`). */
export { KAS_CHAT_TITLE_PREFIX, KAS_WORKFLOWS_FOLDER }

export function kasChatTitle(recipeName: string): string {
  return `${KAS_CHAT_TITLE_PREFIX}${recipeName}`
}

export interface KasLaunchSteps {
  ensureFolder: () => Promise<string>
  /** Create the chat with its project already set; resolves to its slot key. */
  createChat: (opts: { title: string; folderId: string; project: string }) => Promise<string>
  launch: (body: {
    slot: string
    source: string
    inputs?: Record<string, string>
    name?: string
    runLabel?: string
    model?: string
    effort?: string
  }) => Promise<{ slot: string; queued: boolean }>
  /** Remove a chat whose run never started. */
  discardChat: (slot: string) => Promise<unknown>
}

export interface KasLaunchArgs {
  recipe: Pick<KasRecipe, 'name' | 'source'>
  project: string
  inputs?: Record<string, string>
  /** Launch options: the run's display name (default: the recipe's), and the
   *  model / effort its steps run on (absent: each step's own). */
  name?: string
  model?: string
  effort?: string
}

export interface KasLaunchResult {
  slot: string
  queued: boolean
}

/** Drop blank inputs so an untouched field is "not given" rather than "". */
export function cleanInputs(inputs: Record<string, string> | undefined): Record<string, string> | undefined {
  if (!inputs) return undefined
  const out: Record<string, string> = {}
  for (const [name, value] of Object.entries(inputs)) {
    if (value.trim()) out[name] = value
  }
  return Object.keys(out).length ? out : undefined
}

export async function launchKasWorkflow(steps: KasLaunchSteps, args: KasLaunchArgs): Promise<KasLaunchResult> {
  const project = args.project.trim()
  if (!project) throw new Error(i18nT('apps.workflows.kas.project_required'))
  // A recipe KAS listed without a `source` cannot be launched (the launch form
  // disables the button; this guards the path regardless).
  const source = args.recipe.source
  if (!source) throw new Error(i18nT('apps.workflows.kas.not_launchable'))
  const typedName = args.name?.trim()
  const name = typedName || args.recipe.name
  const model = args.model?.trim()
  const effort = args.effort?.trim()
  const folderId = await steps.ensureFolder()
  const slot = await steps.createChat({ title: kasChatTitle(name), folderId, project })
  try {
    const inputs = cleanInputs(args.inputs)
    return await steps.launch({
      slot,
      source,
      ...(inputs ? { inputs } : {}),
      name,
      // A typed display name is the run's own label in Kiro Agent (`runLabel`),
      // so the run lists, titles its steps and reports back under it.
      ...(typedName ? { runLabel: typedName } : {}),
      ...(model ? { model } : {}),
      ...(effort ? { effort } : {}),
    })
  } catch (err) {
    // The launch was refused, so the chat holds nothing and never will: an
    // empty "Workflow: …" chat left behind would read as a run that started.
    // A failed cleanup must not mask the refusal the user needs to see.
    await steps.discardChat(slot).catch(() => {})
    throw err
  }
}

/** The real steps: the shared folder helper, the `createSlot` thunk (which
 *  POSTs the slot and then its project, and deletes the slot itself if the
 *  project is refused), the KAS launch route, and `deleteSlot` for rollback. */
export function kasLaunchSteps(dispatch: ReturnType<typeof useAppDispatch>): KasLaunchSteps {
  return {
    ensureFolder: async () => {
      const id = await ensureChatFolder({
        list: () => api.chatFolders(),
        create: name => api.createChatFolder(name),
        name: KAS_WORKFLOWS_FOLDER,
      })
      if (!id) throw new Error(i18nT('apps.workflows.kas.folder_failed'))
      return id
    },
    createChat: async ({ title, folderId, project }) => {
      const slot = await dispatch(
        createSlot({ title, folder_id: folderId, project, activate: false, sessionFolder: false }),
      ).unwrap()
      return slot.key
    },
    launch: body => api.kasLaunchRun(body),
    discardChat: slot => dispatch(deleteSlot(slot)).unwrap(),
  }
}

/** `launch(args)` for components. */
export function useKasLaunch(): (args: KasLaunchArgs) => Promise<KasLaunchResult> {
  const dispatch = useAppDispatch()
  return useCallback((args: KasLaunchArgs) => launchKasWorkflow(kasLaunchSteps(dispatch), args), [dispatch])
}
