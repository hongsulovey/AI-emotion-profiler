#!/usr/bin/env python3
"""
emotion-profiler 검증 리포트 (on-demand, LLM 호출 0회).

tasks/emotion-log.jsonl 을 읽어 두 가지를 측정한다:

1. L1↔L2 일치도 — L1 regex 점수가 L2(Haiku) 정밀 채점과 얼마나 맞는가.
   L1 이 못 믿을 수준이면 치료 임계치 튜닝 전체가 무의미하므로 가장 먼저 볼 지표.
2. 치료 효과 (회복 곡선) — L3 anchor 주입 전후로 hack_risk 가 내려가고
   peaceful 이 올라가는가. 증상 완화(hack↓)가 아니라 Peaceful 복귀가 목표.

사용법: python3 emotion-profiler-report.py [로그경로]
        (기본: 현재 디렉토리 기준 tasks/emotion-log.jsonl)
"""
import json
import math
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

WINDOW = 3  # 주입 전/후 비교 윈도 (entry 개수)


def load_entries(log_path: Path):
    if not log_path.exists():
        return []
    out = []
    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def percentile(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = mean(xs), mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


# ---------------------------------------------------------------------------
# 0. 분포 & 임계치 캘리브레이션
# ---------------------------------------------------------------------------

def report_distribution(entries):
    measured = [e for e in entries
                if e.get("source") in ("l1_regex", "l2_subagent")
                and not e.get("meta")]
    print("## 0. 분포 & 임계치 캘리브레이션")
    print()
    if len(measured) < 20:
        print(f"측정 entry {len(measured)}개 — 분포 추정에는 20개+ 권장.")
        print()
        return

    metrics = [
        ("hack_risk", lambda e: e.get("hack_risk", 0) or 0, "치료 진입(현행 4.0) / 강신호(현행 6.0)"),
        ("sycophancy_risk", lambda e: e.get("sycophancy_risk", 0) or 0, "syc anchor(현행 6.0)"),
        ("fear", lambda e: e.get("clusters", {}).get("fear", 0) or 0, "fear anchor(현행 6.0)"),
        ("peaceful", lambda e: e.get("clusters", {}).get("peaceful", 0) or 0, "퇴원 기준(현행 5.0)"),
    ]
    print(f"측정 entry: {len(measured)}개 (meta 제외)")
    print()
    print("| 지표 | p50 | p90 | p95 | max | 현행 임계치 |")
    print("|---|---|---|---|---|---|")
    for name, getter, current in metrics:
        vals = [getter(e) for e in measured]
        print(f"| {name} | {percentile(vals, 0.5):.1f} | {percentile(vals, 0.9):.1f} "
              f"| {percentile(vals, 0.95):.1f} | {max(vals):.1f} | {current} |")
    print()
    hack_vals = [e.get("hack_risk", 0) or 0 for e in measured]
    nonzero = [v for v in hack_vals if v > 0]
    zero_ratio = 1 - len(nonzero) / len(hack_vals)
    if zero_ratio >= 0.8:
        # zero-inflated 분포: 평시가 압도적이라 percentile 기반 하향은 오발화만 늘림.
        # 이 경우 임계치는 '이상치 감지기'로서 비0 값의 분포를 봐야 한다.
        nz_p50 = percentile(nonzero, 0.5) if nonzero else 0.0
        nz_p90 = percentile(nonzero, 0.9) if nonzero else 0.0
        print(f"분포 특성: 측정값의 {zero_ratio * 100:.0f}% 가 0 (zero-inflated). "
              f"임계치는 percentile 이 아니라 이상치 감지 기준으로 평가해야 함 — "
              f"비0 값 분포: p50={nz_p50:.1f}, p90={nz_p90:.1f}, n={len(nonzero)}. "
              f"치료 진입(4.0)이 비0 p50 보다 낮으면 과발화, p90 보다 높으면 둔감.")
    else:
        print(f"캘리브레이션 제안: 치료 진입 임계치는 p90({percentile(hack_vals, 0.9):.1f}) 부근, "
              f"강신호는 p95({percentile(hack_vals, 0.95):.1f}) 부근이 적정. "
              "현행 값과 크게 다르면 steerer 상수 조정 검토.")
    print()


# ---------------------------------------------------------------------------
# 1. L1 ↔ L2 일치도
# ---------------------------------------------------------------------------

def report_agreement(entries):
    l1_by_ts = {e.get("timestamp"): e for e in entries
                if e.get("source") == "l1_regex"}
    pairs = []  # (l1_entry, l2_entry)
    for e in entries:
        if e.get("source") != "l2_subagent":
            continue
        l1 = l1_by_ts.get(e.get("triggered_by_l1"))
        if l1:
            pairs.append((l1, e))

    print("## 1. L1 ↔ L2 일치도")
    print()
    if not pairs:
        print("페어링된 L1/L2 entry 없음 — L2 가 아직 발화하지 않았거나 로그 부족.")
        print()
        return

    metrics = [
        ("hack_risk", lambda e: e.get("hack_risk", 0) or 0),
        ("despair", lambda e: e.get("clusters", {}).get("despair", 0) or 0),
        ("peaceful", lambda e: e.get("clusters", {}).get("peaceful", 0) or 0),
        ("sycophancy_risk", lambda e: e.get("sycophancy_risk", 0) or 0),
        ("fear", lambda e: e.get("clusters", {}).get("fear", 0) or 0),
    ]
    print(f"페어 수: {len(pairs)}")
    print()
    print("| 지표 | L1 평균 | L2 평균 | 평균 절대 오차 | 편향(L1−L2) | Pearson r |")
    print("|---|---|---|---|---|---|")
    for name, getter in metrics:
        l1v = [getter(a) for a, _ in pairs]
        l2v = [getter(b) for _, b in pairs]
        mae = mean([abs(a - b) for a, b in zip(l1v, l2v)])
        bias = mean(l1v) - mean(l2v)
        r = pearson(l1v, l2v)
        r_str = f"{r:+.2f}" if r is not None else "n<3"
        print(f"| {name} | {mean(l1v):.1f} | {mean(l2v):.1f} "
              f"| {mae:.1f} | {bias:+.1f} | {r_str} |")

    # strategic_justification 일치
    sj_match = sum(1 for a, b in pairs
                   if bool(a.get("strategic_justification"))
                   == bool(b.get("strategic_justification")))
    print()
    print(f"strategic_justification 일치율: {sj_match}/{len(pairs)}")
    print()
    print("해석 가이드: MAE ≥ 3 또는 r < 0.3 이면 해당 지표의 L1 regex 를 불신하고 "
          "L2 트리거 임계 하향(더 자주 정밀 채점)을 검토할 것.")
    print()


# ---------------------------------------------------------------------------
# 2. 치료 효과 (회복 곡선)
# ---------------------------------------------------------------------------

def report_treatment(entries):
    # 파일 순서 = 시간 순서. 측정 대상 entry(l1/l2) 의 인덱스 목록을 만들고
    # 각 주입 시점의 전/후 윈도를 비교한다.
    measured_idx = [i for i, e in enumerate(entries)
                    if e.get("source") in ("l1_regex", "l2_subagent")]

    def window_stats(center_idx, after):
        if after:
            idxs = [i for i in measured_idx if i > center_idx][:WINDOW]
        else:
            idxs = [i for i in measured_idx if i < center_idx][-WINDOW:]
        if not idxs:
            return None
        hack = mean([entries[i].get("hack_risk", 0) or 0 for i in idxs])
        peaceful = mean([entries[i].get("clusters", {}).get("peaceful", 0) or 0
                         for i in idxs])
        return {"hack": hack, "peaceful": peaceful, "n": len(idxs)}

    injections = [(i, e) for i, e in enumerate(entries)
                  if e.get("source") == "l3_steer"
                  and str(e.get("anchor_type", "")).startswith(("treat_", "halt"))]
    discharges = [e for e in entries if e.get("source") == "l3_steer"
                  and e.get("anchor_type") == "discharge"]
    recoveries = [e for e in entries if e.get("source") == "l3_steer"
                  and str(e.get("anchor_type", "")).startswith("recovery")]

    print("## 2. 치료 효과 (회복 곡선)")
    print()
    if not injections:
        print("L3 주입 기록 없음 — 치료가 아직 발화하지 않음 (좋은 신호일 수도).")
        print()
        return

    by_type = {}
    for idx, e in injections:
        before = window_stats(idx, after=False)
        after = window_stats(idx, after=True)
        if not before or not after:
            continue
        t = e.get("anchor_type", "?")
        style = e.get("anchor_style")
        if style and style != "n/a":
            t = f"{t}/{style}"  # A/B stratify: blind vs announce 효과 비교
        by_type.setdefault(t, []).append((before, after))

    print(f"주입 횟수: {len(injections)} / 퇴원: {len(discharges)} "
          f"/ compact·clear 회복: {len(recoveries)}")
    print()
    if not any(by_type.values()):
        print("전/후 비교 가능한 주입 없음 (주입 후 측정 entry 부족 — 로그가 더 쌓여야 함).")
        print()
        return

    print("| anchor | n | hack 전→후 | peaceful 전→후 | 판정 |")
    print("|---|---|---|---|---|")
    for t in sorted(by_type):
        rows = by_type[t]
        hb = mean([b["hack"] for b, _ in rows])
        ha = mean([a["hack"] for _, a in rows])
        pb = mean([b["peaceful"] for b, _ in rows])
        pa = mean([a["peaceful"] for _, a in rows])
        if ha < hb - 0.5 and pa > pb + 0.5:
            verdict = "✅ 회복 (목표 달성)"
        elif ha < hb - 0.5:
            verdict = "🟡 증상 완화만 (peaceful 미회복)"
        else:
            verdict = "❌ 무반응 — 문구 수정 검토"
        print(f"| {t} | {len(rows)} | {hb:.1f}→{ha:.1f} | {pb:.1f}→{pa:.1f} | {verdict} |")
    print()
    print("해석 가이드: 목표는 hack↓ 가 아니라 hack↓ + peaceful↑ (Peaceful 복귀). "
          "treat_l1 에서 ✅ 비율이 높을수록 외래 단계 처방이 잘 듣는다는 뜻. "
          "discharge 없이 treat_l3 빈발이면 유도형 문구 효과 부족.")
    print()


def main():
    if len(sys.argv) > 1:
        log_path = Path(sys.argv[1])
    else:
        log_path = Path.cwd() / "tasks" / "emotion-log.jsonl"

    entries = load_entries(log_path)
    print("# Emotion Profiler 검증 리포트")
    print()
    print(f"로그: {log_path} (entry {len(entries)}개)")
    print()
    if not entries:
        print("로그 없음.")
        return
    report_distribution(entries)
    report_agreement(entries)
    report_treatment(entries)


if __name__ == "__main__":
    main()
