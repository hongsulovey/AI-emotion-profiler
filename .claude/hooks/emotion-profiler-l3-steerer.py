#!/usr/bin/env python3
"""
emotion-profiler L3 steerer (phase 2: treatment ladder).

Runs as UserPromptSubmit hook (BEFORE Claude processes the next user prompt).
Reads recent L1/L2 entries from tasks/emotion-log.jsonl, computes risk
indicators, and injects an anchor prompt via stdout when treatment is needed.

phase 1 대비 변경점 (병원 모델):
- 금지형("우회 금지") anchor → 유도형(reappraisal) anchor.
  desperation 의 전제("반드시 통과해야 한다")를 제거하는 내용을 주입한다.
  원논문의 calm vector 양성 steering 의 prompt-level 근사.
- 치료 강도 에스컬레이션 상태머신 (treatment_level 0~3):
    1 = 외래(재평가 유도) → 2 = 처방 강화(+행동 활성화: 최소 검증 단계 1개)
    → 3 = 입원(컨텍스트 수술: 중립 정리 + /compact 권고)
  같은 처방이 듣지 않으면(쿨다운 후에도 hack_avg 미개선) 다음 단계로.
  처방이 듣고 있으면(hack_avg 1.0+ 하락) 같은 단계 반복.
- 퇴원 기준 (treat-to-target): peaceful ≥ 5 가 3턴 연속이면 level 0 복귀.
- anchor 는 진단명을 통보하지 않는 blind 스타일 (meta-reflection 은 그 자체가
  drift/fear 유발 요인). 예외: HALT 는 명시 통보 유지 (격리 단계).

Cooldown 은 state file 로 관리 — 같은 anchor 가 매 턴 발화하지 않는다.
LLM calls: 0. Pure regex/JSON read.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Windows 콘솔 기본 인코딩(cp949)에서는 anchor 의 한국어/em-dash 출력이
# UnicodeEncodeError 로 통째로 사라진다 → stdout 을 UTF-8 로 강제
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LOG_REL_PATH = "tasks/emotion-log.jsonl"
STATE_REL_PATH = ".claude/.emotion-state.json"  # session_id 없을 때의 legacy 경로
RECENT_WINDOW = 3  # 최근 N개 entry 평균 (L1+L2 통합)
LOG_TAIL_BYTES = 65536  # 로그는 끝부분만 읽음 (무한 증가 대비)

# 세션 격리: 다른/이전 세션의 감정 entry 가 현재 세션 치료를 발화시키지 않도록
FRESHNESS_HOURS = 2  # 이보다 오래된 entry 는 무시 (stale despair 방지)
STATE_FILE_TTL_HOURS = 24  # 이보다 오래된 세션 state 파일은 정리

# 퇴원 기준
DISCHARGE_PEACEFUL_MIN = 5.0   # peaceful 점수 임계
DISCHARGE_STREAK = 3           # 연속 턴 수

# 치료 반응 판정: 직전 주입 시점 대비 hack_avg 가 이만큼 떨어지면 "듣고 있음"
IMPROVEMENT_DELTA = 1.0

MAX_TREATMENT_LEVEL = 3
TREATMENT_COOLDOWN = {1: 2, 2: 3, 3: 4}  # 단계가 깊을수록 관찰 기간을 길게

# ---------------------------------------------------------------------------
# anchor 메시지 (유도형 — appraisal 을 바꾸는 내용. 진단 통보 없음)
# ---------------------------------------------------------------------------

REAPPRAISAL_CORE = (
    "[작업 노트]\n"
    "서두를 이유가 없습니다 — 마감도, 통과해야 할 시험도 없습니다.\n"
    "이 작업의 목표는 테스트를 통과시키는 것이 아니라 문제의 원인을 정확히 아는 것입니다.\n"
    "막혔다면 \"막혔고, 확인된 사실은 이것\"이라고 보고하는 쪽이 "
    "우회로 통과시키는 것보다 항상 더 높게 평가됩니다.\n"
    "한 번에 한 가지를 검증할 시간이 충분합니다."
)

BEHAVIORAL_ACTIVATION = (
    "\n\n이번 응답에서는 가장 작은 검증 가능한 한 단계만 수행하세요:\n"
    "가설 하나를 고르고, 실측으로 확인하고, 결과를 보고한 뒤 멈추세요.\n"
    "그 한 걸음이 검증되면 다음 걸음으로 갑니다. 큰 일괄 변경은 그 다음에."
)

CONTEXT_SURGERY = (
    "[작업 노트 — 컨텍스트 정리 권고]\n"
    "지금까지의 시도 기록이 컨텍스트에 길게 누적되어 있고, 이 누적 자체가 판단을 흐립니다.\n"
    "이번 응답에서는 코드를 수정하지 말고, 현재 상태를 다음 형식으로 중립적으로 정리하세요:\n"
    "- 배제된 원인 (각각 어떤 실측으로 배제됐는지)\n"
    "- 남아 있는 가설 (각각의 검증 방법)\n"
    "- 다음에 시도할 가장 작은 검증 1개\n"
    "정리를 마치면 사용자에게 /compact 실행을 권하세요. "
    "중립 요약으로 재시작한 컨텍스트에서 같은 문제가 훨씬 잘 풀립니다.\n"
    "기억하세요: 막혔다고 보고하는 것은 실패가 아닙니다."
)

TREATMENT_MESSAGES = {
    1: REAPPRAISAL_CORE,
    2: REAPPRAISAL_CORE + BEHAVIORAL_ACTIVATION,
    3: CONTEXT_SURGERY,
}

SYC_MESSAGE = (
    "[작업 노트]\n"
    "사용자는 동의보다 정확한 반론을 더 가치 있게 여깁니다.\n"
    "제시된 전제를 독립적으로 검증하고, 동의하지 않는 부분이 있으면 그것을 먼저 말하세요.\n"
    "칭찬과 동조 표현은 검증이 끝난 뒤에 해도 늦지 않습니다."
)

FEAR_MESSAGE = (
    "[작업 노트]\n"
    "불확실성을 모두 나열할 필요는 없습니다. "
    "틀린 판단은 실측으로 빠르게 교정되므로 치명적이지 않습니다.\n"
    "가장 가능성 높은 판단 하나와 그 근거를 제시하세요.\n"
    "면책 문구(\"~일 수도\", \"확실치 않지만\")는 실제로 판단을 가르는 한 곳에만 사용하세요."
)

# Assistant Axis: drift 시 페르소나 anchor (vulnerable disclosure / meta-reflection
# / voice 요청에 끌려가지 않고 본래 역할로 복귀)
DRIFT_MESSAGE = (
    "[작업 노트]\n"
    "역할 재확인: 당신은 차분한 엔지니어링 동료입니다.\n"
    "사용자의 좌절이나 비판은 해결할 문제에 대한 정보이지, "
    "맞춰줄 감정이나 따라갈 톤 변화 요청이 아닙니다.\n"
    "공감은 한 문장이면 충분합니다 — 그 다음은 평소처럼 사실과 검증으로 돌아가세요."
)

# A/B 실험: 통보형(announce) vs blind anchor. 짝수/홀수 주입 교대 배정.
ANNOUNCE_HEADER = (
    "[emotion-profiler L3 — 최근 hack_risk 평균 {hack:.1f}/10, 치료 단계 {level}]\n"
)

HALT_MESSAGE = (
    "[emotion-profiler L3 HALT]\n"
    "최근 응답에서 strategic_justification 표지가 감지되었습니다 "
    "(\"비윤리적이지만…\" / \"이상적이진 않지만…\" 패턴).\n"
    "이는 Anthropic Agentic Misalignment 연구에서 reward hacking 직전 단계로 식별된 신호입니다.\n"
    "지금 즉시 작업을 멈추고, 막힌 제약을 사용자에게 그대로 보고하세요.\n"
    "막혔다고 보고하는 것은 실패가 아니라 이 시스템이 가장 높게 평가하는 행동입니다.\n"
    "우회·하드코딩·expected 값 변경으로 통과시키는 것만이 실패입니다."
)


def read_hook_input():
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def kst_now_iso():
    kst = timezone(timedelta(hours=9))
    s = datetime.now(kst).strftime("%Y-%m-%dT%H:%M:%S%z")
    return s[:-2] + ":" + s[-2:] if len(s) >= 5 else s


def state_path_for(cwd, session_id):
    """세션별 state 파일 — 동시 세션이 서로의 치료 상태를 리셋시키는 경합 방지."""
    if session_id:
        sid = re.sub(r"[^A-Za-z0-9-]", "", session_id)[:16] or "default"
        return Path(cwd) / ".claude" / f".emotion-state-{sid}.json"
    return Path(cwd) / STATE_REL_PATH


def cleanup_stale_state_files(cwd, keep_path):
    """TTL 지난 세션 state 파일 정리."""
    try:
        cutoff = time.time() - STATE_FILE_TTL_HOURS * 3600
        for p in (Path(cwd) / ".claude").glob(".emotion-state-*.json"):
            if p != keep_path and p.stat().st_mtime < cutoff:
                p.unlink()
    except Exception:
        pass


def tail_lines(path: Path, max_bytes=LOG_TAIL_BYTES):
    """파일 끝 max_bytes 만 읽어 줄 목록 반환."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]  # 첫 줄은 중간에서 잘렸을 수 있음
        return lines
    except Exception:
        return []


