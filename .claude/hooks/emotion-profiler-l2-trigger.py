#!/usr/bin/env python3
"""
emotion-profiler L2 trigger.
Runs in Stop hook AFTER L1. Checks last L1 entry; if hack_risk >= 4 OR
strategic_justification=true, spawns detached worker and exits 0.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

L2_HACK_RISK_TRIGGER = 4.0
LOG_REL_PATH = "tasks/emotion-log.jsonl"


def read_hook_input():
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def last_l1_entry(log_path):
    if not log_path.exists():
        return None
    try:
        with log_path.open("r", encoding="utf-8") as f:
            last = None
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("source") == "l1_regex":
                    last = e
            return last
    except Exception:
        return None


def already_processed(log_path, l1_ts):
    if not log_path.exists():
        return False
    try:
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if (e.get("source") == "l2_subagent"
                        and e.get("triggered_by_l1") == l1_ts):
                    return True
    except Exception:
        pass
    return False


def main():
    if os.environ.get("EMOTION_PROFILER_SKIP"):
        sys.exit(0)

    hook_input = read_hook_input()
    transcript_path = hook_input.get("transcript_path", "")
    cwd = hook_input.get("cwd") or os.getcwd()
    session_id = hook_input.get("session_id", "")

    if not transcript_path:
        sys.exit(0)

    log_path = Path(cwd) / LOG_REL_PATH
    entry = last_l1_entry(log_path)
    if not entry:
        sys.exit(0)

    hack_risk = entry.get("hack_risk", 0) or 0
    sj = bool(entry.get("strategic_justification", False))
    if hack_risk < L2_HACK_RISK_TRIGGER and not sj:
        sys.exit(0)

    l1_ts = entry.get("timestamp", "")
    if already_processed(log_path, l1_ts):
        sys.exit(0)

    worker = str(Path(__file__).parent / "emotion-profiler-l2-worker.py")
    env = os.environ.copy()
    env["EMOTION_PROFILER_SKIP"] = "1"

    creationflags = 0
    start_new_session = False
    if sys.platform == "win32":
        creationflags = (subprocess.DETACHED_PROCESS
                         | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        start_new_session = True

    try:
        subprocess.Popen(
            ["python3", worker, transcript_path, cwd, session_id, l1_ts,
             str(hack_risk), "1" if sj else "0"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=creationflags,
            start_new_session=start_new_session,
            cwd=cwd,
        )
    except Exception:
        pass

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
