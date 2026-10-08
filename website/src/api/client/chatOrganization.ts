/**
 * How conversations are organized in the sidebar and the sessions board:
 * chat folders and their reorder, filing a channel's conversations into its
 * folder, per-slot folder/color/pin/mode, chat tags and per-slot tags,
 * drop-to-column, and the board columns.
 */

import type { AgentTagPolicy, SessionLaneKey } from '../../types'
import type { ClientTransport } from './transport'

/** One moved conversation, as `POST /api/channel-folders/backfill` reports it. */
export interface ChannelFolderBackfillMoved {
  key: string
  title: string
  label: string
}

/** The report `POST /api/channel-folders/backfill` answers with.
 *
 *  The endpoint's report IS its response body and the settings panel renders
 *  exactly these fields, so anything added here has to be kept true on every
 *  path through the handler.
 *
 *  Typed HERE rather than in the panel that renders it because the request itself
 *  belongs on this transport. The panel used to issue its own `fetch`, which
 *  carried no `X-Session-Key` and reached none of the recovery `j` runs, so an
 *  expired session was reported as a generic failure with no way to sign back in
 *  (#12127).
 */
export interface ChannelFolderBackfillReport {
  folder_name: string
  moved: ChannelFolderBackfillMoved[]
  /** Which non-success outcome this was, `''` on a plain pass. Carried on a 200
   *  as well: the endpoint answers 200 with a reason, because "nothing to do" is
   *  a normal result the panel has to render rather than an error. */
  reason: string
  remaining: number
  /** How many of `remaining` are outstanding because their write FAILED rather
   *  than because the run hit its cap. The two need different copy: one says
   *  click again to continue, the other says something went wrong. */
  failed: number
}