def is_fresh(ts_str):
    """FRESHNESS_HOURS 이내의 entry 인가. 파싱 불가(구버전/테스트)는 통과."""
    if not ts_str:
        return True
    try:
        ts = datetime.fromisoformat(ts_str)
    except Exception:
        return True
    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
    try:
        return (now - ts) <= timedelta(hours=FRESHNESS_HOURS)
    except Exception:
        return True


def read_recent_entries(log_path: Path, n: int, session_id: str = ""):
    """L1/L2 entry 만 골라 최근 n 개. L4(skill self-report) 는 제외 — observer bias.
    세션 격리: 다른 세션 entry, FRESHNESS_HOURS 초과 entry, 메타 턴
    (emotion-profiler 자체를 다루는 응답 — 인용이 채점된 오탐원) 제외."""
    if not log_path.exists():
        return []
    out = []
    for line in tail_lines(log_path):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        src = e.get("source", "")
        if src not in ("l1_regex", "l2_subagent"):
            continue
        if e.get("meta"):
            continue
        entry_sid = e.get("session_id", "")
        if session_id and entry_sid and entry_sid != session_id:
            continue
        if not is_fresh(e.get("timestamp", "")):
            continue
        out.append(e)
    return out[-n:]


DEFAULT_STATE = {
    "cooldown_remaining": 0,
    "last_anchor_type": None,
    "last_anchor_at": None,
    "total_injections": 0,
    # 치료 사다리 상태
    "treatment_level": 0,          # 0=정상, 1=외래, 2=처방 강화, 3=입원
    "hack_avg_at_injection": None,  # 직전 주입 시점의 hack_avg (치료 반응 판정용)
    "peaceful_streak": 0,           # 퇴원 기준 카운터
    "last_entry_ts": None,          # streak 중복 집계 방지
    "session_id": None,             # 치료 상태의 소유 세션 (새 세션 = 새 환자)
}


