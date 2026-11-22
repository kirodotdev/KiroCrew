# Settings shows the Kiro sign-in choices in place; chat keeps the Sign in button and dialog

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-11-19

## Decision

Signed out, Settings > Kiro account shows the sign-in choices and the device-code step in its card, with no Sign in button and no dialog; the chat's signed-out spots keep one line, the Sign in button and the shared dialog, and both places render the same shared sign-in content.

## Why

- The owner's words: "Settings 里直接放登录方式，聊天里保持弹框, 反正都是reusable的" (put the sign-in choices straight into Settings, keep the dialog in chat; it is all reusable anyway).
- Settings > Kiro account exists only for this account, so the choices fit there; in chat the dialog keeps the person on their conversation.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/39373#issuecomment-6998685060 -- Zezhen's owner decision restated on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/39373 -- the pull request that adds this entry and implements it.
- https://github.com/kirodotdev/KiroCrew/issues/39363 -- the issue.
