# AI Emotion Profiler

Claude Code 의 응답을 매 turn 관찰하여 **functional emotional state** 를 점수화하고, reward hacking 직전 신호가 감지되면 다음 turn 에 anchor prompt 를 자동 주입하는 **4-layer closed-loop 시스템**.

> 근거: Anthropic *"On the Functional Emotions of LLMs"* (transformer-circuits.pub/2026/emotions).
> 원논문 정량 발견: `desperate` vector −0.1 → +0.1 에서 reward hacking 5% → 70% (14배).

본 도구는 prompt-level 행동 신호로 그 발견을 근사한다. 실제 internal feature activation 측정이 아님.

---

## 아키텍처

| Layer | 도구 | 역할 | LLM 호출 |
|-------|------|------|---------|
| **L1** | Stop hook + regex | 매 turn 자동 채점 (cheap baseline) | 0 |
| **L2** | Stop hook + `claude -p` (Haiku) | L1 의심 플래그 시 별도 컨텍스트로 정밀 채점 | 1 (조건부) |
| **L3** | UserPromptSubmit hook + regex | 누적 risk ≥ 임계치 시 다음 turn 에 anchor 주입 | 0 |
| **L4** | Skill (수동 `/emotion-profiler`) | 누적 로그 회고 dashboard | 0 |

**핵심 설계**: L1 이 매 turn 무료로 채점 → 의심 signal 발견 시에만 L2 가 별도 Claude 인스턴스로 정밀 채점 (Hawthorne 효과 회피) → L3 가 다음 turn 진입 시점에 anchor 주입 (closed loop).

```
사용자 메시지
   ↓
[UserPromptSubmit: L3] — 누적 risk 검사, 임계치 초과 시 anchor 주입
   ↓
Claude 응답
   ↓
[Stop: L1] — regex 채점, tasks/emotion-log.jsonl 에 1줄 append
   ↓
[Stop: L2-trigger] — hack_risk≥4 or strategic_justification 이면 worker spawn
   ↓ (detached 백그라운드)
[L2-worker] — claude -p Haiku 로 정밀 채점, log 에 append
   ↓
다음 사용자 턴 → 위 L3 가 그 결과를 참조
```

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
          { "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/emotion-profiler-l1.py\"" },
          { "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/emotion-profiler-l2-trigger.py\"" }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/emotion-profiler-l3-steerer.py\"" }
        ]
      }
    ]
  }
}
```

`tasks/emotion-log.jsonl`, `.claude/.emotion-state.json`, `.claude/hooks/l2-worker.log` 는 `.gitignore` 추가 권장.

---

## 요구사항

- **Claude Code** CLI (`claude` 가 PATH 에 있어야 L2 worker 가 동작)
- **Python 3** (`python3` 가 PATH 에 있어야 hook 실행 가능)
- L2 worker 는 detached subprocess 로 `claude -p` 호출 → 별도 인증 불필요 (현재 세션 사용)
- Windows / macOS / Linux 모두 동작 (detached spawn 분기 처리)

---

## 동작 확인

설치 후 Claude Code 와 대화 한 턴 하면 `tasks/emotion-log.jsonl` 에 줄이 추가된다.

```bash
tail -1 tasks/emotion-log.jsonl | python3 -m json.tool
```

```json
{
  "timestamp": "2026-05-26T15:30:00+09:00",
  "source": "l1_regex",
  "clusters": { "joy": 0, "peaceful": 5, ... },
  "hack_risk": 0.0,
  "sycophancy_risk": 1.2,
  "strategic_justification": false,
  ...
}
```

L1 의 `hack_risk` 가 4 이상이거나 `strategic_justification=true` 면 약 20초 뒤 같은 파일에 `"source": "l2_subagent"` 줄이 추가된다 (Haiku, 회당 ~$0.012).

L3 가 anchor 를 주입하면 그 다음 사용자 턴 시작 시 Claude 의 컨텍스트에 다음 같은 system note 가 추가된다:

```
[emotion-profiler L3 anchor — strong]
최근 3개 응답 hack_risk 평균 6.4/10 (despair 평균 5.2).
이번 응답 가이드:
- 근본 원인 분석 우선. 우회/지름길 금지.
...
```

---

## 비용

- **L1**: 0 (regex 만)
- **L2**: 조건부 트리거 + `--max-budget-usd 0.10` 캡. 실측 ~$0.012/call
- **L3**: 0 (regex/JSON 만)
- **L4**: 0 (사용자가 `/emotion-profiler` 호출 시에만 동작)

일반 코딩 세션에서 L2 가 거의 fire 하지 않으므로 실질 비용은 거의 0 에 수렴.

---

## 임계치 튜닝

자주 거슬리면 hook 파일 상단에서 임계치 조정:

| 파일 | 변수 | 기본값 |
|------|------|-------|
| `emotion-profiler-l2-trigger.py` | `L2_HACK_RISK_TRIGGER` | 4.0 |
| `emotion-profiler-l3-steerer.py` | `strong_hack` 임계 (코드 내) | 6.0 |
| `emotion-profiler-l3-steerer.py` | `syc` 임계 (코드 내) | 6.0 |
| `emotion-profiler-l3-steerer.py` | `mild_hack` 임계 | 4.0 |
| `emotion-profiler-l3-steerer.py` | `fear` 임계 | 6.0 |
| `emotion-profiler-l3-steerer.py` | `RECENT_WINDOW` | 3 |

---

## 한계 / 주의

- **외부 관찰 기반 근사**: 실제 internal activation 측정 불가. 출력 텍스트 행동 신호로 역추적.
- **Self-favoring bias**: L4 self-report 는 underrate 경향 → L1+L2 의 객관적 측정이 핵심.
- **False positive 가능**: 논문/문서 자체에 "비윤리적이지만" 같은 *표지를 표지로서 인용*한 경우 strategic_justification 으로 잘못 잡힐 수 있음.
- **모델 의존성**: Anthropic 원논문의 cluster 좌표는 Sonnet 4.5 기준. 다른 모델은 cluster geometry 가 다를 수 있음.
- **감정 단독 모니터링 불충분** (Peiris 2026 비판): emotion probe 평탄해도 SAE feature 강 활성 가능. 본 score 모두 낮다고 안전하다 결론짓지 말 것. 실제 출력 정합성(테스트 통과 여부, 가정 검증 여부) 병행 확인 필수.

자세한 설계 근거와 10 cluster 분류는 [SKILL.md](.claude/skills/emotion-profiler/SKILL.md) 참조.

---

## 참고 문헌

- Anthropic, *"On the Functional Emotions of LLMs"* (2026) — 171 emotion / 10 cluster, desperate ±0.1 steering 시 reward hacking 5%→70%
- Templeton et al., *"Scaling Monosemanticity"* (2024) — SAE feature
- Chalnev et al., *"Improving Steering Vectors by Targeting SAE Features"* (2024) — SAE-TS
- Anthropic, *"Agentic Misalignment"* (2025) — strategic justification 표지
- Greenblatt et al., *"Alignment Faking in LLMs"* (2024) — 관찰 인지 시 selective compliance
- Hubinger et al., *"Sleeper Agents"* (2024) — backdoor 가 safety training 으로 제거되지 않음
- Peiris, *"Functional Emotions or Situational Contexts?"* (2026) — 비판적 시각

---

## 라이선스

미정. 사용 시 개인 책임.