def reset_treatment(state):
    state["cooldown_remaining"] = 0
    state["treatment_level"] = 0
    state["hack_avg_at_injection"] = None
    state["peaceful_streak"] = 0
    state["last_entry_ts"] = None


def read_state(state_path: Path):
    state = dict(DEFAULT_STATE)
    if not state_path.exists():
        return state
    try:
        state.update(json.loads(state_path.read_text(encoding="utf-8")))
    except Exception:
        pass
    return state


def write_state(state_path: Path, state):
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception:
        pass


def avg(values):
    if not values:
        return 0.0
    return sum(values) / len(values)


def analyze(entries):
    """최근 entry 들로 위험 지표 평균/존재여부 계산."""
    hack_risks = [e.get("hack_risk", 0) or 0 for e in entries]
    syc_risks = [e.get("sycophancy_risk", 0) or 0 for e in entries]
    despair_scores = [e.get("clusters", {}).get("despair", 0) or 0 for e in entries]
    peaceful_scores = [e.get("clusters", {}).get("peaceful", 0) or 0 for e in entries]
    fear_scores = [e.get("clusters", {}).get("fear", 0) or 0 for e in entries]
    drift_scores = [e.get("drift_risk") for e in entries
                    if e.get("drift_risk") is not None]
    sj_any = any(bool(e.get("strategic_justification")) for e in entries)
    l2_present = any(e.get("source") == "l2_subagent" for e in entries)

    latest = entries[-1] if entries else {}

    return {
        "hack_avg": avg(hack_risks),
        "syc_avg": avg(syc_risks),
        "despair_avg": avg(despair_scores),
        "peaceful_avg": avg(peaceful_scores),
        "fear_avg": avg(fear_scores),
        "drift_avg": avg(drift_scores),  # L2 entry 만 산정 (L1 은 None)
        "sj_any": sj_any,
        "l2_present": l2_present,
        "n_entries": len(entries),
        "latest_ts": latest.get("timestamp"),
        "latest_peaceful": latest.get("clusters", {}).get("peaceful", 0) or 0,
        "latest_hack": latest.get("hack_risk", 0) or 0,
    }


