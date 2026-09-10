"""Prepare and apply exact-version source patches; never restart or configure a gateway."""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import importlib.metadata
import json
import os
import shutil
import tempfile
from pathlib import Path

EXPECTED = {
    "subagent.py": "8d6b4ede6bfb02a1c1cb38e1b204fb48f7d4e266b31e09ce54ba4e56ac760a2e",
    "acp/client.py": "5b7198350a1746adb2e2d54c55f42c9393e5509796bd9885128028a04d125254",
    "context.py": "6658f8a00281c215657e15908ffb91d1ccc19312518d4b89b155e414a31aa878",
    "providers/acp.py": "2133d6ec383588aa992f0ce22588e85bbb882d879def8758a06ebc45c5c6ab0a",
    "dashboard/token_auth.py": "19f7eca8b6966fc6ed2c12e1162c84f6d52d21cd7b8a0a4acfaa1fd802d1b577",
    "dashboard/handlers/core.py": "f4862a3a68587ba194729c281e1916886c75e24331ed02679e2147ac7652a39c",
    "apps/spawn_sdk.py": "59c91f01f5b49e4e801f61a2654e4aafa0bf1c2c4f0a5f5e66fdba96f45e240b",
    "apps/context.py": "498dab03000534e2ed118f5d5ecbe46dc8b401e8976df62cfed6616d00fc0fa6",
    "apps/bridges.py": "d086bc846a89e0f67c61b63f1f7a7ebc0582912e1fc731fb04b97c4ea22fdeb0",
    "apps/execution.py": "4f92f520a1fa7e55b39a2273c4f587750783a2a6391cac1842cf89b6b20d6062",
    "dashboard/handlers/security.py": "efeaafcf7ba0f5a717a1fd33c7845210f4dbb9e1676fc063ca8b585e65f65002",
}
TARGETS = ("acp/client.py", "subagent.py")
MODULE_NAME = "ucam_consumer.py"
APP_FILES = ("app.json", "agents/reader.json", "backend/routes.py", "requirements.txt")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def replace_once(source: str, before: str, after: str) -> str:
    if source.count(before) != 1:
        raise RuntimeError("patch_anchor_drift")
    return source.replace(before, after, 1)


def patched(name: str, source: str) -> str:
    if name == "subagent.py":
        source = replace_once(
            source,
            "        use_session_sharing = (not info.keep) and self._should_use_session_sharing(info)",
            "        from kiro_crew.ucam_consumer import consumer_for, synthetic_app\n\n"
            "        use_session_sharing = (\n"
            "            not synthetic_app(info) and (not info.keep) and self._should_use_session_sharing(info)\n"
            "        )",
        )
        source = replace_once(
            source,
            "        named_agent = bool(info.agent and _AGENT_NAME_RE.fullmatch(info.agent))",
            "        ucam_run = await consumer_for(info, is_new, _resumed, is_cc)\n"
            "        named_agent = bool(info.agent and _AGENT_NAME_RE.fullmatch(info.agent))",
        )
        source = replace_once(
            source,
            "        _groups = _context_groups_of(info)",
            "        _groups = frozenset() if ucam_run else _context_groups_of(info)",
        )
        source = replace_once(
            source,
            "            context_groups=_groups,\n        )",
            "            context_groups=_groups,\n"
            '            **({"agent": info.agent, "blocks_reads": True} if ucam_run else {}),\n'
            "        )",
        )
        source = replace_once(
            source,
            "                    async for _ev in client.stream(msg):",
            "                    stream = ucam_run.stream(client, msg) if ucam_run else client.stream(msg)\n"
            "                    async for _ev in stream:",
        )
    elif name == "acp/client.py":
        tree = ast.parse(source)
        methods = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_request"
        ]
        if len(methods) != 1:
            raise RuntimeError("send_method_drift")
        method = methods[0]
        lines = source.splitlines(keepends=True)
        before = "".join(lines[method.lineno - 1 : method.end_lineno])
        after = replace_once(
            before,
            "        if not self._process or not self._process.stdin:",
            "        from kiro_crew.ucam_consumer import before_prompt\n\n"
            "        if not self._process or not self._process.stdin:",
        )
        after = replace_once(
            after,
            "        req_id = self._next_req_id()",
            "        params, ucam_receipt = await before_prompt(self, method, params)\n"
            "        req_id = self._next_req_id()",
        )
        after = replace_once(
            after,
            "            self._process.stdin.write(data.encode())",
            "            encoded = data.encode()\n"
            "            if ucam_receipt is not None:\n"
            "                ucam_receipt.check_lease()\n"
            "            self._process.stdin.write(encoded)",
        )
        after = replace_once(
            after,
            "        return req_id",
            "        if ucam_receipt is not None:\n"
            "            await ucam_receipt.after_send()\n"
            "        return req_id",
        )
        source = replace_once(source, before, after)
    else:
        raise RuntimeError("unsupported_patch_target")
    ast.parse(source)
    return source


