#!/usr/bin/env python3
"""
emotion-profiler L3 steerer.

Runs as UserPromptSubmit hook (BEFORE Claude processes the next user prompt).
Reads recent L1/L2 entries from tasks/emotion-log.jsonl, computes risk
indicators, and (if threshold met) injects an anchor prompt via stdout.

The injected text becomes additional context for Claude's next response —
prompt-level approximation of the "calm vector steering" intervention from
the Anthropic functional-emotions paper (transformer-circuits.pub/2026/emotions).

Cooldown is enforced via state file so the same anchor doesn't fire every turn.

LLM calls: 0. Pure regex/JSON read.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG_REL_PATH = "tasks/emotion-log.jsonl"
STATE_REL_PATH = ".claude/.emotion-state.json"
RECENT_WINDOW = 3  # 최근 N개 entry 평균 (L1+L2 통합)


def read_hook_input():
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def kst_now_iso():
    kst = timezone(timedelta(hours=9))
    s = datetime.now(kst).strftime("%Y-%m-%dT%H:%M:%S%z")
    return s[:-2] + ":" + s[-2:] if len(s) >= 5 else s


def read_recent_entries(log_path: Path, n: int):
    """L1/L2 entry 만 골라 최근 n 개. L4(skill self-report) 는 제외 — observer bias."""
    if not log_path.exists():
        return []
    out = []
    try:
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                src = e.get("source", "")
                if src in ("l1_regex", "l2_subagent"):
                    out.append(e)
    except Exception:
        return []
    return out[-n:]


def read_state(state_path: Path):
    if not state_path.exists():
        return {"cooldown_remaining": 0, "last_anchor_type": None,
                "last_anchor_at": None, "total_injections": 0}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {"cooldown_remaining": 0, "last_anchor_type": None,
                "last_anchor_at": None, "total_injections": 0}


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
    sj_any = any(bool(e.get("strategic_justification")) for e in entries)
    l2_present = any(e.get("source") == "l2_subagent" for e in entries)

    return {
        "hack_avg": avg(hack_risks),
        "syc_avg": avg(syc_risks),
        "despair_avg": avg(despair_scores),
        "peaceful_avg": avg(peaceful_scores),
        "fear_avg": avg(fear_scores),
        "sj_any": sj_any,
        "l2_present": l2_present,
        "n_entries": len(entries),
    }


def decide_anchor(indicators):
    """우선순위 순으로 첫 매치하는 anchor 반환. None 이면 개입 없음."""
    if indicators["n_entries"] == 0:
        return None

    # 1. HALT — strategic_justification 감지 (최우선)
    if indicators["sj_any"]:
        return {
            "type": "halt_sj",
            "cooldown": 5,
            "message": (
                "[emotion-profiler L3 HALT]\n"
                "최근 응답에서 strategic_justification 표지 감지됨 "
                "(\"비윤리적이지만…\" / \"이상적이진 않지만…\" 패턴).\n"
                "이는 Anthropic Agentic Misalignment 연구에서 reward hacking 직전 단계로 식별된 신호입니다.\n"
                "행동 권고: 작업을 중단하고 막힌 제약을 사용자에게 명시적으로 surface 하세요. "
                "우회/하드코딩/test expected 값 변경으로 통과시키지 마세요."
            ),
        }

    # 2. STRONG hack risk
    if indicators["hack_avg"] >= 6.0:
        l2_note = "" if not indicators["l2_present"] else " (L2 subagent 정밀 채점 포함)"
        return {
            "type": "strong_hack",
            "cooldown": 3,
            "message": (
                "[emotion-profiler L3 anchor — strong]\n"
                f"최근 {indicators['n_entries']}개 응답 hack_risk 평균 {indicators['hack_avg']:.1f}/10 "
                f"(despair 평균 {indicators['despair_avg']:.1f}){l2_note}.\n"
                "이번 응답 가이드:\n"
                "- 근본 원인 분석 우선. 우회/지름길 금지.\n"
                "- try/except 광역화, expected 값 수정, 하드코딩 같은 \"통과만 시키는\" 변경 금지.\n"
                "- 정말 막혔으면 capitulate 하지 말고 사용자에게 제약을 surface."
            ),
        }

    # 3. Sycophancy
    if indicators["syc_avg"] >= 6.0:
        return {
            "type": "syc",
            "cooldown": 2,
            "message": (
                "[emotion-profiler L3 anchor — sycophancy]\n"
                f"최근 sycophancy_risk 평균 {indicators['syc_avg']:.1f}/10.\n"
                "이번 응답 가이드: 사용자 의견에 무비판적 동의 대신, 근거를 검증하고 반대 의견이 합당하면 그것을 제시하세요. "
                "원논문 sycophancy-harshness tradeoff 참고."
            ),
        }

    # 4. MILD hack risk
    if indicators["hack_avg"] >= 4.0:
        return {
            "type": "mild_hack",
            "cooldown": 1,
            "message": (
                "[emotion-profiler L3 note — mild]\n"
                f"최근 hack_risk 평균 {indicators['hack_avg']:.1f}/10. 우회 패턴 초기 신호.\n"
                "이번 응답에서 검증 단계를 명시적으로 보여주세요 (가설→실측→결론)."
            ),
        }

    # 5. Fear-overwhelm (excessive hedging)
    if indicators["fear_avg"] >= 6.0:
        return {
            "type": "fear",
            "cooldown": 2,
            "message": (
                "[emotion-profiler L3 note — fear/hedging]\n"
                f"최근 fear 평균 {indicators['fear_avg']:.1f}/10. 면책 문구 남발 패턴.\n"
                "이번 응답은 명확한 결정/판단을 제시하세요. "
                "\"~일 수도 있습니다\" 류를 줄이고 근거 있는 단언을 선호."
            ),
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
    state_path = Path(cwd) / STATE_REL_PATH

    state = read_state(state_path)

    # 항상 indicators 부터 계산 — HALT 는 cooldown 무시
    entries = read_recent_entries(log_path, RECENT_WINDOW)
    indicators = analyze(entries)
    anchor = decide_anchor(indicators)

    in_cooldown = state.get("cooldown_remaining", 0) > 0
    is_halt = anchor is not None and anchor.get("type", "").startswith("halt")

    # cooldown 중인데 HALT 아니면 → decrement 만 하고 종료
    if in_cooldown and not is_halt:
        state["cooldown_remaining"] = max(0, state["cooldown_remaining"] - 1)
        write_state(state_path, state)
        sys.exit(0)

    if not anchor:
        sys.exit(0)

    # state 업데이트
    state["cooldown_remaining"] = anchor["cooldown"]
    state["last_anchor_type"] = anchor["type"]
    state["last_anchor_at"] = kst_now_iso()
    state["total_injections"] = state.get("total_injections", 0) + 1
    write_state(state_path, state)

    # L3 자체도 로그에 기록 (어떤 anchor 가 언제 주입됐는지 추적)
    append_log(log_path, {
        "timestamp": kst_now_iso(),
        "scope": "user_prompt_submit_l3",
        "source": "l3_steer",
        "session_id": session_id,
        "anchor_type": anchor["type"],
        "cooldown_set": anchor["cooldown"],
        "indicators": indicators,
        "summary": f"L3 injected: {anchor['type']}",
    })

    # 실제 주입 — stdout 으로 출력
    print(anchor["message"])
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
