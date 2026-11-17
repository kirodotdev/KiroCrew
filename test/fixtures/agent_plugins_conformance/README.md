# Agent Plugins conformance fixtures

The `plugin.json`, `mcp.json` and `SKILL.md` files of three plugins from the
official [Agent Plugins conformance suite](https://github.com/agentplugins/agent-plugins-conformance),
copied unchanged from commit `6bfce5436435ffed10ab0020ee0f331ddeb640d6`. Each
`SKILL.md` is stored as `SKILL.fixture.md`, so repository-wide scans of shipped
skills do not count these test inputs; the test renames them back.
Licensing is in `LICENSE.md` and `LICENSES/` (Apache-2.0 for configuration,
CC-BY-4.0 for documentation).

Only the files the loader reads are kept. The suite's probe scripts, bundled
binaries and symbolic links are not: `test/test_agent_plugins_conformance.py`
creates stand-in files for the bundled commands and recreates each link exactly
as the suite ships it, so no link has to survive a Git checkout on Windows. There
the directory link is a junction, which needs no privilege.

To refresh, copy the same three file kinds from a newer suite commit, update the
commit above, and fix any test whose expectation the suite changed.