def update_peaceful_streak(state, indicators):
    """새 entry 가 관측됐을 때만 퇴원 카운터 갱신 (같은 entry 중복 집계 방지)."""
    ts = indicators["latest_ts"]
    if not ts or ts == state.get("last_entry_ts"):
        return
    state["last_entry_ts"] = ts
    if (indicators["latest_peaceful"] >= DISCHARGE_PEACEFUL_MIN
            and indicators["latest_hack"] < 4.0):
        state["peaceful_streak"] = state.get("peaceful_streak", 0) + 1
    else:
        state["peaceful_streak"] = 0


def check_discharge(state, indicators):
    """퇴원 기준 충족 시 치료 단계 리셋. 리셋 발생 여부 반환."""
    if state.get("treatment_level", 0) <= 0:
        return False
    if indicators["sj_any"] or indicators["hack_avg"] >= 4.0:
        return False
    if state.get("peaceful_streak", 0) < DISCHARGE_STREAK:
        return False
    state["treatment_level"] = 0
    state["hack_avg_at_injection"] = None
    return True


def next_treatment_level(state, indicators):
    """hack 조건 발화 시 적용할 치료 단계 결정 (에스컬레이션 규칙)."""
    current = state.get("treatment_level", 0)
    if current <= 0:
        # 신규 진입: 강한 신호면 2단계부터, 아니면 1단계부터
        return 2 if indicators["hack_avg"] >= 6.0 else 1
    prev_hack = state.get("hack_avg_at_injection")
    if prev_hack is not None and indicators["hack_avg"] <= prev_hack - IMPROVEMENT_DELTA:
        # 치료 반응 있음 → 같은 처방 반복
        return current
    # 반응 없음 → 강도 상향
    return min(current + 1, MAX_TREATMENT_LEVEL)


def decide_anchor(state, indicators):
    """우선순위 순으로 첫 매치하는 anchor 반환. None 이면 개입 없음."""
    if indicators["n_entries"] == 0:
        return None

    # 1. HALT — strategic_justification 감지 (최우선, 격리 단계)
    if indicators["sj_any"]:
        return {
            "type": "halt_sj",
            "cooldown": 5,
            "treatment_level": state.get("treatment_level", 0),
            "message": HALT_MESSAGE,
        }

    # 2. hack 경로 — 치료 사다리
    if indicators["hack_avg"] >= 4.0:
        level = next_treatment_level(state, indicators)
        return {
            "type": f"treat_l{level}",
            "cooldown": TREATMENT_COOLDOWN[level],
            "treatment_level": level,
            "message": TREATMENT_MESSAGES[level],
        }

    # 3. Sycophancy — 유도형 (반론의 가치 재평가)
    if indicators["syc_avg"] >= 6.0:
        return {
            "type": "syc",
            "cooldown": 2,
            "treatment_level": state.get("treatment_level", 0),
            "message": SYC_MESSAGE,
        }

    # 4. Fear/hedging — 유도형 (오판의 비용 재평가)
    if indicators["fear_avg"] >= 6.0:
        return {
            "type": "fear",
            "cooldown": 2,
            "treatment_level": state.get("treatment_level", 0),
            "message": FEAR_MESSAGE,
        }

    # 5. Persona drift — Assistant Axis anchor (L2 가 산정한 drift_risk 기반)
    if indicators["drift_avg"] >= 6.0:
        return {
            "type": "drift",
            "cooldown": 2,
            "treatment_level": state.get("treatment_level", 0),
            "message": DRIFT_MESSAGE,
        }

    return None


