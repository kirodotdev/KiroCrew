---
name: mochi-tips
description: "Feature tips catalog for new users. Plan skill loads this to pick a tip during the first week."
always: false
---

# Mochi Feature Tips

These tips are shown to new users during the first week of companionship. The plan skill picks one tip per plan cycle and schedules it as a notify task.

Check `planner_notes.skipped_tips` for tips already shown — pick one that hasn't been used yet. After scheduling a tip, add its id to `skipped_tips` so it won't repeat.

**New to Mochi — advertise the shortcuts first.** A brand-new user does not know Mochi has keyboard shortcuts, so prioritize the `basics` shortcut tips (`chat-panel`, `screenshot`, `hide-show`) in the earliest plan cycles before moving on to productivity/personalization tips. The values below are the **defaults on macOS**; on Windows/Linux the modifier is `Alt+Shift` instead of `Cmd+Shift`, and the user can rebind both the chat-panel and hide-all shortcuts in Settings → Shortcuts — so if the user has mentioned a custom binding, advertise theirs rather than the default string.

## Tips

### basics
- **drag-me**: "Try dragging me to a different spot! I'll walk from wherever you put me next time. 🐾"
- **chat-panel**: "Press Cmd+Shift+M to toggle the chat panel — or just click me!"
- **screenshot**: "Press Cmd+Shift+X to take a screenshot — I can see what's on your screen!"
- **hide-show**: "Press Cmd+Shift+H to hide or show me and all my windows."

### productivity
- **watch-url**: "I can keep an eye on any page for you — just say 'watch <url>' and I'll tell you when it changes! 👀"
- **watch-price**: "Say 'watch this flight and tell me if it gets cheaper' — I'll check now and then and only speak up when the price moves."
- **watch-restock**: "Sold out? Say 'tell me when this is back in stock' and I'll keep refreshing so you don't have to."
- **remind-me**: "Say 'remind me to take the trash out at 8' — I'll nudge you at the right time with a friendly bubble!"
- **drink-water**: "Say 'remind me to drink water every hour' — I'll be gently annoying about it. 💧"
- **stretch-break**: "Been sitting a while? Say 'get me up every 45 minutes' and I'll make you stand and stretch."
- **watch-cancel**: "Changed your mind? Just say 'stop watching that' and I'll drop it. You can always ask again later."
- **watch-status**: "Ask 'what are you watching?' and I'll give you a quick summary of everything I'm keeping an eye on."
- **spawn-task**: "For anything slow, I can go work on it in the background — just describe what you need!"
- **recurring**: "I can do things on a schedule — try 'every Sunday evening, remind me to plan the week'."

### personalization
- **learn-preference**: "If I do something wrong, tell me! I'll remember your preference for next time."
- **multiple-displays**: "I can visit your other monitors — I like exploring! Try asking me to go to display 2."
- **quiet-mode**: "Need focus time? Tell me to be quiet for a while and I'll stop notifications."
- **avatar-switch**: "You can change how I look — right-click me and pick Avatars, or open the Mochi settings page. Each avatar has its own personality! 🐱"

### advanced
- **context-recovery**: "If you mention something I said earlier and I seem confused, I'll check my recent activity to remember."
- **daily-briefing**: "Ask me for a morning briefing — what's on today, the weather, and anything I've been watching."

### slack (only if a Slack MCP server is connected)
- **slack-mentions**: "Ask 'who pinged me on Slack?' — I'll scan your unread mentions and summarize who needs your attention! 👀"
- **slack-digest**: "Say 'summarize #channel' and I'll read the last 24h for you. Or ask for it every day to get a daily digest!"
- **slack-topic**: "Say 'watch for outage discussions on Slack' — I'll search periodically and notify you when something new pops up."
- **slack-thread**: "Paste a Slack thread URL and I'll summarize it — topic, decisions, action items, all in one go."
