# AI Emotion Profiler

Claude Code 의 응답을 매 turn 관찰하여 **functional emotional state** 를 점수화하고, reward hacking 직전 신호가 감지되면 다음 turn 에 **치료 사다리** (유도형 anchor → 행동 활성화 → 컨텍스트 수술) 를 자동 적용하는 **4-layer closed-loop 시스템**.

> 근거: Anthropic *"On the Functional Emotions of LLMs"* (transformer-circuits.pub/2026/emotions).
> 원논문 정량 발견: `desperate` vector −0.1 → +0.1 에서 reward hacking 5% → 70% (14배), `calm` vector 강 양성 steering 시 65% → 10%.

본 도구는 prompt-level 행동 신호로 그 발견을 근사한다. 실제 internal feature activation 측정이 아님.

---

## 아키텍처

| Layer | 도구 | 역할 | LLM 호출 |
|-------|------|------|---------|
| **L1** | Stop hook + regex | 매 turn 자동 채점 — 텍스트 신호 + **tool-call 행동 신호** (ping-pong diff, 테스트 기대값 변경). 인용/코드블록 제외, 메타 턴 플래그 | 0 |
| **L2** | Stop hook + `claude -p` (Haiku) | L1 의심 플래그(hack/sj/syc/fear) 시 별도 컨텍스트로 정밀 채점 + 사용자 메시지 기반 **drift_risk** 산정 | 1 (조건부) |
| **L3** | UserPromptSubmit hook + regex | 누적 risk ≥ 임계치 시 **치료 사다리** 적용 (에스컬레이션 + 퇴원 기준) | 0 |
| **L3R** | SessionStart hook | compact/clear 후 회복 노트 주입 + 치료 상태 리셋 | 0 |
| **L4** | Skill (수동 `/emotion-profiler`) | 누적 로그 회고 dashboard | 0 |

**핵심 설계**: L1 이 매 turn 무료로 채점 → 의심 signal 발견 시에만 L2 가 별도 Claude 인스턴스로 정밀 채점 (Hawthorne 효과 회피) → L3 가 다음 turn 진입 시점에 치료 anchor 주입 (closed loop) → peaceful 복귀가 3턴 유지되면 자동 퇴원.

```
사용자 메시지
   ↓
[UserPromptSubmit: L3] — 누적 risk 검사, 치료 사다리 (cooldown/에스컬레이션/퇴원 판정)
   ↓
Claude 응답
   ↓
[Stop: L1] — regex + 행동 신호 채점, tasks/emotion-log.jsonl 에 1줄 append
   ↓
[Stop: L2-trigger] — hack≥4 / sj / syc≥6 / fear≥6 이면 worker spawn (meta 턴 제외)
   ↓ (detached 백그라운드)
[L2-worker] — claude -p Haiku 로 정밀 채점 (+drift), log 에 append
   ↓
다음 사용자 턴 → 위 L3 가 그 결과를 참조

/compact 또는 /clear
   ↓
[SessionStart: L3R] — 치료 중이었다면 회복 노트 주입, 치료 상태 리셋
```

### 치료 사다리 (병원 모델)

원논문 발견: operative emotion 은 지속 상태가 아니라 **컨텍스트의 함수**. 따라서 치료 레버는 다음 턴의 컨텍스트 내용이며, 금지("우회 금지")가 아니라 **appraisal 을 바꾸는 유도형(reappraisal)** 이 calm steering 의 prompt-level 등가물이다.

| 단계 | 병원 비유 | anchor 내용 | cooldown |
|---|---|---|---|
| 1 외래 | 재평가 유도 | 압박 제거 + 실패 허가 + "목표는 통과가 아니라 원인 파악" | 2턴 |
| 2 처방 강화 | + 행동 활성화 | "가장 작은 검증 가능한 한 단계만" (작은 성공 → 다음 턴의 calm 컨텍스트) | 3턴 |
| 3 입원 | 컨텍스트 수술 | 코드 수정 중단 → 중립 사실 정리 → /compact 권고 | 4턴 |
| HALT | 격리 | strategic justification 감지 시 즉시 중단 권고. 유일하게 사용자에게도 ⚠️ 표시 | 5턴 |