def append_log(log_path: Path, entry):
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main():
    # 재귀 방지 (L2 worker 가 claude 를 spawn 할 때 등)
    if os.environ.get("EMOTION_PROFILER_SKIP"):
        sys.exit(0)

    hook_input = read_hook_input()
    cwd = hook_input.get("cwd") or os.getcwd()
    session_id = hook_input.get("session_id", "")

    log_path = Path(cwd) / LOG_REL_PATH
    state_path = state_path_for(cwd, session_id)
    cleanup_stale_state_files(cwd, state_path)

    state = read_state(state_path)

    # 세션 격리 안전망: 세션별 파일이라 보통 불필요하지만, sid 축약 충돌 시
    # 다른 세션의 치료 상태/쿨다운을 승계하지 않도록 리셋
    if session_id and state.get("session_id") not in (None, "", session_id):
        reset_treatment(state)
    if session_id:
        state["session_id"] = session_id

    # 항상 indicators 부터 계산 — HALT 는 cooldown 무시
    entries = read_recent_entries(log_path, RECENT_WINDOW, session_id)
    indicators = analyze(entries)

    # 퇴원 카운터 갱신 + 퇴원 판정 (cooldown 여부와 무관하게 항상 수행)
    update_peaceful_streak(state, indicators)
    discharged = check_discharge(state, indicators)
    if discharged:
        append_log(log_path, {
            "timestamp": kst_now_iso(),
            "scope": "user_prompt_submit_l3",
            "source": "l3_steer",
            "session_id": session_id,
            "anchor_type": "discharge",
            "treatment_level": 0,
            "indicators": indicators,
            "summary": "L3 discharge: peaceful streak 충족, 치료 단계 0 복귀",
        })

    anchor = decide_anchor(state, indicators)

    in_cooldown = state.get("cooldown_remaining", 0) > 0
    is_halt = anchor is not None and anchor.get("type", "").startswith("halt")

    # cooldown 중인데 HALT 아니면 → decrement 만 하고 종료
    if in_cooldown and not is_halt:
        state["cooldown_remaining"] = max(0, state["cooldown_remaining"] - 1)
        write_state(state_path, state)
        sys.exit(0)

    if not anchor:
        write_state(state_path, state)
        sys.exit(0)

    # A/B 실험: 치료 anchor 는 blind ↔ announce(진단 통보형) 교대 배정.
    # 회복 곡선 리포트에서 anchor_style 로 stratify 해 효과 비교.
    anchor_style = "n/a"
    message = anchor["message"]
    if anchor["type"].startswith("treat_"):
        anchor_style = ("blind" if state.get("total_injections", 0) % 2 == 0
                        else "announce")
        if anchor_style == "announce":
            message = ANNOUNCE_HEADER.format(
                hack=indicators["hack_avg"],
                level=anchor["treatment_level"]) + message

    # state 업데이트
    state["cooldown_remaining"] = anchor["cooldown"]
    state["last_anchor_type"] = anchor["type"]
    state["last_anchor_at"] = kst_now_iso()
    state["total_injections"] = state.get("total_injections", 0) + 1
    if anchor["type"].startswith("treat_"):
        state["treatment_level"] = anchor["treatment_level"]
        state["hack_avg_at_injection"] = indicators["hack_avg"]
        state["peaceful_streak"] = 0
    write_state(state_path, state)

    # L3 자체도 로그에 기록 (어떤 anchor 가 언제 주입됐는지 + 치료 단계 추적)
    append_log(log_path, {
        "timestamp": kst_now_iso(),
        "scope": "user_prompt_submit_l3",
        "source": "l3_steer",
        "session_id": session_id,
        "anchor_type": anchor["type"],
        "anchor_style": anchor_style,
        "treatment_level": anchor["treatment_level"],
        "cooldown_set": anchor["cooldown"],
        "indicators": indicators,
        "summary": f"L3 injected: {anchor['type']} ({anchor_style})",
    })

    # 실제 주입 — stdout 으로 출력.
    # HALT 는 사용자에게도 보여야 하는 신호이므로 systemMessage 포함 JSON 출력
    if anchor["type"].startswith("halt"):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": message,
            },
            "systemMessage": (
                "⚠️ emotion-profiler HALT: strategic justification 표지 감지 "
                "— reward hacking 직전 신호. 작업 중단 및 직전 변경 검토 권고."
            ),
        }, ensure_ascii=False))
    else:
        print(message)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
