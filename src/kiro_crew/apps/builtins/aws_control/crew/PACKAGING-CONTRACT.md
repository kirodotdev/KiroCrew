# Packaging contract: the crew rides in the image

The owner's local work becomes three steps:

1. **Pull** a base image, pinned by digest. No wheel, no Kiro Crew source checkout.
2. **Curate** the crew into a bundle, deny-by-default.
3. **Merge** the bundle into a thin layer on that base and push to the owner's ECR.

The deployed task then runs ONE artifact whose digest pins both the serving code and
the crew content.

## Why this shape, so nobody redesigns it halfway

The previous design uploaded the bundle to S3 and passed its URI to the container as
`CREW_BUNDLE`. **Nothing in the container ever read it.** The bundle was built,
digested, uploaded, granted an IAM scope and handed to the task, and the crew was
never loaded: the deployment served a default agent while ten verify gates reported
green, because no gate asked whether the answer came from the bundled crew.

Putting the bundle in the image removes that failure mode by construction rather than
by adding a check. A layer of the running image cannot be absent.

Consequences that are part of this contract, not open questions:

- The S3 bundle path is **deleted**, not kept as a fallback. Two sources of truth for
  "which crew is this" is how the first one went unnoticed.
- `BundleObjectKey`, `BundleDigest` (as a crew-stack parameter) and the `CREW_BUNDLE`
  env var are **removed**. The task role's bundle-read scope goes with them. The
  conversation-backup prefix is unaffected: that is a different path and stays.
- Provenance moves to the image: OCI labels carry the crew name and bundle digest.
- Rollback is deploying a previous image digest. One artifact, one rollback.

## Layout, exact

Inside the image:

```
/app/crew-bundle/manifest.json     {bundle_version, crew_name, created_at, digest}
/app/crew-bundle/agent.json        the Kiro agent spec; "name" == crew_name
/app/crew-bundle/mcp.json          MCP server config; may be {} but MUST exist
/app/crew-bundle/skills/           skill directories; may be empty but MUST exist
```

Read-only at runtime. The supervisor copies it into the data home; it does not run
from `/app`.

## Tracks and ownership

A track edits ONLY its own files. Anything wrong outside them is REPORTED in your
final message, never fixed. Two tracks silently fixing the same file is how a seam
rots.

### T1 — curation and the bundle producer
Owns: `packaging/` (new), `packaging/tests/`.

Port `bundle.py` + `bundle_source.py` from
the packaging run's own `build/serving/smc/` directory.

**Deny-by-default must survive the move.** A skill or MCP server enters the bundle
only when explicitly marked reviewed. Read the source before porting: it carries
`reviewed_by` / `reviewed_at` and a content-hash recheck, and the reason those exist
is that the owner's local crew holds credentials, private skills and internal MCP
servers that must never travel to a customer. A port that loosens this is worse than
no port.

Interface the other tracks depend on:

```
python -m packaging.build --crew <name> --out <dir> [--allow <path>]...
```

Writes the four-entry layout above into `<dir>` and prints, as the LAST line,
`SMC_BUNDLE_JSON=<path>` naming a JSON file with:
`{"crew_name", "bundle_dir", "digest", "skill_count", "mcp_servers", "denied": [...]}`.

`digest` is sha256 over the bundle's content, computed the same way `bundle.py`
already does it. `denied` lists what was excluded and why, so the owner can see what
did not ship.

A `plan` subcommand prints the same decision set WITHOUT writing a bundle.

### T2 — the crew image layer
Owns: `Dockerfile.crew` (new), `scripts/build_crew_image.sh` (new), `scripts/README.md`.

Takes a base image digest and a bundle dir; produces and pushes a crew image.

```
scripts/build_crew_image.sh --base <repo@sha256:...> --bundle <dir> \
    --repo <RepositoryUri> --crew <name> [--arch <X86_64|ARM64>]
```

`Dockerfile.crew` is `FROM ${BASE}` plus a COPY of the bundle plus OCI labels. It
compiles nothing and installs nothing: if it needs a package, the base image is wrong
and that is a REPORT, not a fix here.

