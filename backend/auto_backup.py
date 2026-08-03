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
    """Commits and pushes the memory snapshot AND the raw signals.db to GitHub.

    signals.db is gitignored (binary), so it's force-added — the raw DB is the
    only fully-restorable copy of every trade, not just the 500-row journal in
    the JSON snapshot. A local commit still succeeds even if the push later
    fails (e.g. no network), so nothing is lost off the VPS disk.
    """
    try:
        export_memory_snapshot()
        repo_dir = Path(__file__).parent.parent
        subprocess.run(["git", "add", "backend/memory_backup.json"], cwd=repo_dir, check=True)
        # -f: signals.db is in .gitignore; force it in so the raw data is saved.
        subprocess.run(["git", "add", "-f", "backend/signals.db"], cwd=repo_dir,
                       capture_output=True, text=True)
        msg = f"data(backup): sync learned memory + signals.db {time.strftime('%Y-%m-%d %H:%M')}"
        res = subprocess.run(["git", "commit", "-m", msg], cwd=repo_dir, capture_output=True, text=True)
        made = res.returncode == 0
        if not made and "nothing to commit" not in (res.stdout + res.stderr).lower():
            print(f"[Backup] commit failed: {res.stdout[:150]} {res.stderr[:150]}")
            return False
        # Force-push to a DEDICATED data branch, never main. The VPS updates its
        # code via raw-file curl (no git pull), so its local branch trails
        # origin/main — a normal `git push` to main would be rejected
        # (non-fast-forward). Mirroring HEAD to its own branch always succeeds
        # and keeps trade data cleanly separate from code history.
        push_res = subprocess.run(
            ["git", "push", "-f", "origin", "HEAD:refs/heads/vps-data-backup"],
            cwd=repo_dir, capture_output=True, text=True)
        if push_res.returncode != 0:
            print(f"[Backup] committed locally but push failed: {push_res.stderr[:200]}")
        return push_res.returncode == 0
    except Exception as e:
        print(f"[Backup] Failed to push memory backup to GitHub: {e}")
    return False


if __name__ == "__main__":
    snap = export_memory_snapshot()
    print(f"Memory snapshot exported: {len(snap['trade_journal'])} trades backed up.")
    success = push_backup_to_github()
    print(f"GitHub Push Status: {'Success' if success else 'No changes / Skipped'}")
