---
title: Remote crew file hand-off — attachments past the upload cap, and files back from a remote run
status: draft
author: koenvincent
created: 2026-10-12
last-audited: 2026-10-12
audited-at: d866dd2607
doc-pr: 19460
implementation-prs: []
tracking-issues: [19405]
supersedes: []
superseded-by: []
---

# RFC: Remote crew file hand-off

Status: draft. Nothing built. Every code reference below was read at `d866dd2607`.

This document lands on its own, before any implementation, because it changes what a
user can attach and where those bytes go, and because part 2 adds a surface the
instance proxy does not forward today. The triage on
[#19405](https://github.com/kirodotdev/KiroCrew/issues/19405) asked for a design
sign-off before code.

## 1. Problem

Two limits meet when the work should run on a remote crew.

**The local upload cap.** `POST /api/upload/file` (`dashboard/file_api/uploads.py`
`api_upload_file`) accepts at most `_MAX_UPLOAD_FILES` (20) files per request. The
per-file ceiling is `dashboard.upload_max_mb` (`dashboard/upload_limits.py`
`upload_max_bytes`, default 100 MB, bounded by the loader to 1-512 MB, from #18617).
Only video gets more: `max_bytes = _MAX_VIDEO_UPLOAD_BYTES if is_video else
max_upload_bytes` gives video 512 MB. Media parts stream to disk
(`_stream_media_part`, through `part_stream.stream_part_to_file`); every other
accepted part is read into a `bytearray` in memory up to the ceiling. So raising the
ceiling raises the memory a single request can hold (up to 20 parts), and a dataset,
a folder of documents or a build artifact above 512 MB cannot be attached at all.

**No file path to a remote crew at all.** The crew window that opens a peer's own slot
(`website/src/pages/chat/crew-window/CrewChatWindow.tsx`) does not pass Attach to the
composer, by design: the comment there says Attach uploads to THIS gateway. The
instance proxy forwards only `api/chat` and `api/stream`
(`_PROXY_ALLOWED_PREFIXES` in `dashboard/handlers_instances.py`). So a user who wants a
remote crew to work on local files has no supported way to send them, and no way to get
a remote run's output files back to look at them locally.

The usual workaround is to start the work locally instead, on the machine with less disk
and CPU, which defeats the reason the crew exists.

## 2. Goals and non-goals

Goals:

1. Attach a file or folder to a chat, local or remote, without the ceiling being a
   memory bound, including above 512 MB if maintainers raise the bound.
2. Hand a file or folder to a session that runs on a remote crew, verified on arrival.
3. Get files a remote run produced back to the hub, on request or when the run ends.

Non-goals:

- Live or two-way sync. Each transfer is one explicit push or pull.
- Mounting object storage as a filesystem on the peer.
- Changing the inbound channel attachment cap (`chat_attachments.MAX_ATTACHMENT_BYTES`,
  25 MB). That path has its own limits per channel.

## 3. Proposal

Two independent parts. Part 1 is useful without part 2.

### 3.1 Part 1 — stream every accepted upload to disk

Apply the media path to every accepted type: each part goes through
`part_stream.stream_part_to_file` with its content check, instead of being read into
a `bytearray`. `dashboard.upload_max_mb` then stops being a memory bound and is only a
policy value, with a free-disk check before the write the way `session_transfer.py`
already does for arrivals (`_DiskFull`, `_MAX_CONCURRENT_UPLOADS`). The app-wide
`client_max_size` (60 MB, `dashboard/server.py`) stays as it is for every other route;
the upload route reads its body as a multipart stream.

Whether the loader's 512 MB bound on `upload_max_mb` may then rise is a maintainer
decision (§6, Q1). This part changes no transport and no other surface.

### 3.2 Part 2 — a file hand-off for remote instances

When the target session runs on a peer, the hub does not push the bytes through the
proxied chat request. Instead:

1. **Pack.** The hub packs the selection (one file or one folder) into a tar with a
   manifest: relative path, size and sha256 per entry, plus the total.
2. **Transfer.** The default transport is the hop `session_transfer.py` already uses:
   "an ordinary authenticated dashboard request over an Instances tunnel", hub to the
   operator's own peer, with the body streamed to a staging file. No row is added to
   `_PROXY_ALLOWED_PREFIXES`, so the browser-facing peer surface stays `api/chat` and
   `api/stream`. An object-store provider (S3-compatible, bucket and prefix from
   configuration, no account or bucket names in code) is optional and can be an app
   (§6, Q3).
3. **Verify and unpack on the peer.** The archive is staged under the peer's
   `data_home()/tmp/handoff-import`, the same pattern as `_import_tmp_dir`, which only
   the gateway touches. The peer checks the total size and free disk before it writes,
   then every entry's size and hash. Only a verified archive is unpacked, into the
   session's inbox (layout below). The whole archive is refused when any entry is not a
   regular file or directory, or when its path is absolute, contains `..`, or would land
   outside the inbox through a symlink.
4. **Tell the session.** The next turn's context names the inbox path and what arrived,
   the same way a local attachment is named.
5. **Files back.** Each session on the peer gets an outbox. When a run ends, or when the
   user asks, the peer packs the outbox the same way and the hub fetches and verifies it.
   Small results land automatically; a result above a threshold is announced and fetched
   on request.

**Where the files live.** On the peer, per slot: `data_home()/uploads/handoff/<slot>/in/<transfer-id>/`
for the inbox and `.../out/` for the outbox, under the same `uploads` directory that
composer attachments land in today (`_upload_dir` in `dashboard/file_api/uploads.py`).
`uploads` is not a crew-home leaf in `security.sensitive_home_dirs()`, so it needs no
HIDDEN/READONLY/VISIBLE disposition and `test_sandbox_governance_mask.py` is unaffected;
the agent reads the inbox and writes the outbox the way it already reads an attachment
there. If maintainers prefer the files inside the session's own workspace instead, the
layout moves and nothing else changes (§6, Q4). On the hub, a fetched outbox lands in
the hub's own `uploads/handoff/<slot>/` and is offered in the chat as downloadable
files, so a result is looked at the way an attachment is.

Optional, after the above: an immutable, versioned shared snapshot (`name@vN`) that the
hub publishes once and several remote sessions attach read-only. A peer can read a
snapshot but never write or list one, so one compromised session cannot change what the
other sessions start from.

Retention: inbox and outbox copies held by a provider expire after a configurable period.
Shared snapshots do not expire.

## 4. Prior art

A downstream deployment runs part 2 over an object store plus a queue for its remote
crews: a manifest with sha256 per entry, 5 GiB per transfer, a per-task inbox and
outbox, immutable shared snapshots that peers can only read, and a 7-day expiry on
transfer copies. It has been used end to end with 60 MB folders, single files and run
results in both directions, and as the route that carried patches and test reports for
a review cycle on this repository.

[#12821](https://github.com/kirodotdev/KiroCrew/pull/12821) (remote subagent executor)
already ships one narrow case of part 2: a tracked-files snapshot of the parent project,
bounded and checked by the peer (`dashboard/remote_workspaces.py` on that branch). Part 2
generalizes that path; it does not add a second one.

## 5. Alternatives considered

- **Raise the `upload_max_mb` bound alone.** Every non-media part is read into memory,
  so a higher bound is a memory risk on a small host. Rejected without part 1.
- **Forward `/api/upload/file` through the instance proxy.** It inherits the in-memory
  ceiling, widens the browser-facing proxied surface, and the peer's upload directory is
  not session-scoped. Rejected.
- **Mount the bucket on the peer (FUSE).** Needs a host package, slow for many small
  files, and turns a one-off transfer into shared mutable state. Rejected.

## 6. Open questions for maintainers

1. Is part 1 acceptable as a standalone change, and may the loader bound on
   `dashboard.upload_max_mb` rise above 512 MB once non-media parts stream to disk?
2. Should part 2 live with the instance proxy, or with the remote executor work in
   #12821?
3. Is an object-store provider in scope for core, or should it be an app?
4. Should the inbox and outbox live under the peer's `uploads` directory, as proposed,
   or inside the session's workspace?
