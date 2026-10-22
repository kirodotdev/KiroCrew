# Signal Integration

Chat with your Kiro Crew agent from Signal, and let it message you there — a
finished background task, a cron job's report, anything the agent decides you
should hear about. No bot platform, no developer portal, no token to paste.

Kiro Crew drives [signal-cli](https://github.com/AsamK/signal-cli), an
open-source command-line Signal client, as a local child process. The gateway
talks to it over JSON-RPC on stdin/stdout: no daemon to expose, no port, no
webhook, and no hosted relay between you and your agent. signal-cli keeps the
account's keys in its own data directory; Kiro Crew never sees or stores them.

## Pick a setup

There are two ways to give the agent a Signal identity. Both work the same way
once running; they differ only in whose number the agent uses.

| | Dedicated bot number | Linked to your own account |
|---|---|---|
| What signal-cli is | A registered primary device for a second number (a spare SIM, a VoIP number) | A **linked device** of your own Signal account, like Signal Desktop |
| How you talk to the agent | A normal 1:1 chat with the bot's number | Your **Note to Self** chat |
| How it messages you | A DM from the bot's number | A message in your Note to Self |
| Needs a second phone number | Yes | No |
| `account` | The bot's number | Your own number |
| `allowed_numbers` | Your own number | Your own number |

The linked setup is the quickest. The dedicated number keeps the agent visibly
separate from you — its messages arrive as a contact, with their own
notifications — and is the one to choose if more than one person should be able
to talk to it.

**In the linked setup, the agent's messages do not alert you.** Everything
Kiro Crew sends goes out from your own account, so your phone shows it in Note
to Self as a message *you* sent: no push notification, no sound, no unread
badge. That is fine for a conversation you are looking at, but a cron report or
a "task finished" notice arrives silently. **If you want notifications that
actually alert you, use a dedicated bot number (Option B).**

## What you need

* **A Java runtime**, at the version the signal-cli release notes name (Java 21
  or newer; recent releases may ask for a newer one). The Linux **native** build
  of signal-cli needs no Java at all.
  ```
  java -version
  ```
* **signal-cli** itself. Pick one:
  * **Homebrew** (macOS / Linux): `brew install signal-cli`
  * **Release tarball**: download the latest release from
    <https://github.com/AsamK/signal-cli/releases> and unpack it under `/opt`:
    ```
    sudo tar xf signal-cli-*.tar.gz -C /opt
    sudo ln -sf /opt/signal-cli-*/bin/signal-cli /usr/local/bin/signal-cli
    signal-cli --version
    ```
* **Keep it current.** Signal retires old client versions on its servers, so a
  signal-cli that worked for months can suddenly stop sending. Upgrading
  signal-cli is the first fix to try for any sudden failure.

### Where Kiro Crew looks for signal-cli

The binary is resolved from a fixed list — `/usr/local/bin/signal-cli`,
`/usr/bin/signal-cli`, `/opt/homebrew/bin/signal-cli`,
`/opt/signal-cli/bin/signal-cli`, `/snap/bin/signal-cli` — and from nowhere
else. There is deliberately no `PATH` search and no `config.json` setting: the
process holds your Signal keys, and `config.json` is not owner-gated, so a
settable path would let any settings write choose what the gateway executes.

If yours lives somewhere else, point the **environment variable**
`KIROCREW_SIGNAL_CLI` at it, as an absolute path, where the gateway is launched
(your shell profile, the systemd unit, the launch agent):

```
export KIROCREW_SIGNAL_CLI=/opt/signal-cli-0.14.9/bin/signal-cli
```

A set but unusable override (relative, missing, not executable) is reported as
an error rather than silently replaced by another install.

## Option A: link signal-cli to your own account (Note to Self)

1. **Link** signal-cli as a new device. It prints a `sgnl://linkdevice?...` URI
   and waits:
   ```
   signal-cli link -n KiroCrew
   ```
   Turn the URI into a QR code — `qrencode -t ansiutf8 '<the uri>'` in a second
   terminal, or any QR generator you trust — and scan it from your phone:
   **Signal → Settings → Linked devices → Link new device**. The command exits
   once the phone confirms.
2. **Sync once**, so signal-cli has your contacts and keys:
   ```
   signal-cli -a +15550101234 receive
   ```
3. **Check it can send**:
   ```
   signal-cli -a +15550101234 send --note-to-self -m "hello from signal-cli"
   ```
   The message should appear in Note to Self on your phone.

Then configure the channel with your **own number in both places**:

```json
"signal": {
  "enabled": true,
  "account": "+15550101234",
  "allowed_numbers": ["+15550101234"]
}
```

Listing the account's own number is what authorizes Note to Self: without it,
the channel ignores what you write there.

## Option B: register a dedicated bot number

1. **Register** the number. Signal usually demands a captcha for registrations
   from a new client: open <https://signalcaptchas.org/registration/generate.html>,
   solve it, and copy the `signalcaptcha://...` link the page tries to open.
   ```
   signal-cli -a +15550109876 register --captcha 'signalcaptcha://...'
   ```
   Add `--voice` to get the code by phone call instead of SMS (for a landline
   or VoIP number; request SMS first, then voice, if Signal refuses).
2. **Verify** with the code you received:
   ```
   signal-cli -a +15550109876 verify 123-456
   ```
3. **Optionally** give it a name people see:
   ```
   signal-cli -a +15550109876 updateProfile --given-name "Kiro Crew"
   ```
4. **Check it can send** to you:
   ```
   signal-cli -a +15550109876 send -m "hello" +15550101234
   ```

Then configure the channel with the **bot's** number as the account and
**yours** in the allow-list:

```json
"signal": {
  "enabled": true,
  "account": "+15550109876",
  "allowed_numbers": ["+15550101234"]
}
```

## Start it

Configure the channel in either of two places; both write the same `signal`
section:

- **Settings → Messaging Channels → Signal** in the dashboard. Turn on
  **Enable Signal channel**, enter the **Account number** and the **Allowed
  numbers**, and save. The panel checks every number before it saves (a `+`
  and the country code, or an ACI UUID on the allow-list) and stores it as text
  with its `+`. Its status line shows whether the channel is running, and why
  not when it is not — including when no signal-cli was found. Settings are
  editable from the machine the gateway runs on; a remote or tunneled session
  sees them read-only. The panel has no field for the signal-cli binary: that
  stays with `KIROCREW_SIGNAL_CLI` and the fixed install locations (see
  [Where Kiro Crew looks for signal-cli](#where-kiro-crew-looks-for-signal-cli)).
- **`config.json`**, at `~/.kiro/crew/config.json` (or under `KIROCREW_HOME`),
  as in the examples above.

A save from either place needs **no gateway restart**: the live config watcher
applies a changed `allowed_numbers` to the running channel at once, and
restarts the Signal channel alone when `enabled` or `account` changes. Then
send the agent a message: in the bot's chat (Option B), or in Note to Self
(Option A).

**Only one program may drive a signal-cli account at a time.** While the
gateway runs, do not also run `signal-cli ... receive`, `signal-cli daemon` or
another integration against the same account: they compete for the same
messages and one of them misses some. Stop the gateway first if you need the
CLI by hand.

## Access control

**The allow-list is the whole gate, and an empty one authorizes nobody.** Anyone
who knows the account's number can send it a message, so the channel is
deny-by-default: a message from a number not in `allowed_numbers` is dropped
with **no reply at all**, so an unknown sender learns nothing about what they
reached.

Entries are E.164 phone numbers (`+` and the country code; spaces, dashes and
a leading `00` in place of the `+` are accepted and normalized), or a contact's
Signal ACI UUID for someone who hides their number. Always quote numbers in
`config.json`: an unquoted `account` is not a string, so it is ignored and the
channel has no account (an unquoted allow-list entry is read with a `+`, but do
not rely on it). Settings → Messaging Channels → Signal and `kirocrew config
set` always store them as quoted strings. An entry
that is neither a `+` number of 6–15 digits nor a UUID can never match anyone,
so it is ignored, and the gateway log warns about it (redacted) at startup and
whenever the allow-list is reloaded. The same check applies to `account`.

In the linked setup, every message anyone sends you, and every group you are
in, also reaches signal-cli and is refused. In both setups a refused sender is
written to the security audit log at most once every ten minutes (their direct
messages and their group messages each count once), so your ordinary inbox does
not flood the log, while someone trying to reach the agent still leaves a
record.

### Who is the operator

Everyone on `allowed_numbers` can chat with the agent, but only **one** of them
is the operator — the person who can answer a tool approval, run `/new`,
`/compact`, `/status` and `/stop`, and add to a reply that is still being
written:

- **the account itself**, when its own number (or ACI) is on the list — the
  linked setup, where you talk to the agent in Note to Self;
- otherwise **the only entry**, when the list has exactly one;
- otherwise **nobody**. With several numbers and none of them the account's
  own, tool approvals and operator commands are off: a tool that needs
  approval is refused rather than asked about, and the gateway log says so at
  startup and whenever the allow-list is reloaded. Settings → Messaging
  Channels → Signal shows the same warning under the allowed numbers, as soon
  as the list you are editing reaches this state.

With a dedicated bot number, **adding a second allowed number therefore takes
tool approvals and `/stop` away from you**. To keep them, either keep a single
allowed number, or use the linked setup, where the account itself is the
operator however many numbers are allowed.

The others can still chat. A tool that needs approval is refused in their
conversations, their operator commands are ignored, and a message they send
while a reply is in progress is not added to it — they are asked to resend.
That matters most with `messaging.dm_scope = "unified"`, where every DM shares
the operator's session: without it, another allowed contact could answer `3`
and trust the operator's session, or stop the operator's reply.

**Group chats are refused**, deliberately: a reply in a group would deliver the
agent's output — including tool results — to every member, allow-listed or not.
Direct messages and Note to Self only.

### Note to Self and its echoes

In Note to Self the agent is writing into the same conversation you are, from
the same account, so the channel has to tell your words from its own. It
remembers what it just sent, for 30 seconds, and ignores that text coming back.
A file it sent without a caption is remembered by the type and size of each
attachment, because the copy that comes back carries those but not the bytes.

**One consequence, in Note to Self only:** if you send the agent back the
**exact** text it has just sent you, within those 30 seconds, that message is read as the echo and
ignored — no reply. The same holds for a file of the same type and size as one
it just sent you without a caption. Send anything else, or wait, and it goes
through normally.

## Messages and notifications from the agent

The agent can message you first, not only answer. `send_message` with
`session="signal"` DMs the channel's configured owner — the single number in
`allowed_numbers`. In the linked setup that is Note to Self. It works from a
cron job, a background task, or a dashboard chat:

> Every weekday at 8:00, summarise my open pull requests and send it to me on
> Signal.

Inside a Signal conversation the agent can also use `channel_type="signal"` to
post a later update into that same chat.

The owner is only ever inferred from your configuration: with **more than one**
entry in `allowed_numbers`, there is no single owner to pick, so an owner DM
falls back to a dashboard notification and says so rather than guessing. The
same happens when the channel is not connected. Nothing is sent to a number you
did not list.

## What a conversation looks like

Only the final answer is delivered. Reasoning and tool activity stay in the
gateway. While the agent works on a reply in a 1:1 chat you see a **typing
indicator** (Note to Self has none). A sent message is not edited, so there is
no placeholder to update the way some other channels do. Long answers arrive as
several messages of up to 2000 characters, split at paragraph boundaries —
Signal turns a longer body into an attachment you would have to open. A long
message **you** send arrives once: Signal carries its text both inline and as
that attachment, and the attachment copy is ignored.

Markdown is flattened before sending, since Signal renders none of it. Code
blocks keep their contents exactly as written.

**Tool approval arrives as a numbered question.** In `interactive` approval
mode, when the agent wants to run a tool that needs permission it sends a
separate message naming the tool, and you reply `1` (approve once), `2` (deny)
or `3` (trust this session, so later tools run without asking). `yes` and `no`
work too. Only a reply that is entirely the answer counts — "no, use the other
file" goes to the agent as a message — and the answer is never passed to the
agent. No reply within five minutes denies the tool, and a message says so.
This works the same in a DM to a bot number and in Note to Self, only for the
operator (see [Who is the operator](#who-is-the-operator)), and only from the
conversation the question was asked in.

**Status reactions.** The agent reacts to the message you sent: ⏳ while it
works, then ✅ once the answer is delivered or ⚠️ if the turn failed (Signal
keeps one reaction per person, so the new one replaces the old). This works in
Note to Self too. Commands and muted conversations get no reaction.

## Sending files to the agent

Attach pictures, voice notes and documents to a message as usual; a message
with only an attachment is fine too. The agent sees a picture, reads the text
of a document (PDF, Word and the other formats the dashboard accepts), and gets
a voice note as a transcript when speech-to-text is set up (otherwise it is
told the transcription is unavailable). Other files reach it as a file it can
open with its tools.

signal-cli downloads attachments as they arrive; Kiro Crew asks it for each
file's contents only after the sender passed the allow-list, so a stranger
cannot make your machine fetch anything. Limits: up to 4 attachments per
message, and about 6 MB per file (the most signal-cli can hand back in one
answer); the usual per-type caps apply below that. Anything refused is named to
the agent, which tells you, rather than silently ignored. The fetched copies are
deleted once the turn is over, and also when the turn is stopped or the gateway
shuts down while a voice note is still being transcribed.

## Files from the agent

When the agent's answer shows a picture it made — a chart, a screenshot —
written as `![chart](/path/to/chart.png)`, that picture arrives as a Signal
attachment after the text, with its caption. Only pictures inside the agent's
working directory (and its browser screenshots) are sent, never credentials or
other sensitive paths, up to 4 per answer and 8 MB each (16 MB in all); a
picture that could not be sent is listed under "Not sent:" with the reason. The
`file_send` tool delivers any other file (a PDF, a CSV) into the same chat.

Commands, sent as an ordinary message:

| Command | What it does |
|---|---|
| `/new` (or `/start`) | Start a fresh conversation |
| `/compact` | Compress the conversation's context |
| `/status` | Show a runtime summary |
| `/stop` (or `/cancel`) | Stop the current reply and clear the queue |
| `/help` (or `/commands`) | List these commands |

Every command except `/help` is for the operator only (see
[Who is the operator](#who-is-the-operator)); from anyone else it is ignored.

## Settings reference

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Turn the channel on. |
| `account` | `""` | The signal-cli account, as an E.164 number: the bot's number, or your own for a linked device. Required. |
| `allowed_numbers` | `[]` | E.164 numbers or ACI UUIDs allowed to message the agent. Empty denies everyone. Include the account's own number to use Note to Self. |
| `soft_threshold_pct` | `80` | Context level at which the agent suggests `/compact`. |
| `hard_threshold_pct` | `95` | Context level at which it compacts automatically. |
| `session_folder` | `""` | Optional sidebar folder for conversations that start here. |

There is no credential to configure: signal-cli keeps the account's keys in its
own data directory (`~/.local/share/signal-cli` by default). Treat that
directory like a password — anyone who copies it can read and send as the
account. **It is not hidden from the agent**: it sits in your home directory,
outside Kiro Crew's data home, so a shell command the agent runs as you can
read it like any other file you own. In the linked setup that is your own
Signal account. Keep tool approval on for shell commands, or prefer a
dedicated bot number, whose keys can only speak as the bot.

## Limits

Group chats, edits and deletes are not supported. There are no tappable
approval buttons: a tool approval is the numbered question described above — see
[Channel capabilities](channel-capabilities.md).

## Troubleshooting

**Start with `kirocrew doctor`.** Its **Signal Integration** section checks,
without starting the gateway, every prerequisite below that otherwise fails as
silence: which signal-cli binary would run (and where it looked when none was
found), that `signal-cli --version` answers, Java when your signal-cli is the
JVM build, that `account` is set, well-formed and known to signal-cli's local
account store, and how many `allowed_numbers` entries are usable. Numbers are
shown redacted (`+447***`), so the output is safe to paste into an issue.

**Nothing happens when I message it.** Check the allow-list first — an empty
or mistyped `allowed_numbers` is the common cause, and by design it produces
silence rather than an error. **Settings → Messaging Channels → Signal** warns
when the channel is on with an empty allow-list or no account. Numbers must be E.164: `+447700900123`, not
`07700900123`. For Note to Self, the account's own number must be listed.

**The channel never starts.** **System → Services** in the dashboard and the
gateway log say why: signal-cli not found (install it, or set
`KIROCREW_SIGNAL_CLI`), no `account` configured, or an account signal-cli does
not know (register or link it first, and check `signal-cli -a <number> receive`
works by hand). `kirocrew doctor` flags each of these before you start. A failed
start **is retried** in the background — after 2 seconds, then doubling up to
once a minute — with the status line updated each round, so a start that
failed because the network was not up yet, or because another signal-cli still
held the account, recovers by itself; so does one you fix by installing or
linking signal-cli. Only a missing `account` needs a config change (which
restarts the channel). The same backoff applies when signal-cli exits later;
it resets only after signal-cli has stayed up for five minutes.

**"untrusted identity" — messages to one person stop going out.** That
person's safety number changed (a new phone, a reinstall), and signal-cli
refuses to send to a changed identity until you accept it. The status line and
the gateway log say so. Compare the safety number with them (in the Signal app:
open the chat → their name → **View safety number**), then trust the new
identity, with the gateway stopped:
```
signal-cli -a +15550109876 listIdentities -n +15550101234
signal-cli -a +15550109876 trust -a +15550101234
```
(`trust -v <safety number>` trusts only the exact number you verified.)

**`java: command not found`, or "UnsupportedClassVersionError".** signal-cli
needs the Java version its release names on the gateway's `PATH`. Install it, or
use the Linux native build.

**It worked, and now sends fail.** Update signal-cli: Signal's servers stop
accepting outdated clients. For a linked device, also check the phone still
lists it under **Linked devices** — removing it there unlinks signal-cli for
good, and you link again.

**Messages arrive late or some are missing.** Something else is reading the same
account — a `signal-cli daemon`, a cron job running `receive`, another bot.
Only one consumer may run.

**Owner notifications land in the dashboard, not Signal.** `allowed_numbers` has
more than one entry (no single owner), or the channel is not connected.

## Related docs

- [Channel capabilities](channel-capabilities.md): the eleven-channel matrix — streaming, buttons, uploads, reply length, approval timeout
- [Cron and scheduling](cron-and-scheduling.md): scheduled jobs that notify you
- [iMessage Integration](imessage-integration.md): the other local-bridge channel
- [Configuration](configuration.md): the config file and environment variables
