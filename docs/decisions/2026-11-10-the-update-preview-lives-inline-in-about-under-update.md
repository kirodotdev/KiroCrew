# The update preview lives inline in About under Update

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

The advance notice of the next update is shown inline in Settings > About, in
the Updates card, directly under the update controls, and is never a modal, a
pop-up, a toast or an auto-opened dialog. Its text says the update takes effect
the next time the person restarts Kiro Crew and never names a time it takes
effect by itself.

## Why

- The maintainer's words, verbatim: "更新预告现在就开工 我要求的是不能是弹窗 而是在About Page里Update之下的更新预告".
- On the same day the maintainer ruled how an update lands, verbatim: "我们自动更新只遵循一点， 用户主动退出App才Apply更新， 用户的gateway还开着不管有没有对话在不在活跃都不要自己自动重启".
- And, later that day, verbatim: "点击update now也不应该重启用户的gateway而是得等用户自己重启".
- A notice that asks for no decision has no reason to interrupt, so it waits on
  the page where a person already looks for update information.
- The notice never restarts anything and never advertises a restart, so it
  carries no restart control of its own.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/34863#issuecomment-6781230289:
  the maintainer's comment on the pull request that adds this entry, restating
  that the preview is inline under Update, never a pop-up, and that Update now
  must not restart the gateway.