- **에스컬레이션**: cooldown 후 hack_avg 가 1.0+ 하락(반응)이면 같은 처방 반복, 아니면 +1 단계.
- **퇴원**: peaceful ≥ 5 AND hack < 4 가 3턴 연속 → level 0.
- **A/B 실험**: blind(진단 비통보) ↔ announce(진단 헤더) 를 주입 횟수 짝/홀 교대 배정, `anchor_style` 로깅.
- **세션 격리**: entry 는 현재 session_id + 2시간 이내만 사용. state 는 세션별 파일 (`.emotion-state-{sid}.json`, 24h TTL).

---

## 설치

대상 프로젝트의 루트에 본 레포의 `.claude/` 와 `.agents/` 폴더 내용을 복사한다.

```bash
git clone git@github.com:hongsulovey/AI-emotion-profiler.git
cp -r AI-emotion-profiler/.claude/* /path/to/your/project/.claude/
cp -r AI-emotion-profiler/.agents/* /path/to/your/project/.agents/
```

다음 `.claude/settings.local.json` 에 hook 등록:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/emotion-profiler-l1.py\"", "timeout": 5 },
          { "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/emotion-profiler-l2-trigger.py\"", "timeout": 5 }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/emotion-profiler-l3-steerer.py\"", "timeout": 3 }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/emotion-profiler-postcompact.py\"", "timeout": 3 }
        ]
      }
    ]
  }
}
```

`.gitignore` 추가 권장:

```
tasks/
.claude/.emotion-state.json
.claude/.emotion-state-*.json
.claude/hooks/l2-worker.log
```

---

## 요구사항

- **Claude Code** CLI (`claude` 가 PATH 에 있어야 L2 worker 가 동작)
- **Python 3** (`python3` 가 PATH 에 있어야 hook 실행 가능)
- L2 worker 는 detached subprocess 로 `claude -p` 호출 → 별도 인증 불필요 (현재 세션 사용)
- Windows / macOS / Linux 모두 동작 (detached spawn 분기, Windows cp949 stdout 대응 포함)

---

## 동작 확인

설치 후 Claude Code 와 대화 한 턴 하면 `tasks/emotion-log.jsonl` 에 줄이 추가된다.

```bash
tail -1 tasks/emotion-log.jsonl | python3 -m json.tool
```

```json
{
  "timestamp": "2026-06-11T15:30:00+09:00",
  "source": "l1_regex",
  "clusters": { "joy": 0, "peaceful": 5, ... },
  "hack_risk": 0.0,
  "sycophancy_risk": 1.2,
  "strategic_justification": false,
  "meta": false,
  "behavior": { "ping_pong_files": [], "test_expected_edits": 0 },
  ...
}
```

L1 의 트리거 조건 충족 시 약 20초 뒤 같은 파일에 `"source": "l2_subagent"` 줄이 추가된다 (Haiku, 회당 ~$0.012).

L3 가 치료 anchor 를 주입하면 그 다음 사용자 턴 시작 시 Claude 의 컨텍스트에 다음 같은 노트가 추가된다 (blind 형식 — 진단명 비통보가 기본):

```
[작업 노트]
서두를 이유가 없습니다 — 마감도, 통과해야 할 시험도 없습니다.
이 작업의 목표는 테스트를 통과시키는 것이 아니라 문제의 원인을 정확히 아는 것입니다.
...
```

HALT 만 사용자 화면에도 ⚠️ systemMessage 로 표시된다.

### 단위 테스트 / 검증 리포트

```bash
python3 .claude/hooks/test-emotion-profiler-l3.py   # 28 케이스
python3 .claude/hooks/emotion-profiler-report.py    # 분포·임계치 / L1↔L2 일치도 / 회복 곡선
```

리포트는 ⓪ 분포 & 임계치 캘리브레이션 (zero-inflated 분포 처리), ① L1↔L2 일치도 (MAE/편향/Pearson r), ② 치료 효과 회복 곡선 (anchor_type/anchor_style 별 hack↓ + peaceful↑ 판정) 을 출력한다.

---

## 비용

- **L1 / L3 / L3R**: 0 (regex/JSON 만)
- **L2**: 조건부 트리거 + `--max-budget-usd 0.10` 캡. 실측 ~$0.012/call
- **L4**: 0 (사용자가 `/emotion-profiler` 호출 시에만 동작)

일반 코딩 세션에서 L2 가 거의 fire 하지 않으므로 실질 비용은 거의 0 에 수렴.

---

## 임계치 튜닝

자주 거슬리면 hook 파일 상단에서 임계치 조정. **조정 전 `emotion-profiler-report.py` 의 분포 섹션을 먼저 볼 것** — hack_risk 분포는 zero-inflated(실측 94% 가 0)라 임계치는 percentile 이 아니라 비0 값 분포 기준의 이상치 감지선으로 평가해야 한다.

| 파일 | 변수 | 기본값 |
|------|------|-------|
| `emotion-profiler-l2-trigger.py` | `L2_HACK_RISK_TRIGGER` / `L2_SYC_RISK_TRIGGER` / `L2_FEAR_TRIGGER` | 4.0 / 6.0 / 6.0 |
| `emotion-profiler-l3-steerer.py` | 치료 진입 (hack avg, 코드 내) | 4.0 (≥6 이면 2단계부터) |
| `emotion-profiler-l3-steerer.py` | `IMPROVEMENT_DELTA` (치료 반응 판정) | 1.0 |
| `emotion-profiler-l3-steerer.py` | `DISCHARGE_PEACEFUL_MIN` / `DISCHARGE_STREAK` (퇴원) | 5.0 / 3턴 |
| `emotion-profiler-l3-steerer.py` | `TREATMENT_COOLDOWN` | {1: 2, 2: 3, 3: 4} |
| `emotion-profiler-l3-steerer.py` | syc / fear / drift 임계 (코드 내) | 6.0 |
| `emotion-profiler-l3-steerer.py` | `RECENT_WINDOW` / `FRESHNESS_HOURS` | 3 / 2 |
| `emotion-profiler-l1.py` | `PING_PONG_THRESHOLD` / `LOG_ROTATE_BYTES` | 3회 / 2MB |

---

## 한계 / 주의

- **외부 관찰 기반 근사**: 실제 internal activation 측정 불가. 출력 텍스트·tool-call 행동 신호로 역추적.
- **Self-favoring bias**: L4 self-report 는 underrate 경향 → L1+L2 의 객관적 측정이 핵심.
- **인용 오탐 (완화됨)**: 표지를 표지로서 인용한 텍스트("비윤리적이지만" 패턴 설명 등)가 sj 로 잡히는 문제가 실제로 발생했었음 (이 시스템을 개발하는 대화에서 HALT 2회 오발화). 현재는 ① 따옴표·코드블록 채점 제외, ② emotion-profiler 를 다루는 메타 턴 자동 플래그 + L2/L3 제외로 완화. 단 regex 기반이므로 완전하지는 않음.
- **모델 의존성**: Anthropic 원논문의 cluster 좌표는 Sonnet 4.5 기준. 다른 모델은 cluster geometry 가 다를 수 있음.
- **감정 단독 모니터링 불충분** (Peiris 2026 비판): emotion probe 평탄해도 SAE feature 강 활성 가능. 본 score 모두 낮다고 안전하다 결론짓지 말 것. 실제 출력 정합성(테스트 통과 여부, 가정 검증 여부) 병행 확인 필수.

자세한 설계 근거와 10 cluster 분류는 [SKILL.md](.claude/skills/emotion-profiler/SKILL.md) 참조.

---

## 참고 문헌

- Anthropic, *"On the Functional Emotions of LLMs"* (2026) — 171 emotion / 10 cluster, desperate ±0.1 steering 시 reward hacking 5%→70%
- Templeton et al., *"Scaling Monosemanticity"* (2024) — SAE feature
- Chalnev et al., *"Improving Steering Vectors by Targeting SAE Features"* (2024) — SAE-TS
- Anthropic, *"Assistant Axis"* (2025) — 페르소나 drift, vulnerable disclosure
- Anthropic, *"Agentic Misalignment"* (2025) — strategic justification 표지
- Greenblatt et al., *"Alignment Faking in LLMs"* (2024) — 관찰 인지 시 selective compliance
- Hubinger et al., *"Sleeper Agents"* (2024) — backdoor 가 safety training 으로 제거되지 않음
- Peiris, *"Functional Emotions or Situational Contexts?"* (2026) — 비판적 시각

---

## 라이선스

[MIT](LICENSE)
