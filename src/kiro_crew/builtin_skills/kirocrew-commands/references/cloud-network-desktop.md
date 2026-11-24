# Cloud, tailnet and computer-use commands

Moved verbatim from the kirocrew-commands `SKILL.md` core.

## Cloud (Bring-Your-Own AWS)

Runs Kiro Crew on an EC2 instance in **your own** AWS account; credentials are
resolved by the `aws` CLI and never stored by Kiro Crew. All verbs accept
`--profile` / `--region`; the single-instance verbs also accept `--tag`
(defaults to the last launched instance).

| Command | Description |
|---------|-------------|
| `kirocrew cloud doctor` | Check cloud prerequisites + AWS reachability |
| `kirocrew cloud launch` | Provision + configure an instance (interactive) |
| `kirocrew cloud launch --size TIER -y` | Non-interactive launch at a size tier |
| `kirocrew cloud launch --new` | Create a separate new instance instead of resuming the saved one |
| `kirocrew cloud launch --keep-on-failure` | On bootstrap failure keep the instance for inspection |
| `kirocrew cloud list` | List your Kiro Crew cloud instances |
| `kirocrew cloud status` | Show one instance's state |
| `kirocrew cloud connect` | Open the dashboard over an SSM tunnel |
| `kirocrew cloud tunnel` | Open the dashboard SSM tunnel (standalone alias of connect) |
| `kirocrew cloud connect --local-port N --no-browser` | Forward to a specific local port, no browser |
| `kirocrew cloud login` | Sign kiro-cli in on the instance (fixes "not logged in" chat errors) |
| `kirocrew cloud logout` | Sign kiro-cli out on the instance, to switch Kiro account |
| `kirocrew cloud stop` | Stop the instance (pause billing) |
| `kirocrew cloud start` | Start a stopped instance |
| `kirocrew cloud destroy` | Remove the instance and ALL its AWS resources |
| `kirocrew cloud destroy --dry-run` | Show the delete command without running it |
| `kirocrew cloud iam-policy` | Print the least-privilege IAM policy to apply. Too big for an inline user policy: create it as a customer-managed policy and attach it to the user, group or role (or Identity Center permission set) behind the launch profile |
| `kirocrew cloud iam-boundary` | Pre-create the immutable EC2 + Fargate permissions boundaries (admin, one-time) |

## Tailnet (Tailscale)

Publishes this dashboard on your tailnet and trusts its origin, so a device on the
tailnet reaches it without a public tunnel.

| Command | Description |
|---------|-------------|
| `kirocrew tailnet status` | Show whether the dashboard is published and trusted on your tailnet |
| `kirocrew tailnet up` | Publish the dashboard on your tailnet and trust its origin |
| `kirocrew tailnet down` | Stop publishing the dashboard on your tailnet |
| `... --port N` | Name the dashboard port; `up` needs it whenever discovery has no verified port |

`up` publishes only a port it has evidence for: an explicit `--port`, `KIROCREW_PORT`,
or the running gateway's run marker. With none of those — the gateway is down, the
marker is unreadable, or several gateways are up, where the marker deliberately
refuses — `up` refuses rather than falling back to the configured `dashboard.url`
port, because nothing is verified to answer there and `tailscale serve` would expose
whatever does. Start the gateway and re-run, or name the port yourself. `status` and
`down` do accept the configured port: one only reports, and the other checks mount
ownership before removing anything.

## Computer Use (Desktop Automation)

Default-OFF behind a keystone enable (`~/.kiro/crew/computer_use.json`, **not**
`config.json`). macOS and Windows carry the full tool set; Linux answers a typed
unsupported refusal. On Windows there is no per-process input, so a keystroke takes
the user's keyboard focus and a coordinate click moves their real cursor — say so
rather than reporting a silent success. These are human debug/diagnostic twins of the
`computer_*` MCP tools — the agent uses the MCP tools, not these.

| Command | Description |
|---------|-------------|
| `kirocrew computer doctor` | Report platform support, keystone enable state, and the advisory Accessibility / Screen Recording probe |
| `kirocrew computer doctor --json` | Same as JSON |
| `kirocrew computer apps` | List on-screen applications the accessibility layer can address |
| `kirocrew computer call TOOL k=v …` | Run ONE computer-use tool through the same gated chokepoint the agent uses |
| `kirocrew computer call --calls '[…]'` | Run a JSON array of calls in a SINGLE process, so `element_index` values stay resolvable |
