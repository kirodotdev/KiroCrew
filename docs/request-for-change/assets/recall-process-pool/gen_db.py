"""Build a synthetic V1 memory.db the size of the one in kirodotdev/KiroCrew#26600.

54k semantic rows (37k project.*) with 1024-d float32 embeddings, 12k lessons,
27k episodic rows. Words follow a Zipf law so keyword scoring behaves like text.
Usage: gen_db.py <repo_src_dir> <out_dir> [--scale 1.0]
"""

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

src, out = Path(sys.argv[1]), Path(sys.argv[2])
scale = float(sys.argv[sys.argv.index("--scale") + 1]) if "--scale" in sys.argv else 1.0
sys.path.insert(0, str(src))
from kiro_crew.vector_memory import VectorMemoryStore  # noqa: E402

N_SEM, N_PROJ, N_LES, N_EPI, DIM = (int(54000 * scale), int(37000 * scale), int(12000 * scale), int(27000 * scale), 1024)
out.mkdir(parents=True, exist_ok=True)
db_path = out / "memory.db"
if db_path.exists():
    db_path.unlink()
store = VectorMemoryStore(db_path)
store.init()
store.close()

rng = np.random.default_rng(7)
VOCAB = [f"w{i:04d}x" for i in range(6000)]
# Real-looking stems so the stemmer has something to do.
SUFFIX = ["", "s", "ing", "ed", "tion"]
ranks = np.arange(1, len(VOCAB) + 1)
p = 1.0 / ranks
p /= p.sum()


def words(n):
    idx = rng.choice(len(VOCAB), size=n, p=p)
    return " ".join(VOCAB[i] + SUFFIX[j % 5] for j, i in enumerate(idx))


def emb_blob(n):
    m = rng.standard_normal((n, DIM), dtype=np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    return [m[i].tobytes() for i in range(n)]


base = datetime(2026, 5, 1, tzinfo=timezone.utc)


def ts(i, n):
    return (base + timedelta(seconds=int(i * (170 * 86400) / n))).isoformat()


c = sqlite3.connect(db_path)
c.execute("PRAGMA journal_mode=WAL")
rows = []
embs = emb_blob(N_SEM)
for i in range(N_SEM):
    if i < N_PROJ:
        key = f"project.repo{i % 40}.fact{i}"
    elif i % 3 == 0:
        key = f"user.attr{i}"
    else:
        key = f"pref.item{i}"
    val = json.dumps(words(int(rng.integers(5, 40))))
    rows.append((key, val, 0.8, "consolidation", ts(i, N_SEM), ts(i, N_SEM), 0, embs[i]))
c.executemany(
    "INSERT INTO semantic_memory(key,value_json,confidence,source,created_at,updated_at,is_deleted,embedding) VALUES(?,?,?,?,?,?,?,?)",
    rows,
)
del rows, embs
rows = []
embs = emb_blob(N_LES)
for i in range(N_LES):
    rule = words(int(rng.integers(8, 30)))
    neg = words(int(rng.integers(5, 15))) if i % 3 == 0 else None
    val = {"rule": rule, "category": "knowledge", "applies": "on_topic" if i % 2 else "always"}
    if neg:
        val["negative"] = neg
    key = "lesson." + hashlib.md5(rule.encode(), usedforsecurity=False).hexdigest()[:12] + f"{i:x}"
    rows.append((key, json.dumps(val), 0.9, "consolidation", ts(i, N_LES), ts(i, N_LES), 0, embs[i]))
c.executemany(
    "INSERT INTO semantic_memory(key,value_json,confidence,source,created_at,updated_at,is_deleted,embedding) VALUES(?,?,?,?,?,?,?,?)",
    rows,
)
del rows, embs
rows = []
embs = emb_blob(N_EPI)
for i in range(N_EPI):
    tags = json.dumps([]) if i % 2 else json.dumps(["project", f"repo{i % 40}"])
    rows.append((f"epi-{i:06d}", f"conv{i % 500}", words(int(rng.integers(15, 80))), embs[i], tags, 0.5, ts(i, N_EPI), None, 0))
c.executemany(
    "INSERT INTO episodic_memories(id,conversation_id,text,embedding,tags,importance,created_at,last_accessed_at,is_deleted) VALUES(?,?,?,?,?,?,?,?,?)",
    rows,
)
c.commit()
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.close()
print(db_path, db_path.stat().st_size // 1_000_000, "MB", N_SEM, N_LES, N_EPI)