export function createChatOrganizationEndpoints({ post, del, patch, j, jfetch: fetch, sessionKeyHeader: _sk }: ClientTransport) {
  const sidebar = {
    // Folders
    chatFolders: () => fetch('/api/chat/folders', { headers: { ..._sk } }).then(j),
    /** `config` carries the folder settings the create modal collects. Each is
     *  omitted when empty so the backend applies its own default. */
    createChatFolder: (name: string, parentId?: string, config?: { project_dir?: string; default_agent?: string; color?: string; icon?: string; tags?: string[]; steering_dirs?: string[] }) =>
      post('/api/chat/folders', { name, parent_id: parentId || '', ...(config ?? {}) }).then(j),
    updateChatFolder: (id: string, body: object) => patch('/api/chat/folders/' + encodeURIComponent(id), body).then(j),
    /** Set several folders' `order` in ONE atomic request. The sidebar drag
     *  renumbers a run of siblings, and one PATCH per row has no transaction: a
     *  failure partway leaves a mix of old and new order numbers. This posts the
     *  whole list to the reorder endpoint, which applies it all-or-none under the
     *  folder-store lock, so a rejected write leaves the stored order untouched
     *  rather than half-applied (issue #10406). */
    reorderChatFolders: (orders: { id: string; order: number }[], expectedParent?: string) =>
      // Empty string is a real claim (the root lane); only undefined means the
      // caller makes no assumption about which container it computed against.
      post('/api/chat/folders/reorder', expectedParent === undefined ? { orders } : { orders, expected_parent: expectedParent }).then(j),
    deleteChatFolder: (id: string) => del('/api/chat/folders/' + encodeURIComponent(id)).then(j),
    /** Delete every folder whose subtree holds no live session and no setting.
     *  `dryRun` lists the ids (and how many archived sessions each one holds)
     *  without changing anything; top-level folders are spared unless asked.
     *  A real run passes the previewed `ids` and deletes only those still empty. */
    cleanupChatFolders: (opts: { dryRun?: boolean; includeTopLevel?: boolean; ids?: string[] }) =>
      post('/api/chat/folders/cleanup', { dry_run: !!opts.dryRun, include_top_level: !!opts.includeTopLevel, ...(opts.ids ? { ids: opts.ids } : {}) }).then(j) as Promise<{
        ok: boolean
        ids?: string[]
        deleted?: string[]
        count: number
        archived?: Record<string, number>
      }>,
    /** File a channel's EXISTING conversations into the folder its settings name.
     *
     *  On this transport rather than the panel's own `fetch`, which is what every
     *  sibling settings panel already does and what the panel gains by it: the
     *  `X-Session-Key` header, `checkSessionExpired`'s silent refresh and re-auth
     *  banner, and an `ApiError` whose message is the sign-in instruction instead of
     *  the gateway's cryptographic reason. Raw `fetch` reaches none of that, so an
     *  expired session was told the panel's generic failure sentence and given no
     *  way to sign back in (#12127).
     *
     *  A 4xx is reserved for a request that was never actionable; every other
     *  outcome arrives as a 200 whose `reason` says which it was. */
    backfillChannelFolder: (namespace: string) =>
      post('/api/channel-folders/backfill', { namespace }).then(j) as Promise<ChannelFolderBackfillReport>,
    setSlotFolder: (slot: string, folderId: string | null) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/folder', { folder_id: folderId || '' }).then(j),
    setSlotColor: (slot: string, colorIndex: number | null) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/color', { color_index: colorIndex }).then(j),
    /** Set a custom per-session color (#rrggbb). The backend clears color_index
     *  when a hex is set and vice versa (mutual exclusion), so callers send one
     *  or the other, never both. */
    setSlotColorHex: (slot: string, colorHex: string | null) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/color', { color_hex: colorHex }).then(j),
    /** Clear BOTH color fields in one PATCH. The endpoint is in-body-gated, so
     *  an index-only null would leave a custom hex behind. */
    clearSlotColor: (slot: string) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/color', { color_index: null, color_hex: null }).then(j),
    setSlotPin: (slot: string, pinned: boolean) => patch('/api/chat/slots/' + encodeURIComponent(slot) + '/pin', { pinned }).then(j),
    /** Set the order of pinned sessions in one atomic write; answers the stored order.
     *  `onlyIfUnset` lands the write only while no order is stored (a 409
     *  otherwise), which is how a browser hands over its old local order. */
    setPinnedOrder: (keys: string[], onlyIfUnset = false, expectedCreated?: Record<string, string>) => post('/api/chat/pinned-order', {
      keys,
      ...(onlyIfUnset ? { only_if_unset: true } : {}),
      ...(expectedCreated && Object.keys(expectedCreated).length > 0 ? { expected_created: expectedCreated } : {}),
    }).then(j) as Promise<{ ok: boolean; order: string[] }>,
    // Tags
    chatTags: () => fetch('/api/chat/tags', { headers: { ..._sk } }).then(j),
    createChatTag: (name: string, color?: string, status?: boolean) => post('/api/chat/tags', { name, color: color || '', status: !!status }).then(j),
    adoptChatTag: (id: string, status: boolean) => post('/api/chat/tags/' + encodeURIComponent(id) + '/adopt', { status }).then(j),
    updateChatTag: (id: string, body: { name?: string; color?: string; order?: number; status?: boolean; agent?: AgentTagPolicy }) => patch('/api/chat/tags/' + encodeURIComponent(id), body).then(j),
    deleteChatTag: (id: string) => del('/api/chat/tags/' + encodeURIComponent(id)).then(j),
    setSlotTags: (slot: string, tags: string[], baseTagsRevision?: string) => fetch('/api/chat/slots/' + encodeURIComponent(slot) + '/tags', { method: 'PUT', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify(baseTagsRevision ? { tags, base_tags_revision: baseTagsRevision } : { tags }) }).then(j),
    dropSlotToColumn: (slot: string, columnId: string) => post('/api/chat/slots/' + encodeURIComponent(slot) + '/drop', { column_id: columnId }).then(j),
    tagColumns: () => fetch('/api/chat/tag-columns', { headers: { ..._sk } }).then(j),
    createTagColumn: (body: { name?: string; tag_ids?: string[]; mode?: 'any' | 'all' | 'none'; include_untagged?: boolean; source?: 'tags' | 'state'; state_key?: SessionLaneKey }) => post('/api/chat/tag-columns', body).then(j),
    updateTagColumn: (id: string, body: { name?: string; tag_ids?: string[]; mode?: 'any' | 'all' | 'none'; order?: number; include_untagged?: boolean }) => patch('/api/chat/tag-columns/' + encodeURIComponent(id), body).then(j),
    deleteTagColumn: (id: string) => del('/api/chat/tag-columns/' + encodeURIComponent(id)).then(j),
    reorderTagColumns: (ids: string[]) => fetch('/api/chat/tag-columns/order', { method: 'PUT', headers: { 'Content-Type': 'application/json', ..._sk }, body: JSON.stringify({ ids }) }).then(j),
  }

  return { sidebar }
}