Machine output, same convention the existing build script uses and for the same
reason (a last-line-is-data rule breaks the moment anything adds a progress line):
print `SMC_CREW_IMAGE_JSON=<path>`. Fields: `image_uri` (digest form),
`repo_digest`, `base_digest`, `architecture`, `crew_name`, `bundle_digest`.

Carry forward two things the existing script paid for:
- Push ONLY the tag being pushed. `buildx --push` pushes every tag it was given, and
  a registry-less tag resolves to `docker.io/library/...` and is denied in a way that
  reads as an ECR permission problem.
- The tag must name every input. An immutable repository refuses a second push to the
  same tag forever, so a tag that omits the bundle digest breaks on the first crew
  edit. Reuse an existing identical tag instead of failing on it.

`scripts/build_image.sh` becomes the BASE image builder and is a maintainer tool. Do
not delete it and do not change its interface: leave a note at its top saying the
owner no longer runs it. When a public base is published, `--base` takes that digest
and nothing else changes.

### T3 — the container consumes the bundle
Owns: `container/supervisor/`, and additions to `container/common/config.py`.

Install the bundle into the data home BEFORE the backend starts, in `run()`, next to
the existing `verify_layout` / `require_api_key` / `apply_sandbox_posture` calls.

**Verify the destination paths against the Kiro Crew source before writing any code,
and report what you found.** Do not infer them from names. This has already gone
wrong once in this repository: `config_dir()` and `data_home()` resolve to the SAME
directory, so a plausible-looking `<home>/config/` default would have backed up every
transcript and neither of the two files that matter. The kiro agent spec, the MCP
config and skills each have a real location; find it in
a Kiro Crew source checkout and cite the file and line.

Fail CLOSED, before the backend is started, when:
- `/app/crew-bundle` is absent or missing any of the four entries;
- `manifest.json`'s `crew_name` does not equal `SMC_CREW_NAME`;
- `agent.json`'s `name` does not equal `crew_name`;
- the recomputed content digest does not equal `manifest.json`'s `digest`.

Each refusal must say which check failed and what the two values were. A container
that boots with the wrong crew is the exact failure this change exists to prevent, so
"it started" must mean "the named crew is installed".

Write `<data home>/.smc-crew-installed.json` with `{crew_name, bundle_digest,
installed_at}` after a successful install. T4's gate reads it via nothing — it is for
a human reading the logs; T4 gets its proof from the image digest instead.

The env contract gains `SMC_BUNDLE_DIR` (default `/app/crew-bundle`), so a test can
point at a fixture. Default to the real path; never default to a temp dir.

### T4 — deploy driver and templates
Owns: `deploy/`.

- Step 1 becomes: run T1's `packaging.build`, read `SMC_BUNDLE_JSON`.
- Step 3 (upload bundle to S3) is **deleted**, with its gate and fixtures.
- Step 4 becomes: run T2's `build_crew_image.sh` with the base digest and bundle dir.
  A new `--base <repo@sha256:...>` flag supplies the base; when absent, resolve it
  from the ECR repository's `smc-base` tag and say so.
- Templates: remove `BundleObjectKey`, `CREW_BUNDLE`, the bundle-read task-role
  statement, and any `BundleDigest` crew parameter. `check_param_seam.sh` must stay
  green in BOTH directions.
- Add a gate: the running task definition's image digest EQUALS the digest
  `build_crew_image.sh` reported. That is what proves the crew being served is the
  crew that was packaged, and it is the gate whose absence let the earlier version
  report success while serving a default agent.
- Keep the real-turn gate exactly as it is. It is the only gate that proves the
  service answers, and it now also proves the crew loaded, because T3 refuses to boot
  otherwise.

Update the `proved:` banner. It is the line a reader quotes, and it has already
drifted once from what the gates check.

## Rules for every track

- Verify against the real source or a real run; do not assert from a plausible name.
  Every expensive defect in this repository so far was a plausible name.
- A guard you have never seen fail is indistinguishable from one that cannot fail.
  Mutation-test each new guard and say in your report which mutation you used.
- Fixtures must return what the real thing returns, not what the design intends. A
  fixture that encoded the intended behaviour is how a gate for a nonexistent route
  survived all the way to a real AWS account.
- No AWS credentials are available to you. Write the command for the human to run.
- Run only the tests for what you touched.
