#!/usr/bin/env python3
"""
Claude Daemon Backup / Restore CLI
====================================
Manage conversation backups and list history.

Usage:
  python3 backup.py backup [label]
  python3 backup.py restore <path>
  python3 backup.py list
  python3 backup.py prune [keep=30]
  python3 backup.py info
"""

import sys
import json
import gzip
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import storage


def cmd_backup(args):
    label = args[0] if args else None
    path = storage.backup_to_json(label=label)
    count = storage.message_count()
    print(f"✓ Backed up {count} messages → {path}")


def cmd_restore(args):
    if not args:
        print("Usage: backup.py restore <path>")
        sys.exit(1)
    path = args[0]
    confirm = input(f"This will REPLACE the current conversation with {path}.\nContinue? [y/N] ")
    if confirm.lower() != "y":
        print("Aborted.")
        return
    count = storage.restore_from_json(path)
    print(f"✓ Restored {count} messages from {path}")
    print("  Restart the daemon to pick up the restored context.")


def cmd_list(args):
    backups = storage.list_backups()
    if not backups:
        print("No backups found.")
        return
    print(f"{'ID':<5} {'Date':<22} {'Messages':<10} {'Label/Path'}")
    print("-" * 70)
    for b in backups:
        path = Path(b["path"]).name
        print(f"{b['id']:<5} {b['created_at']:<22} {b['message_count']:<10} {path}")


def cmd_prune(args):
    keep = int(args[0]) if args else 30
    storage.prune_backups(keep=keep)
    print(f"✓ Pruned backups, keeping last {keep}.")


def cmd_info(args):
    count = storage.message_count()
    backups = storage.list_backups()
    print(f"Messages in conversation: {count}")
    print(f"Backups on disk:          {len(backups)}")
    if backups:
        latest = backups[0]
        print(f"Latest backup:            {latest['created_at']}  ({latest['message_count']} msgs)")


COMMANDS = {
    "backup":  cmd_backup,
    "restore": cmd_restore,
    "list":    cmd_list,
    "prune":   cmd_prune,
    "info":    cmd_info,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](sys.argv[2:])
