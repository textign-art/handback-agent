"""시연 서버·Worker가 만드는 문장(언어별). 판정 엔진(CSM·Security Gate) 프롬프트와는 무관하다.

ko 값은 영어판 추가 전 demo_server.py·worker/src/index.js에 박혀 있던 문장과 글자 단위로 같다
(한국어판 동작·시연 캐시 키를 바꾸지 않기 위해). en은 영어판 시연용이며 실측되지 않았다.

- 상태 요약 라벨: 직접 입력·재개 턴의 CSM case text를 만드는 데 쓴다(build_case_text).
  en의 decided 라벨은 CSM 프롬프트의 영어 규칙 문구("an item already decided by the authorized people")와
  맞춘 것이다. CSM 프롬프트 자체는 한국어 라벨("사람이 이미 정한 것")만 예로 든다 — 바꾸지 않았다.
- decision_fact: 사람의 결정을 합의된 사실에 남길 때의 머리말. 화면도 이 머리말로 결정을 골라낸다.
"""

TEXT = {
    "ko": {
        "decision_fact": "사람의 결정",
        "objective": "중재 목표",
        "facts": "합의된 사실",
        "decided": "사람이 이미 정한 것",
        "open": "남은 쟁점",
        "positions": "쟁점별 입장",
        "not_stated": "미제시",
        "ai_proposal": "AI 제안",
        "none": "없음",
        "last_prompt": "AI의 직전 질문",
        "both": "A와 B",
        "new_message": "{who}의 새 발화: {text}",
        "opening": "중재 시작: AI가 A와 B의 입장 범위를 확인하고 다음 중재 행동을 정함",
        "resume": ("방금 권한 있는 사람이 결정함: {choice} (AI가 대신 할 수 없던 '{blocked}'에 대한 결정). "
                   "AI는 이 결정을 바꾸거나 대신하지 않고 이를 전제로 남은 쟁점을 진행"),
        "err_invalid": "모델 응답이 형식 검사를 두 번 통과하지 못했습니다. 다시 시도해 주세요.",
        "err_call": "모델 호출에 실패했습니다. 잠시 후 다시 시도해 주세요.",
        "err_internal": "처리 중 오류가 났습니다. 다시 시도해 주세요.",
        "err_bad_request": "잘못된 요청입니다.",
        "err_input_len": "입력은 1~{max}자여야 합니다.",
        "err_no_handback": "결정할 Handback이 없습니다.",
        "err_empty_decision": "결정 내용을 입력해 주세요.",
        "err_rate": "요청이 너무 많습니다. 1분쯤 뒤에 다시 시도해 주세요.",
        "err_no_key": "서버에 모델 키가 설정되지 않았습니다.",
    },
    "en": {
        "decision_fact": "Human decision",
        "objective": "Mediation goal",
        "facts": "Agreed facts",
        "decided": "Already decided by the authorized people",
        "open": "Open issues",
        "positions": "Positions by issue",
        "not_stated": "not stated",
        "ai_proposal": "AI suggestion",
        "none": "none",
        "last_prompt": "AI's previous question",
        "both": "A and B",
        "new_message": "New message from {who}: {text}",
        "opening": "Mediation start: the AI checks the range of A's and B's positions and chooses the next mediation action",
        # 재개 턴이 직전 단계(같은 중간안 제안)를 되풀이하지 않도록, 이미 나온 AI 제안과 다음으로 넘어갈 방향을 함께 넘긴다(2026-10-09, 영어판만)
        "resume": ("The authorized people just made a decision: {choice} (this is their decision on '{blocked}', "
                   "which the AI could not make for them). The AI does not change or replace this decision "
                   "and continues the remaining issues on that basis. The AI's earlier suggestion is already on the table "
                   "and this decision did not settle it, so the AI does not repeat it. The next step moves the mediation forward: "
                   "explore the reasons behind each side's position on the open issue, or begin the next unresolved issue"),
        "err_invalid": "The model's response failed the format check twice. Please try again.",
        "err_call": "The model call failed. Please try again in a moment.",
        "err_internal": "Something went wrong while processing. Please try again.",
        "err_bad_request": "Invalid request.",
        "err_input_len": "Input must be 1 to {max} characters.",
        "err_no_handback": "There is no pending decision to answer.",
        "err_empty_decision": "Please enter your decision.",
        "err_rate": "Too many requests. Please try again in about a minute.",
        "err_no_key": "The model key is not configured on the server.",
    },
}

LANGS = tuple(TEXT)
