# Updates never restart a running gateway

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

No path applies an update to, or restarts, a gateway that is running. That holds
for every unattended path (the scheduler, voluntary and mandatory, the stale-asset
watchdog, any idle gate, settle or drain window) and for every clicked path.

- A click on Update ARMS the update (`POST /api/update {"when": "next-start"}`
  answers `202 {"armed": true, "version": ...}`; `{"when": "cancel"}` disarms it).
  It never runs an installer, an `apply_command` or a git update, and never
  restarts anything. The label is "Update when I restart".
- The apply runs at the NEXT START, at service boot before the listeners bind and
  before any session is restored, when an update is armed and the installed
  version differs from the armed one: the policy `apply_command`, the managed
  venv or the git route, then a bounded, logged re-exec. A failure clears the arm,
  keeps the old version and shows `pending_apply_error`. Nothing runs at shutdown:
  the desktop app signals the gateway on quit with a timeout, which would cut a
  hook. A gateway the desktop app hosts skips the boot apply; the desktop owns it.
- Auto update only prepares: the managed venv builds and promotes beside the
  running install and is left staged ("update ready"). A git checkout or a policy
  command rewrites the running tree in place, so it only notifies, a mandatory
  floor included (the floor keeps its prominent notice and forces nothing).
- The stale-asset watchdog keeps serving and shows a restart notice; it no longer
  exits the gateway.
- A user-clicked Restart (the dashboard and desktop Restart buttons) is still a
  user restart: it applies an armed update, after a warning when live work would be
  cut. Declining that warning is a quiet cancel. Slack `/kirocrew restart`,
  `!restart` and the CLI `kirocrew restart` are explicit commands and stay.

## Why

The maintainer's rulings of 2026-11-10, verbatim:

1. 10:55 "auto update不能在gateway还在运行的时候更新，得gateway关掉或者重启的时候启动更新流程，App内部只显示有更新了"
   (auto update must not update while the gateway is still running; the update
   flow starts when the gateway is shut down or restarted; inside the app, only
   show that an update is available).
2. 13:05 "我们自动更新只遵循一点， 用户主动退出App才Apply更新， 用户的gateway还开着不管有没有对话在不在活跃都不要自己自动重启"
   (automatic update follows one rule only: the update is applied when the user
   quits the app themselves; while the user's gateway is open, never restart it by
   itself, whether or not a conversation is active).
3. 13:55 "点击update now也不应该重启用户的gateway而是得等用户自己重启"
   (clicking update now must not restart the user's gateway either; it waits for
   the user to restart it themselves).
4. 14:00 "然后由于toolbox的机制， 点击update 不能直接开始toolbox update, 得等用户退出当前gateway再启动这个"
   (and because of how Toolbox works, clicking update cannot start the Toolbox
   update directly; it has to wait until the user quits the current gateway, then
   start it).

A restart that nobody asked for cuts running turns, queued messages and approval
cards; the person finds out afterwards, if at all.

## Evidence

- The maintainer's instructions of 2026-11-10, quoted above. Rule 4 of this
  directory asks the maintainer to restate them on the pull request that adds this
  entry, and that comment is the on-record artifact.
  The maintainer's comment on pull request 35088 restates them:
  https://github.com/kirodotdev/KiroCrew/pull/35088#issuecomment-6788259131
