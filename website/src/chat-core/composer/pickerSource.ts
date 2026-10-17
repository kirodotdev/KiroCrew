/**
 * Where the composer's trigger pickers (`/` commands, `@` files, `./` paths,
 * `$` skills) read their data from.
 *
 * The menus are shared by every composer, but the data behind them is not:
 * the dashboard reads through its own api client, while an app embed must
 * reach the gateway only through its scoped client, whose declared API paths
 * gate every call. A host hands the menus a source; each member is one read.
 *
 * A member that is absent (no such key) is a picker the surface does not
 * offer. The embed leaves out every read its app was not granted, so the
 * picker is hidden instead of opening on a request the scoped client would
 * refuse. A member that is present but `undefined` is a picker that is
 * offered with nothing to load: the slash menu then shows its offline list,
 * as it always has for a dashboard client without the endpoint. `offers` is
 * the whole rule; nothing else decides whether a menu is offered.
 *
 * Store-free: ChatEmbed mounts the pickers inside an app with no Redux
 * `Provider` (pinned by `ChatEmbed.composerStoreFree.test`).
 */
import { createContext, useContext } from 'react'

/** One row of the `/` menu, as `GET /api/slash-commands` sends it. */
export interface SlashCommand {
  name: string
  /**
   * Description as the SERVER sent it (English). Only a fallback for display —
   * the menu's `commandDescription` prefers the catalog. Optional because the
   * local command lists carry no copy of their own; the backend itself also
   * sends `""` for a command missing from its description map.
   */
  description?: string
}

export type FileKind = 'file' | 'dir'

/** One row of the `@` / `./` menus. */
export interface FileResult {
  path: string
  name: string
  size: number
  mtime: number
  /** Absent on responses from older backends — treated as 'file'. */
  kind?: FileKind
}

/** The `@` file search and `./` path completion payload. */
export interface FileSearchResponse {
  results?: FileResult[]
  root?: string
  /**
   * Path mode only: the directory prefix these rows were produced FOR.
   *
   * Rows and prefix have to travel together. `placeholderData` keeps the
   * PREVIOUS payload on screen while a new key fetches, so a prefix read from
   * anything outside the payload — the live token or even the debounced one —
   * is one level deeper than the rows a user can still see and choose, and the
   * accepted token comes out doubled (`./src/src/`).
   */
  dir?: string
  /**
   * Path mode only: `true` when the token resolved out of the project, which is the
   * one thing zero rows cannot say for itself — an out-of-project token and an empty
   * directory look identical from here. It rides in the payload with the rows it
   * describes, so a placeholder answer cannot label the wrong ones.
   */
  outside?: boolean
}

export interface ComposerPickerSource {
  /** Prefix for every picker query key, so two sources never share a cache
   *  entry. Empty for the dashboard, whose keys other code invalidates. */
  scope: string
  slashCommands?: (signal?: AbortSignal) => Promise<SlashCommand[]>
  fileSearch?: (q: string, project: string | undefined, signal?: AbortSignal) => Promise<FileSearchResponse>
  pathComplete?: (project: string, dir: string, q: string, signal?: AbortSignal) => Promise<FileSearchResponse>
  /** The raw `/api/skills` payload (a bare array or the agent-scoped envelope). */
  skills?: (slotKey: string | undefined, agent: string | undefined, signal?: AbortSignal) => Promise<unknown>
}

const ComposerPickerSourceContext = createContext<ComposerPickerSource | null>(null)

/** Hands the pickers below it their data source. */
export const ComposerPickerSourceProvider = ComposerPickerSourceContext.Provider

/** The source a host provided, or null (the dashboard's own client then serves). */
export function useComposerPickerSource(): ComposerPickerSource | null {
  return useContext(ComposerPickerSourceContext)
}

/** Whether the source offers the picker behind `read` (see the module doc). */
export function offers(source: ComposerPickerSource, read: Exclude<keyof ComposerPickerSource, 'scope'>): boolean {
  return read in source
}

/** A picker query key under the source's scope. */
export function pickerQueryKey(source: ComposerPickerSource, key: readonly unknown[]): unknown[] {
  return source.scope ? [source.scope, ...key] : [...key]
}
