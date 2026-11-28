# YOLO and chat trust survive every gateway exit the user did not choose

Decided by: @bolichen97 (maintainer)
Date: 2026-11-18

## Decision

An ad-hoc YOLO grant and an owner's Trust or Trust reads on a chat stay on across
every gateway exit the user did not choose, and end only on a stop the user chose.

- Kept: every restart (the dashboard or desktop Restart, Slack `/kirocrew restart`,
  `kirocrew restart`, the re-exec after an update), a crash, a watchdog or OOM kill,
  a host reboot, and a stop a service manager sends that did not come from Kiro Crew.
- Ended: `kirocrew stop`, the desktop Quit, and Ctrl+C on a foreground
  `kirocrew gateway`. Each writes a user-stop mark before the process goes, and the
  next boot restores nothing.
- A timed YOLO grant keeps its original wall-clock expiry and is never extended. An
  `until_shutdown` grant lasts until the user stops Kiro Crew; its label says so.
- A restore is bounded by the policy ceiling in force at that boot, and every restore
  and every clear is audited as the manual change is.
- The record a grant comes back from is signed, lives where the agent sandbox cannot
  reach, and fails closed.

## Why

The maintainer's rulings, verbatim:

1. "我要求 yolo 如果是 设置成 reset after gateway exit 但是 exit 不是因为用户主动退出而是自动更新或者 gateway 崩溃 应该保持 yolo, trust 也是 不能 gateway 重启所有 session 都回归 normal 了"
   (If YOLO is set to reset after gateway exit, but the exit was not the user quitting
   on purpose, an automatic update or a gateway crash, YOLO must be kept. Trust too. A
   gateway restart must not put every session back to Normal.)
2. "主机重启也保持"
   (A host reboot keeps them too.)
3. "新开一个 PR 全部覆盖"
   (Open one new PR that covers all of it.)

On a fresh Linux VM before this change, a planned restart, a `kill -9` relaunched by
the supervisor, and `kirocrew stop` followed by a start each turned YOLO off and put
every trusted chat back to Normal, so an unattended run stalled on an approval nobody
was there to give.

## Evidence

- The maintainer's ruling on issue 12275:
  https://github.com/kirodotdev/KiroCrew/issues/12275#issuecomment-6974288653
