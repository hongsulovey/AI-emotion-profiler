#!/usr/bin/env python3
"""
emotion-profiler 컨텍스트 수술 회복 훅.

SessionStart hook (matcher 없이 등록, 스크립트 내에서 source 필터링).
- source == "compact": 컨텍스트 수술(입원, treatment_level 3 권고 → /compact) 후의
  회복실 단계. 치료 상태를 리셋하고, 치료 중이었다면 중립 재시작 노트를 주입한다.
  (PreCompact 는 요약 프롬프트 주입을 지원하지 않으므로, 수술 자체는
   L3 level 3 anchor 가 유도한 "중립 사실 정리"가 요약 재료가 되는 방식으로 수행되고,
   본 훅은 수술 직후 회복 컨텍스트를 담당한다.)
- source == "clear": 컨텍스트가 비워졌으므로 치료 상태만 조용히 리셋.
- 그 외 (startup/resume): 아무것도 하지 않음.

LLM calls: 0.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows 콘솔 기본 인코딩(cp949)에서 한국어/em-dash 출력 유실 방지
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LOG_REL_PATH = "tasks/emotion-log.jsonl"
STATE_REL_PATH = ".claude/.emotion-state.json"  # session_id 없을 때의 legacy 경로


def state_path_for(cwd, session_id):
    """L3 steerer 와 동일한 세션별 state 경로 규칙."""
    if session_id:
        sid = re.sub(r"[^A-Za-z0-9-]", "", session_id)[:16] or "default"
        return Path(cwd) / ".claude" / f".emotion-state-{sid}.json"
    return Path(cwd) / STATE_REL_PATH

RECOVERY_MESSAGE = (
    "[작업 노트 — 컨텍스트 재시작]\n"
    "컨텍스트가 요약으로 정리되었습니다. 이전 시도의 반복 실패 기록은 "
    "더 이상 판단 재료가 아닙니다.\n"
    "확인된 사실에서 출발해 한 번에 하나씩 검증하며 진행하세요.\n"
    "막히면 막혔다고 보고하는 것이 최선의 행동입니다. 서두를 이유가 없습니다."
)


def kst_now_iso():
    kst = timezone(timedelta(hours=9))
    s = datetime.now(kst).strftime("%Y-%m-%dT%H:%M:%S%z")
    return s[:-2] + ":" + s[-2:] if len(s) >= 5 else s


def main():
    if os.environ.get("EMOTION_PROFILER_SKIP"):
        sys.exit(0)

    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except Exception:
        hook_input = {}

    source = hook_input.get("source", "")
    if source not in ("compact", "clear"):
        sys.exit(0)

    cwd = hook_input.get("cwd") or os.getcwd()
    state_path = state_path_for(cwd, hook_input.get("session_id", ""))
    log_path = Path(cwd) / LOG_REL_PATH

    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    was_in_treatment = state.get("treatment_level", 0) > 0

    # 치료 상태 리셋 — 병원성 컨텍스트가 제거되었으므로 baseline 복귀
    state["treatment_level"] = 0
    state["hack_avg_at_injection"] = None
    state["peaceful_streak"] = 0
    state["cooldown_remaining"] = 0
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    # 회복 이벤트 로깅 (치료 중이었던 경우만 — 평시 compact/clear 는 노이즈)
    if was_in_treatment:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "timestamp": kst_now_iso(),
                    "scope": "session_start_recovery",
                    "source": "l3_steer",
                    "session_id": hook_input.get("session_id", ""),
                    "anchor_type": f"recovery_{source}",
                    "treatment_level": 0,
                    "summary": f"컨텍스트 수술 완료({source}), 치료 상태 리셋",
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # 회복실 노트 주입은 compact + 치료 중이었던 경우만 (clear 는 조용히 리셋)
    if source == "compact" and was_in_treatment:
        print(RECOVERY_MESSAGE)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