def prepare(source_root: Path, module: Path, bundle: Path):
    originals = {name: (source_root / name).read_bytes() for name in TARGETS}
    if any(digest(originals[name]) != EXPECTED[name] for name in TARGETS):
        raise RuntimeError("deployed_preimage_drift")
    module_bytes = module.read_bytes()
    ast.parse(module_bytes)
    generated = {name: patched(name, originals[name].decode()).encode() for name in TARGETS}
    manifest = {
        "schema": "ucam-kirocrew-patch/1",
        "expected": EXPECTED,
        "postimages": {name: digest(data) for name, data in generated.items()},
        "module_digest": digest(module_bytes),
        "driver_digest": digest(Path(__file__).read_bytes()),
        "app_digests": {
            name: digest((Path(__file__).parent / name).read_bytes()) for name in APP_FILES
        },
    }
    bundle.mkdir(parents=True, exist_ok=False)
    for name in TARGETS:
        output = bundle / "patched" / name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(generated[name])
        diff = "".join(
            difflib.unified_diff(
                originals[name].decode().splitlines(keepends=True),
                generated[name].decode().splitlines(keepends=True),
                fromfile="a/" + name,
                tofile="b/" + name,
            )
        )
        (bundle / (name.replace("/", "-") + ".patch")).write_text(diff)
    shutil.copy2(module, bundle / MODULE_NAME)
    for name in APP_FILES:
        target = bundle / "app" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(__file__).parent / name, target)
    shutil.copy2(Path(__file__), bundle / "deploy.py")
    plan = json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
    (bundle / "plan.json").write_bytes(plan)
    return digest(plan)


def load_plan(bundle: Path, approval: str):
    raw = (bundle / "plan.json").read_bytes()
    if digest(raw) != approval:
        raise RuntimeError("plan_approval_mismatch")
    plan = json.loads(raw)
    if plan["schema"] != "ucam-kirocrew-patch/1" or plan["expected"] != EXPECTED:
        raise RuntimeError("plan_contract_mismatch")
    if set(plan["postimages"]) != set(TARGETS):
        raise RuntimeError("plan_target_mismatch")
    if digest(Path(__file__).read_bytes()) != plan["driver_digest"]:
        raise RuntimeError("driver_bundle_drift")
    if set(plan["app_digests"]) != set(APP_FILES):
        raise RuntimeError("app_bundle_drift")
    for name in APP_FILES:
        if digest((bundle / "app" / name).read_bytes()) != plan["app_digests"][name]:
            raise RuntimeError("app_bundle_drift")
    if digest((bundle / MODULE_NAME).read_bytes()) != plan["module_digest"]:
        raise RuntimeError("module_bundle_drift")
    for name in TARGETS:
        if digest((bundle / "patched" / name).read_bytes()) != plan["postimages"][name]:
            raise RuntimeError("patch_bundle_drift")
    return plan


def check_sources(root: Path, hashes):
    for name, expected in hashes.items():
        path = root / name
        if path.is_symlink() or digest(path.read_bytes()) != expected:
            raise RuntimeError("live_source_drift:" + name)


def preflight(root: Path, bundle: Path, approval: str):
    plan = load_plan(bundle, approval)
    check_sources(root, EXPECTED)
    if (root / MODULE_NAME).exists():
        raise RuntimeError("addon_module_already_exists")
    if importlib.metadata.version("kirocrew") != "0.2.0":
        raise RuntimeError("distribution_version_drift")
    for name, version in (("botocore", "1.43.91"), ("aiohttp", "3.14.3")):
        if importlib.metadata.version(name) != version:
            raise RuntimeError("dependency_version_drift:" + name)
    return plan


def atomic_copy(source: Path, destination: Path):
    descriptor, temporary = tempfile.mkstemp(prefix=".ucam-", dir=destination.parent)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply(root: Path, bundle: Path, approval: str):
    plan = preflight(root, bundle, approval)
    backup = bundle / "backup"
    backup.mkdir(exist_ok=False)
    for name in TARGETS:
        target = backup / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target)
    check_sources(root, EXPECTED)
    atomic_copy(bundle / MODULE_NAME, root / MODULE_NAME)
    applied = []
    try:
        for name in TARGETS:
            check_sources(root, {name: EXPECTED[name]})
            original = (root / name).read_text()
            if digest(patched(name, original).encode()) != plan["postimages"][name]:
                raise RuntimeError("derived_patch_drift")
            atomic_copy(bundle / "patched" / name, root / name)
            applied.append(name)
        check_sources(root, plan["postimages"])
    except Exception:
        for name in reversed(applied):
            check_sources(root, {name: plan["postimages"][name]})
            atomic_copy(backup / name, root / name)
        raise


def rollback(root: Path, bundle: Path, approval: str):
    plan = load_plan(bundle, approval)
    check_sources(root, plan["postimages"])
    check_sources(bundle / "backup", {name: EXPECTED[name] for name in TARGETS})
    for name in reversed(TARGETS):
        check_sources(root, {name: plan["postimages"][name]})
        atomic_copy(bundle / "backup" / name, root / name)
    check_sources(root, {name: EXPECTED[name] for name in TARGETS})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "preflight", "apply", "rollback"))
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--module", type=Path)
    parser.add_argument("--approved-plan-sha", default="")
    args = parser.parse_args()
    if args.operation == "prepare":
        if args.module is None:
            parser.error("prepare requires --module")
        print(prepare(args.source_root, args.module, args.bundle))
    else:
        globals()[args.operation](args.source_root, args.bundle, args.approved_plan_sha)
        print(json.dumps({"status": args.operation + "_complete", "restarted": False}))


if __name__ == "__main__":
    main()
