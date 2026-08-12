"""One-shot backfill: write channel_name to session metadata for all Slack sessions.

Reads session_map.json for channel IDs, calls Slack API to resolve names,
writes to session metadata files. Run once then delete.

Usage:
    python scripts/backfill_channel_names.py
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kiro_crew.config.loader import config_dir
from kiro_crew.history import ConversationLog


async def main():
    data_dir = config_dir()
    session_map_path = data_dir / "session_map.json"
    
    if not session_map_path.exists():
        print("No session_map.json found")
        return

    sm = json.loads(session_map_path.read_text(encoding="utf-8"))
    
    # Collect unique non-DM channel IDs from Slack sessions
    slack_sessions: dict[str, str] = {}  # session_key → channel_id
    for key, entry in sm.items():
        if not key.startswith("slack:"):
            continue
        ch = entry.get("slack_channel_id", "")
        if ch and not ch.startswith("D"):
            slack_sessions[key] = ch

    print(f"Found {len(slack_sessions)} Slack channel sessions (non-DM)")
    if not slack_sessions:
        return

    unique_channels = set(slack_sessions.values())
    print(f"Unique channel IDs: {unique_channels}")

    # Resolve channel names via Slack API
    from slack_sdk.web.async_client import AsyncWebClient
    import os
    
    # Load .env manually (no dotenv dependency needed)
    env_path = data_dir / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("No SLACK_BOT_TOKEN in .env")
        return

    client = AsyncWebClient(token=token)
    
    # Resolve names
    channel_names: dict[str, str] = {}
    for ch_id in unique_channels:
        try:
            resp = await client.conversations_info(channel=ch_id)
            name = resp.data.get("channel", {}).get("name", "")
            if name:
                channel_names[ch_id] = name
                print(f"  {ch_id} → #{name}")
        except Exception as e:
            print(f"  {ch_id} → ERROR: {e}")

    if not channel_names:
        print("No channel names resolved")
        return

    # Write to session metadata — MUST run outside asyncio event loop
    # because ConversationLog._locked checks for loop discipline.
    # So we collect results here and write after asyncio.run() exits.
    global _channel_names_result, _slack_sessions_result
    _channel_names_result = channel_names
    _slack_sessions_result = slack_sessions


_channel_names_result = {}
_slack_sessions_result = {}

if __name__ == "__main__":
    asyncio.run(main())
    
    # Now write metadata synchronously (outside event loop)
    from kiro_crew.history import ConversationLog
    data_dir = config_dir()
    log = ConversationLog(data_dir / 'sessions')
    updated = 0
    for session_key, ch_id in _slack_sessions_result.items():
        name = _channel_names_result.get(ch_id, "")
        if not name:
            continue
        try:
            log.update_metadata(session_key, {"channel_name": name})
            updated += 1
        except Exception as e:
            print(f"  Failed to update {session_key}: {e}")

    print(f"\n✓ Updated {updated} session(s) with channel_name")
    
    # Also write the resolver cache
    import time
    cache_path = data_dir / "slack-channels.cache.json"
    cache = {"names": _channel_names_result, "fetched_at": time.time()}
    cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    print(f"✓ Wrote channel name cache ({len(_channel_names_result)} entries)")
