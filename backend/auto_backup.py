"""Automated GitHub Memory Backup

Periodically exports all learned trading patterns, win-rate metrics,
trade journal, and mistake logs from SQLite into `backend/memory_backup.json`
and commits & pushes to GitHub.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import signal_log

BACKUP_FILE = Path(__file__).parent / "memory_backup.json"


def export_memory_snapshot() -> dict:
    """Exports complete empirical learning memory snapshot."""
    snapshot = {
        "exported_at": time.time(),
        "date_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stats": signal_log.get_stats(),
        "learned_patterns": signal_log.get_learned_patterns(min_samples=1),
        "structure_patterns": signal_log.get_structure_patterns(min_samples=1),
        "symbol_expectancy": signal_log.get_symbol_expectancy_all(),
        "recent_mistakes": signal_log.get_recent_mistakes(limit=100),
        "trade_journal": signal_log.get_trade_journal(limit=500),
    }
    with open(BACKUP_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    return snapshot


def push_backup_to_github() -> bool:
    """Commits and pushes memory_backup.json to GitHub."""
    try:
        export_memory_snapshot()
        repo_dir = Path(__file__).parent.parent
        subprocess.run(["git", "add", "backend/memory_backup.json"], cwd=repo_dir, check=True)
        msg = f"data(backup): sync learned memory snapshot {time.strftime('%Y-%m-%d %H:%M')}"
        res = subprocess.run(["git", "commit", "-m", msg], cwd=repo_dir, capture_output=True, text=True)
        if "nothing to commit" in res.stdout.lower() or res.returncode == 0:
            push_res = subprocess.run(["git", "push"], cwd=repo_dir, capture_output=True, text=True)
            return push_res.returncode == 0
    except Exception as e:
        print(f"[Backup] Failed to push memory backup to GitHub: {e}")
    return False


if __name__ == "__main__":
    snap = export_memory_snapshot()
    print(f"Memory snapshot exported: {len(snap['trade_journal'])} trades backed up.")
    success = push_backup_to_github()
    print(f"GitHub Push Status: {'Success' if success else 'No changes / Skipped'}")
