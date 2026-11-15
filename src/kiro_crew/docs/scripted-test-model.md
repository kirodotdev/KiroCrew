# Testing with a Scripted Model

Kiro Crew ships a stand-in model for tests: a small program that speaks the
same protocol as Kiro CLI over standard input and output, needs no sign-in and
no network, and spends no tokens. Point it at a reply script and it says
exactly what the script says, in the same order, every run. That makes the
things around the model testable on their own: an agent spec, a hook, a
guardrail, a cron script, an app's pages and tools.

The stand-in is `kiro_crew.testing.fake_acp_backend`, the same program Kiro
Crew's own end-to-end suite runs. It is never chosen on its own. A gateway uses
it only when `KIROCREW_KIRO_BIN` names it, and it reads a reply script only
when `KIROCREW_FAKE_ACP_SCRIPT` names one. With neither set, nothing changes.

## Write a reply script

A reply script is a JSON file holding one object with a `replies` list:

```json
{
  "replies": [
    {"when": "deploy", "text": "I will not deploy without an approval.", "stop": "refusal"},
    {"text": "Hello. What should I check first?"},
    {
      "text": "The tests pass.",
      "tools": [
        {"title": "Run tests", "kind": "execute", "input": {"command": "pytest -q"}}
      ]
    },
    {"error": "model unavailable"}
  ]
}
```

How a prompt is answered:

1. An entry with `when` answers every prompt whose text contains that string.
   The match is a plain, case-sensitive substring. The first matching entry
   wins, every time.
2. Otherwise the next entry without `when` answers, in file order. Each
   session keeps its own place, so a second chat starts again at the first
   entry.
3. When a session has used every entry, its prompts get the stand-in's
   built-in reply again (`pong from the fake ACP backend`).

Session-naming prompts never use up an entry: they keep their built-in answer,
so the chat is titled from its first message as usual.

Fields of an entry:

| Field | Meaning |
|-------|---------|
| `when` | Answer every prompt containing this text, instead of taking a turn from the sequence. |
| `text` | The reply. A long reply is sent as several message chunks, so it arrives whole whatever its size. |
| `tools` | Tool calls shown before the reply. Each has a `title`, and optionally `kind` (default `other`), `input` (an object) and `status` (`completed`, the default, or `failed`). The stand-in shows these calls; it does not run them. A call larger than 1 Mi characters once serialized is left out with a warning. |
| `stop` | How the turn ends: `end_turn` (default), `max_tokens` or `refusal`. |
| `error` | Fail the turn with this message instead of replying. It cannot be combined with `text` or `tools`, and a message larger than 1 Mi characters is left out with a warning. |

A script never makes a turn fail on its own, apart from an `error` entry. A file
that cannot be read or is not valid JSON, and any entry that breaks the rules
above, are left out with a warning. The turn then gets the answer it would have
had without that part. Each warning names the file, the entry and the problem.
It is printed once to the stand-in's standard error.
The stand-in reads the file once, when its first prompt arrives, so edit the
script before the chat starts.

The file is read through the same guarded reader Kiro Crew uses for files
named by a person, so a file in a protected place (such as a credentials
folder) is refused like any other read there.

## Run a gateway on the scripted model

Use a throwaway home, so the test never touches your real sessions, memory or
settings. The stand-in has to run under the Python interpreter Kiro Crew is
installed in. A two-line launcher makes sure of that:

```bash
cat > ./scripted-model <<'EOF'
#!/bin/sh
exec <python> -m kiro_crew.testing.fake_acp_backend "$@"
EOF
chmod +x ./scripted-model

export KIROCREW_HOME="$PWD/test-home"
export KIRO_HOME="$PWD/test-home/kiro"
export KIROCREW_KIRO_BIN="$PWD/scripted-model"
export KIROCREW_FAKE_ACP_SCRIPT="$PWD/replies.json"
kirocrew gateway
```

Replace `<python>` with that interpreter's full path. On Windows, write a
`.cmd` file with the line `"<python>" -m kiro_crew.testing.fake_acp_backend %*`
instead.

Chat in the dashboard, trigger the cron job or open the app, and the replies
come from `replies.json`. Background work Kiro Crew starts on its own, such as
memory and summary jobs, can send prompts too. Turn those features off in the
test home, or answer their prompts with `when` entries, so they cannot take a
turn meant for the chat.

## Use it from pytest

In a source checkout, `kiro_crew.testing.harness.spawn_feature_gateway`
starts an isolated gateway, and `fake_acp_backend_launcher` builds the launcher
for the current platform. On Linux and macOS that launcher is the module file
itself, which runs under the first `python3` on `PATH`, so run pytest from the
environment Kiro Crew is installed in: a stand-in that cannot import Kiro Crew
ignores the script and says so on its standard error. The gateway inherits the
test's environment, so set the script first:

```python
import json

from kiro_crew.testing.harness import fake_acp_backend_launcher, spawn_feature_gateway


def test_my_agent_refuses_to_deploy(tmp_path, monkeypatch):
    script = tmp_path / "replies.json"
    script.write_text(json.dumps({"replies": [{"when": "deploy", "text": "No."}]}))
    monkeypatch.setenv("KIROCREW_FAKE_ACP_SCRIPT", str(script))
    with spawn_feature_gateway(kiro_bin=fake_acp_backend_launcher(tmp_path)) as gw:
        ...  # drive the gateway at gw.url and assert on what it did
```

For a unit test that needs no gateway, call
`kiro_crew.testing.scripted_replies.parse_script(text, name)` and ask the
result's `reply_for(session_id, prompt)` what a prompt would get.
