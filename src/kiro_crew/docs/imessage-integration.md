# iMessage Integration

Chat with your Kiro Crew agent from the Messages app you already use — from your
iPhone, your iPad, your Watch, or the Mac itself. No bot to register, no
developer portal, no token to paste.

This is the only channel where the transport is your own device and your own
account. Kiro Crew talks to Messages.app locally, so nothing about the
conversation is relayed through a third party. That is the point of the channel,
not a footnote: hosted services exist that will hand you an iMessage-capable
number and let any server talk to it over an API, and this integration
deliberately does not use one.

## What you need

* **macOS 14 or newer**, signed in to Messages.
* **The gateway running on that same Mac.** This is a hard requirement, not a
  preference — see [Why the gateway must run here](#why-the-gateway-must-run-here).
* **The `imsg` bridge**, a small open-source CLI:
  ```
  brew install steipete/tap/imsg
  imsg --version
  ```
* **Two macOS permissions**, granted once:
  * **Full Disk Access** — so the process can read the Messages database.
  * **Automation → Messages** — so it can send. The first send prompts for this.

  Both grants are recorded **per process**, against whatever launched the
  gateway. If you run the gateway as a launch agent, grant them to that
  context; a grant given to Terminal does not carry over.

## Quick start

1. **Install the bridge** and confirm it runs (above).
2. **Turn the channel on** in `~/.kiro/crew/config.json`. Your own handle is the
   allow-list — a phone number or the email on your Apple Account:
   ```json
   "imessage": {
     "enabled": true,
     "allowed_handles": ["+15551234567"]
   }
   ```
3. **Restart the gateway**, then send it a message and say hi. Messaging your own
   handle from another device works — see [Messaging yourself](#messaging-yourself).

If the gateway is not on your Mac, or `imsg` is missing, the channel reports why
in **Settings → Channels → iMessage** instead of failing silently.

## Who can reach the agent

**The allow-list is the whole gate, and an empty one authorizes nobody.** Every
other channel has an org or workspace boundary in front of it; iMessage has
none — anyone who knows your number can send to it. So the channel is
deny-by-default, and a message from an unlisted handle is dropped with **no
reply at all**, so an unknown sender learns nothing about what they reached.

Formatting is ignored when handles are compared, so `+1 (555) 123-4567` and
`+15551234567` are the same handle.

**Group chats are refused**, and this is deliberate: a reply in a group would
deliver the agent's output — including tool results — to everyone in the thread,
allow-listed or not. Direct messages only.

### Messaging yourself

Listing your **own** handle and messaging it from another device is supported,
and it is the most convenient setup: no second number, no second Apple Account.
It is also the one chat where the agent is talking to its own identity, so the
channel needs a way to tell your words from its own.

It does not use the platform's own outbound flag alone for that. In a self-chat
every message belongs to your account in both directions, and Messages writes
that attribution asynchronously — the bridge's watch waits 500ms expressly so a
correction can land — so a reply can come back looking exactly like something you
typed. The channel instead remembers what it just sent, for 30 seconds, and
ignores that coming back. Without it the agent answers its own reply and the
conversation never stops.

**One consequence, and it applies to every chat rather than only this one:** if
you send the agent back the **exact** text it has just sent you, within those 30
seconds, that message is read as the echo and ignored — no reply, and nothing
said about it. Send anything else, or wait, and it goes through normally. The
alternative is worse: the only way to tell a returning message from the agent's
own is the platform's attribution, and trusting that is what produced the loop.

## What a conversation looks like

Only the final answer is delivered. Reasoning and tool activity stay in the
gateway: a phone is a poor place to read a tool log, and an iMessage cannot be
taken back once sent.

While the agent works you see a **typing indicator**. That is the only progress
signal iMessage offers — a sent message cannot be edited, so there is no
placeholder to update the way the other channels do. Long answers arrive as
several messages, split at paragraph boundaries.

Markdown is flattened before sending, since Messages renders none of it. Code
blocks are the exception: their contents pass through exactly as written, so
what you copy out of the message is what the agent wrote.

Commands, sent as an ordinary message:

| Command | What it does |
|---|---|
| `/new` | Start a fresh conversation |
| `/compact` | Compress the conversation's context |
| `/help` | List these commands |

## Settings

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turn the channel on. |
| `allowed_handles` | `[]` | Phone numbers / Apple Account emails allowed to message the agent. Empty denies everyone. |
| `cli_path` | `imsg` | Path to the bridge binary. Use an absolute path when the gateway runs as a launch agent, whose `PATH` has no Homebrew. |
| `db_path` | `""` | Override the Messages database location. Empty uses the default. |
| `service` | `imessage` | Which service replies use: `imessage`, `sms`, or `auto` to fall back to SMS. |
| `soft_threshold_pct` | `80` | Context level at which the agent suggests `/compact`. |
| `hard_threshold_pct` | `95` | Context level at which it compacts automatically. |
| `session_folder` | `""` | Optional sidebar folder for conversations that start here. |

There is no credential to configure — that is the whole idea.

## Why the gateway must run here

A gateway running elsewhere could be pointed at a wrapper that reaches your Mac
over SSH, and it would even appear to work: it can read chats and process
incoming messages. **Sends would fail.** macOS records the Automation grant
against the process that asks for it — which in that setup is the remote-shell
server, something the system exposes no way to grant. So the channel would
receive fine and answer nothing.

Rather than ship a send path that cannot be made to work, the channel refuses to
start off-Mac and says so.

## Not in this version

Group chats, attachments in either direction, and every kind of message
mutation: tapbacks, edit, unsend, effects, polls, and group management. Those
last ones need a helper injected into Messages.app, which requires System
Integrity Protection to be disabled for the whole system. Asking you to turn off
SIP to talk to your own agent is not a reasonable default, so this version does
not.

## Troubleshooting

**Nothing happens when I message it.** Check the allow-list first — an empty or
mistyped `allowed_handles` is the common cause, and by design it produces
silence rather than an error. **Settings → Channels → iMessage** shows whether
the channel is connected and why not.

**It answers its own messages / the conversation never stops.** A self-chat where
the echo guard is not doing its job — [Messaging yourself](#messaging-yourself)
explains the mechanism. Turn the channel off in **Settings → Channels →
iMessage** to stop it immediately (every cycle is a real turn), and please report
it: the log records the handle, redacted, the first time it suppresses an echo.

**"Messages database unavailable".** Full Disk Access is missing for the process
running the gateway. Grant it, then quit and relaunch — macOS only re-reads that
permission at launch.

**Sends fail but messages arrive.** Automation → Messages has not been granted,
or the gateway is not running on the Messages host.

**The channel never starts and the log says it requires macOS.** Expected on
Linux or Windows; there is no iMessage there to reach.
