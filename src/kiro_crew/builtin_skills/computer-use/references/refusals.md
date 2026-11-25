# Reading the refusals correctly

Moved verbatim from the computer-use `SKILL.md` core.

| You see | What it means | What to do |
|---|---|---|
| a value ending in `…` | the text was cut at `text_limit`, not truncated by the app | re-snapshot with a larger `text_limit`; do NOT read the screenshot to recover it, and do not tell the user the content is missing |
| `[tree truncated at N nodes]` | the window has more controls than the budget | raise `max_tree_nodes`, or scroll to bring your target into range — the rest of the window is real, you just have not been shown it |
| `Screenshot suppressed: the accessibility tree was truncated …` | a cut-off walk cannot prove the window holds no password field, so no pixels were captured. Routine for a browser or an Electron app at the default budget | if you actually need the image, re-snapshot with a higher `max_tree_nodes` / `max_tree_depth`; otherwise work from the tree and do not re-request the screenshot |
| `no state for 'X'. Call computer_get_state first.` | you acted without a snapshot | snapshot, then act |
| `state for 'X' is 214s old. Call computer_get_state again.` | the snapshot expired (90s) | snapshot again |
| `element_index 7 changed since the last computer_get_state (was 'AXButton "Save"', now 'AXButton "Delete"')` | the UI moved under you — this refusal is what stopped you clicking the wrong thing | snapshot again and re-locate the element by its label, not its old number |
| `'…' is a blocked target for computer use (…)` | Kiro Crew's own dashboard is permanently refused — driving it would let you change your own security settings | do the task another way; tell the user why |
| `refusing to type this text into 'X': …` | the text looked like a sensitive command or credential | do not rephrase to get around it; explain and stop |
| `refusing to send input to a secure (password) field …` | the target is a password field | ask the user to type it themselves |
| `computer use is disabled …` | the primary switch is off | tell the user to enable it in Settings → Computer Use; do not retry |
| `computer use is not supported on this platform (…)` | no driver for this OS (e.g. Linux) | say so once |
| `click_method 'app_post' is macOS-only …` / `'sky_click' is macOS-only …` | a macOS-only click method on Windows | use an `element_index` (no pointer moves), or name `click_method: "global"` to accept the cursor move |
| `click_method 'sky_click' supports only the left mouse button (…)` | `sky_click` is a left-button-only recipe | use `left`, or switch to `app_post` for a right/middle click — the refusal names it |
| `click_method 'auto' with coordinates cannot be served on Windows` | `auto` + x/y, where the only coordinate route moves the real cursor | pass an `element_index`, or name `global` explicitly — `auto` never takes the pointer on its own |
| `element N … advertises no action this platform can perform` | the element implements no invoke / toggle / select / legacy default action | try its parent or a child from `computer_get_state` |
| `dragging on Windows moves the operator's real cursor …` | a drag with the default `click_method` (the macOS app-scoped route) | pass `click_method: "global"` explicitly — Windows has no pointer-free drag, so the opt-in is the whole point. There is no element form of a drag |
| `a right-button click cannot be delivered to element N …` | a non-left button on the element path, where UIA has no context-menu pattern | name `click_method: "global"` with coordinates, or address the menu command directly if `computer_get_state` already lists it |
| `moving the real mouse pointer is switched off for this caller` | you asked for `click_method: "global"` on a leg that refuses it | use an element index — it moves no pointer on either platform. (`app_post` also avoids the pointer, but only on macOS; it is refused on Windows) |
| `sent cmd+..., but 'X' is not the frontmost app and macOS may ignore ...` | the Cmd shortcut was sent, but macOS often drops a Cmd shortcut sent to a background app | read the refreshed tree: if nothing changed, the key was ignored, so do the same thing through an element (`computer_click` a menu or toolbar control, `computer_set_value`, `computer_perform_action`), or ask the user to bring the app to the front. Do not retry the key |
| `give either element_index or both x and y, not both forms` | you supplied two different targets in one call | pick one — the element index if the outline has the control |
| `no installed application matches 'X'` | `computer_launch_app` resolves names against the OS's own list of installed apps | try the name as it appears in the Start menu / Applications folder. Do NOT retry with a path — a path is never accepted |
| `'X' already has a window on screen` | the app you asked to launch is already running | call `computer_get_state` on it; launching again would open a second copy |
| `launched X, but no window appeared within 30s` | this is a SUCCESS — the process started and may still be loading | call `computer_list_apps` to check. Do **not** launch it again |
| `'X' matches N installed applications` | an ambiguous name | name one exactly; launching the wrong app is not undoable |
| `unknown drag path 'X'` | a `path` value that is neither `straight` nor `curved` | use one of those — the value is refused rather than straightened, because a straightened stroke is a wrong drawing |
