---
title: In-app dashboard links -- a plain click on a link to a dashboard page opens it in the same window
status: draft
author: peterhieuvu
created: 2026-11-05
last-audited: 2026-11-05
audited-at: 142d40ecfc
doc-pr: null
implementation-prs: [26450]
tracking-issues: [26442]
supersedes: []
superseded-by: []
---

# RFC: In-app dashboard links

- Status: draft. Nothing is on main. The implementation is open as [#26450](https://github.com/kirodotdev/KiroCrew/pull/26450) and waits on this document; [#26442](https://github.com/kirodotdev/KiroCrew/issues/26442) is the issue (tier T3, because it changes how existing links behave). This document asks a maintainer to accept or change the design.
- Main measured at `142d40ecfc`. Every file and symbol named below is on the #26450 head `c1fa2144f3`, paths under `website/src/`.

## 1. Problem

A link in a chat message, or in an agent-written page (a Bot's Dashboard tab, an artifact, a widget), that points at a page of this same dashboard opens a new browser tab. In the desktop app that is a second window. Only one kind of link already behaves differently: a session link in a chat message switches session in place.

- In a chat message, every link that is not a session, file or editor link gets `target="_blank"` (the anchor renderer in `components/MarkdownRenderer.tsx`).
- In a frame, the link bridge opens every accepted URL with `window.open(url, '_blank', 'noopener,noreferrer')` (`openExternal` in `hooks/useFrameOpenLink.ts`).

So an agent that writes `see [Schedule](/schedule)` sends the person to a copy of the dashboard they are already in, with its own socket, its own scroll and no shared composer draft.

## 2. Goals and non-goals

Goals:

- A plain click on a link to a known dashboard page opens it in the window the person is in, the way a sidebar click does.
- The same rule for chat messages and for the links in agent-written pages that the host already opens: links that open a window, and script opens. A bare root-relative link in a frame is not covered (section 3.2).
- No new way for page content to reach a page action or an arbitrary URL.

Non-goals:

- Web URLs. They keep opening in a new tab, with the existing gesture checks.
- Popout and embed windows. They keep today's behaviour.
- A user setting. Section 9 asks whether one is wanted.

## 3. Design

### 3.1 Which links open in place

One resolver, `resolveInternalNav` in `utils/internalNav.ts`, decides. It returns a rebuilt path, or null to leave the link as it is.

Origin. The link must be `http:` or `https:` with no user or password. Its origin must be the page's own, or exactly one of two configured origins: `dashboard.url` and the tunnel's public URL, read from `GET /api/config/kirocrew` and `GET /api/tunnel/status` (`useInternalNavHandler` in `hooks/useInternalNavHandler.ts`). A relative href resolves against the page's own origin. A sub-path build's base is removed before matching (`appPathname`).

Destinations. Only this list, each with the query parameters it may carry (`DESTINATIONS`):

| Path | Parameters |
|---|---|
| `/schedule` | `job` |
| `/settings` and up to three sub-segments | `tab`, `sub`, `section`, `highlight`, `channel` |
| `/capabilities` | `tab` |
| `/members` | `member` (the parameter `MembersPage` selects by, `MEMBER_PARAM`) |
| `/sessions`, `/session-dashboards`, `/notifications`, `/crew-board`, `/logs`, `/apps`, `/apps/library` | none |
| `/artifacts` | `folder` |
| `/artifacts/<slug>`, except `remote` and `deploy` | none |
| `/chat` or `/chat/<one segment>` | `sid` or `slot`, naming a full session key |

Popout and embed routes, app pages and redirect-only aliases are left out on purpose. A chat link must name a session by its full key, and it is rebuilt as `/chat?sid=<key>` (`isChatPath` and `sessionKeyFrom`).

### 3.2 What still opens a new tab

- A middle click or a click with Cmd, Ctrl, Alt or Shift. In a chat message only a plain primary click is taken (the message click handler in `components/MarkdownRenderer.tsx`). In a frame the link-bridge shim (`LINK_BRIDGE_SHIM_BODY` in `lib/widgetSrcdoc.ts`) posts a middle click, or a click with Cmd, Ctrl or Shift, with `tab: true`, and the bridge then opens a new tab (`wantsNewTab` and `go` in `hooks/useFrameLinkBridge.ts`). Alt is not yet included in a frame (section 8).

What reaches the host from a frame is unchanged by this design. The shim posts a link that opens a window (`target="_blank"` or any target but `_self`), a bare absolute web link, and a `window.open` call. A link with no target and a root-relative href, such as a bare `<a href="/schedule">`, is left to the frame (`planOf` returns null for it), so it navigates the frame itself, as it does today. Section 9 asks whether it should reach the host.
- Any link the resolver refuses, including every web URL.
- A chat link some earlier handler already took, such as a session, file or staged-dashboard link. The in-place step runs last and only when the click is not yet handled (`!e.defaultPrevented`).

A link in a popout or embedded window keeps its previous behavior. The shell installs the in-app navigator only in the main window (`useInternalNavHandler(!isPopout && !isEmbed, …)` in `shell/appShell.tsx`), and with none installed `openInternalLink` returns false, so this design does not change what such a link does.

### 3.3 The leave check runs first

An accepted link goes through `useOpenInAppPath` in `shell/nav/openInAppPath.ts`, the same function the desktop menu and the Crew Companion's "Open session" use (`subscribeNativeNavigate(openInAppPath)` in `shell/appShell.tsx`). It asks the page on screen first (`useMayLeaveForNavigation`), as a sidebar click does. A link to the address already on screen skips the question, because it unmounts nothing (`useIsCurrentUrl` in `components/NavigationLeaveGuard.tsx`, which compares pathname and query). A same-page link that adds a fragment does not yet skip it (section 8). A session link selects the session through `applyNavIntentInMain` in `utils/navIntent.ts`, which runs the same check through its `mayLeave` dependency.

### 3.4 Frames: tying the open to the person

A frame's request is honoured only while the dashboard window has a live user activation and focus is in that frame (`hasGesture` in `hooks/useFrameOpenLink.ts`). The bridge then opens at once when the click is tied to the frame (`tiedToFrame` in `hooks/useFrameLinkBridge.ts`); a web URL always needs this full tie. When neither tie holds, the existing "Open" button appears beside the frame (`FrameLinkChip`), and the person's click on it opens the link.

For an in-app link only, #26450 adds a weaker tie (`pointerTiedToFrame`): focus came into this frame from the dashboard, the pointer is over it and entered it after the dashboard's last click or key, and no sibling frame reported input within an activation's lifespan. Without it, a link clicked within a few seconds of opening the page, or of clicking its tab, needed a second click on the "Open" button.

This tie does not prove the click happened in the frame. A page script that moves focus into its own frame while the pointer rests over it, within the activation lifespan of a dashboard click, can open an in-app link with no click in the frame. Section 4 states the limits; section 9 asks the reviewer to keep or drop it.

## 4. Security considerations

- The path that is navigated is rebuilt from parsed fields, never the authored text (the tail of `resolveInternalNav`). Each segment must match `[A-Za-z0-9._~-]`, at most 128 characters, with `.` and `..` refused (`SEGMENT_RE`). A fragment must match a short anchor pattern (`HASH_RE`). A link therefore cannot carry its own URL, scheme or traversal into the router.
- For every destination but `/chat`, a query parameter not listed for it, a repeated one, one longer than 512 characters, or one holding a control character refuses the whole link (`MAX_PARAM_LENGTH` and the parameter loop in `resolveInternalNav`). A `/chat` link is checked only for parameter names (`sid`, `slot`); its session key must then pass `sessionKeyFrom`, and the link is rebuilt with that one key. Section 8 lists bringing `/chat` under the same checks. A link cannot reach a page action through a parameter the list never named.
- The pointer tie in section 3.4 is the one open that can happen without a click in the frame. Its reach is bounded: only a page on the list in section 3.1, never a web URL or a new tab; the leave check still runs first, so unsaved work is never discarded without the person's answer; and only within an activation lifespan of the person's own dashboard click or key.
- The extra origins are exact. No loopback alias is added, because `localhost` or `127.0.0.1` on another port may be a different gateway (the `useInternalNavHandler` doc comment).
- The change only moves this window to a page the person could open from the sidebar, behind the leave check. It grants no page a new capability.

## 5. Alternatives considered

- **Treat any same-origin path as in-app.** That would let page content open popout and embed routes, app pages and query-driven actions in the main window. The list keeps the reachable set to pages a person can reach from the navigation, with known parameters.
- **Accept any loopback origin.** Another port on `localhost` may be a different gateway with different data, and a link would open it as if it were this one.
- **Only drop `target="_blank"`.** In a chat message the browser would then do a full page load: the socket reconnects, state resets, and the leave check never runs. It does nothing for frames, which open links through the bridge. It also removes the new tab for modified clicks unless that is rebuilt.

## 6. Compatibility

Someone who relied on a plain click opening a second tab gets the same page in place. The escape hatch is the browser's own: a middle click, or Cmd/Ctrl-click, still opens a new tab, in chat and in frames. Links in popouts and embeds, and every web URL, are unchanged. In chat a relative `/schedule` link needs nothing new. In an agent-written page the link must open a window (`target="_blank"`) to reach the host, which the dashboard skills already tell agents to write.

## 7. Rollout and tests

One PR, #26450, with no flag. It ships these tests:

- `utils/internalNav.test.ts` and `utils/internalNav.basePath.test.ts`: the destination list, parameter refusal, origins, traversal and fragment rules, and a sub-path base.
- `hooks/useInternalNavHandler.test.tsx`: the extra origins and the main-window-only install.
- `shell/nav/openInAppPath.test.tsx`: the leave check, and its skip for the current address.
- `test/MarkdownRenderer.internalNav.test.tsx`: plain versus modified clicks, and handlers that take a click first.
- `hooks/useFrameLinkBridge.test.tsx` and `test/widgetSrcdocLinkBridge.test.tsx`: the `tab` flag, the pointer tie and the "Open" button fallback.

The feature map gains an "In-app dashboard links" row, and the dashboard skills say a dashboard page opens in place.

## 8. Known gaps in #26450

Measured at `c1fa2144f3`. Each is fixed in #26450 before it merges.

1. **Alt-click in a frame.** The shim's click handler marks Cmd, Ctrl and Shift as a new-tab click, not Alt, so an Alt-click on a dashboard link in a frame opens it in place. The chat side already includes Alt.
2. **`/chat` parameter checks.** The `/chat` branch of `resolveInternalNav` returns before the repeat, length and control-character checks the other destinations get, so `/chat?sid=a&slot=b` or a repeated `sid` opens the first key instead of being refused.
3. **Fragment on the current address.** `useIsCurrentUrl` compares the current pathname and query with the whole target, fragment included, so a same-page link with `#section` asks the leave question on a page with an unsaved draft.
4. **`/members` parameter.** `DESTINATIONS` allows `name` for `/members`, but `MembersPage` selects a member by `member` (`MEMBER_PARAM` in `pages/members/membersPageModel.ts`), and the dashboard's own links use it (`/members?member=<name>`, for example in `src/kiro_crew/milestones.py`). So a real member link is refused and keeps its new tab. The list in section 3.1 is the intended one.

## 9. Open questions

1. Is a setting wanted for people who want every link in a new tab, or is the modified click enough?
2. Should `/chat` accept a short session name, which needs the session roster to resolve? Today only a full key is taken (the `resolveInternalNav` doc comment).
3. Should app pages join the list once the app platform says which of their routes are safe to open from a link?
4. Should a popout window forward an in-app link to the main window, as app pop-outs already do for `/chat` navigation, instead of keeping its previous behavior?
5. Should a bare root-relative link in an agent-written page (`<a href="/schedule">` with no target) reach the host and open in place, instead of navigating the frame? That needs the shim to relay it, while fragments and other relative links stay in the frame.
6. Keep or drop the pointer tie (section 3.4)? Keeping it opens an in-app link on the first click after opening a page, with the residual in section 4. Dropping it leaves those clicks to the "Open" button.
