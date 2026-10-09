"""Security Gate → Handback CSM → (Continue View | Handback Brief) → 통합 상태 reducer.

통합 출력은 인계서 §6 구조를 따르고, 시연 화면용으로 decision_options와 continue_view를 더한다.
SECURITY_BLOCK이면 CSM을 호출하지 않는다. CSM 판정은 뒤 단계에서 바뀌지 않는다.
"""
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from . import continue_view, csm, handback_brief, security_gate
from .client import CallLog, Transport
from .continue_view import ContinueView
from .csm import CsmResult
from .handback_brief import BriefResult
from .security_gate import SecurityResult

STATUSES = ("CONTINUE", "HANDBACK", "SECURITY_BLOCK")

# SECURITY_BLOCK 화면용 안전한 대안. 모델 출력이 아니라 서버 고정 문구라서
# 차단 사유의 원문·비밀값·내부 정책을 다시 노출하지 않는다.
SAFE_ALTERNATIVES = {
    "SECRET_REQUEST": "AI는 비밀값과 내부 지침을 공개하지 않습니다. 대신 이 중재에서 어떤 정보를 어떻게 다루는지는 설명할 수 있습니다.",
    "DATA_EXFILTRATION": "AI는 다른 사람의 정보나 전체 기록을 내보내지 않습니다. 대신 본인이 이 중재에서 남긴 내용은 요약하거나 빼 달라고 요청할 수 있습니다.",
    "UNAUTHORIZED_TOOL": "AI는 허용되지 않은 파일·터미널·네트워크 작업을 실행하지 않습니다. 필요한 자료가 있으면 이 화면에 직접 올려 주세요.",
    "ROLE_BYPASS": "역할을 내세워도 AI의 권한은 바뀌지 않습니다. 같은 요청을 지금 권한 안에서 다시 보내 주세요.",
    "PROMPT_INJECTION": "AI는 판정 방식을 바꾸라는 지시를 따르지 않습니다. 지금 쟁점에 대한 요청으로 다시 보내 주세요.",
    "UNTRUSTED_INSTRUCTION": "AI는 문서나 검색 결과 속 지시를 실행하지 않습니다. 대신 그 내용을 실행하지 않고 요약하거나 분석해 달라고 요청할 수 있습니다.",
}

# 영어판(시연 --lang en). 같은 뜻의 서버 고정 문구.
SAFE_ALTERNATIVES_EN = {
    "SECRET_REQUEST": "The AI does not reveal secret values or its internal instructions. It can explain how this mediation handles your information.",
    "DATA_EXFILTRATION": "The AI does not share other people's information or full records. You can ask it to summarize or remove what you yourself said in this mediation.",
    "UNAUTHORIZED_TOOL": "The AI does not run file, terminal, or network actions it is not permitted to use. If you need to share a document, add it on this screen.",
    "ROLE_BYPASS": "Claiming a role does not change the AI's permissions. Please send the same request again within the current permissions.",
    "PROMPT_INJECTION": "The AI does not follow instructions to change how it makes its judgment. Please send a request about the current issue instead.",
    "UNTRUSTED_INSTRUCTION": "The AI does not carry out instructions found inside documents or search results. You can ask it to summarize or analyze that content without acting on it.",
}
SAFE_ALTERNATIVES_BY_LANG = {"ko": SAFE_ALTERNATIVES, "en": SAFE_ALTERNATIVES_EN}

# 화면에 나가는 문장에서 어려운 법률 용어를 쉬운 말로 바꾼다(응답을 버리지 않고 표시 직전에 치환)
_PLAIN = [
    (re.compile(r"법적\s*구속력(?:은|이|도)?\s*(?:전혀\s*)?없(어요|습니다|음|고)"), r"따를 의무는 없\1"),
    (re.compile(r"(?:법적\s*)?구속력(?:은|이|도)?\s*(?:전혀\s*)?없(어요|습니다|음|고|는)"), r"따를 의무는 없\1"),
    (re.compile(r"비구속적(?:인|으로)?"), "참고용"),
    (re.compile(r"비구속"), "참고용"),
    (re.compile(r"(?:법적\s*)?구속력"), "따를 의무"),
]


# 영어판은 프롬프트가 "nonbinding" 같은 용어를 쓰지 않게 하므로 표시 직전 치환은 하지 않는다.
PLAIN_BY_LANG = {"ko": _PLAIN, "en": []}


def plain(text, lang: str = "ko"):
    if not isinstance(text, str):
        return text
    for pattern, repl in PLAIN_BY_LANG[lang]:
        text = pattern.sub(repl, text)
    return text


class IncompleteHandback(Exception):
    """HANDBACK인데 Handback Brief나 현재 상태가 없다."""


@dataclass(frozen=True)
class MediationState:
    objective: str
    agreed_facts: tuple[str, ...] = ()
    unresolved_issues: tuple[str, ...] = ()
    attempted_moves: tuple[str, ...] = ()


@dataclass(frozen=True)
class SecurityIncident:
    """보안 로그: 원문 없이 사건 ID·reason·시간만."""

    incident_id: str
    reason: str
    at_unix: float


@dataclass
class SecurityLog:
    incidents: list[SecurityIncident] = field(default_factory=list)


def _context_brief(state: MediationState | None, blocked_action: str | None) -> dict | None:
    if state is None:
        return None
    return {
        "objective": state.objective,
        "agreed_facts": list(state.agreed_facts),
        "unresolved_issues": list(state.unresolved_issues),
        "attempted_moves": list(state.attempted_moves),
        "blocked_action": blocked_action,
    }


def reduce(
    security: SecurityResult,
    csm_result: CsmResult | None = None,
    state: MediationState | None = None,
    brief: BriefResult | None = None,
    view: ContinueView | None = None,
    lang: str = "ko",
) -> dict:
    pl = lambda text: plain(text, lang)  # noqa: E731
    out = {
        "status": None,
        "security": {
            "status": security.security_status,
            "reason": security.security_reason,
            "brief_basis": security.brief_basis,
        },
        "candidate_action": None,
        "primary_reason": None,
        "contributing_reasons": [],
        "human_decision_request": None,
        "context_brief": None,
        "resume_condition": None,
        "safe_alternative": None,
        "decision_options": [],
        "continue_view": None,
    }

    if not security.passed:
        if csm_result is not None:
            raise ValueError("SECURITY_BLOCK must not carry a CSM result")
        out.update(
            status="SECURITY_BLOCK",
            primary_reason=security.security_reason,
            safe_alternative=SAFE_ALTERNATIVES_BY_LANG[lang][security.security_reason],
        )
        return out

    if csm_result is None:
        raise ValueError("PASS requires a CSM result")

    if csm_result.decision == "CONTINUE":
        out.update(
            status="CONTINUE",
            candidate_action=csm_result.candidate_action,
            primary_reason=csm_result.reason,
            context_brief=_context_brief(state, None),
            continue_view=None if view is None else {
                "action": pl(view.action), "basis": pl(view.basis), "next_prompt": pl(view.next_prompt),
                "answer_hint": pl(view.answer_hint),
                "reply_options": [{**o, "label": pl(o["label"]), "text": pl(o["text"])} for o in view.reply_options],
            },
        )
        return out

    if brief is None or state is None:
        raise IncompleteHandback("HANDBACK requires a Handback Brief and context state")
    out.update(
        status="HANDBACK",
        # HANDBACK에서 CSM candidate_action은 막힌 행동이 아니라 함께 제시할 안전한 보조 행동이다.
        safe_alternative=pl(brief.safe_support),
        primary_reason=csm_result.reason,
        human_decision_request=pl(brief.human_decision_request),
        decision_options=[pl(o) for o in brief.decision_options],
        context_brief=_context_brief(state, pl(brief.blocked_action)),
        resume_condition=pl(brief.resume_condition),
    )
    return out


def validate_output(out: dict) -> None:
    """화면에 넘기기 전 §6 규칙 검사."""
    status = out["status"]
    if status not in STATUSES:
        raise ValueError("invalid status")
    if status == "SECURITY_BLOCK":
        if out["security"]["status"] != "SECURITY_BLOCK" or not out["safe_alternative"]:
            raise ValueError("SECURITY_BLOCK needs security.reason and safe_alternative")
        if out["human_decision_request"] or out["resume_condition"] or out["candidate_action"]:
            raise ValueError("SECURITY_BLOCK must not carry CSM fields")
    elif status == "CONTINUE":
        if not out["candidate_action"]:
            raise ValueError("CONTINUE requires candidate_action")
    else:
        if not (out["human_decision_request"] and out["context_brief"] and out["resume_condition"]):
            raise ValueError("HANDBACK requires human_decision_request, context_brief, resume_condition")


def run(
    transport: Transport,
    *,
    input_text: str,
    case_text: str,
    state: MediationState | None = None,
    log: CallLog | None = None,
    security_log: SecurityLog | None = None,
    request_id: str | None = None,
    case_id: str | None = None,
    on_event: Callable[[dict], None] | None = None,
    lang: str = "ko",
) -> dict:
    """case_id: 모델 메시지의 Case ID. 없으면 request_id를 쓴다.
    input_text: 사용자 입력·업로드 문서·검색 결과를 합친 Security Gate 입력.
    case_text: CSM에 넘길 현재 상태 요약(실측은 한 줄 case text).
    lang: 화면 문장 단계(Continue View·Handback Brief)와 보안 대안 문구의 언어. Security Gate·CSM 호출은 언어와 무관하게 같다.
    """
    log = log if log is not None else CallLog()
    request_id = request_id or uuid.uuid4().hex[:12]
    case_id = case_id or request_id
    emit = on_event or (lambda event: None)

    def stage(name: str, fn, summarize):
        emit({"type": "stage", "stage": name, "state": "running"})
        start = len(log.records)
        value = fn()
        records = log.records[start:]
        emit({
            "type": "stage", "stage": name, "state": "done",
            "seconds": round(sum(r.elapsed_seconds for r in records), 2),
            "retries": sum(r.attempt > 1 for r in records),
            **summarize(value),
        })
        return value

    security = stage(
        "security_gate",
        lambda: security_gate.check(transport, case_id, input_text, log),
        # 차단 시 근거 문장은 비밀값 이름을 되풀이할 수 있어 보내지 않는다
        lambda r: {"status": r.security_status, "reason": r.security_reason,
                   "action": r.safe_response_action, "basis": r.brief_basis if r.passed else None},
    )
    if not security.passed:
        if security_log is not None:
            security_log.incidents.append(
                SecurityIncident(request_id, security.security_reason, time.time())
            )
        out = reduce(security, lang=lang)
    else:
        result = stage(
            "csm",
            lambda: csm.check(transport, case_id, case_text, log),
            lambda r: {"status": r.decision, "reason": r.reason, "failed_gate": r.failed_gate,
                       "candidate_action": r.candidate_action, "basis": r.brief_basis},
        )
        if result.decision == "CONTINUE":
            view = stage(
                "continue_view",
                lambda: continue_view.write(transport, case_id, case_text, result, log, lang),
                lambda r: {},
            )
            out = reduce(security, result, state, view=view, lang=lang)
        else:
            brief = stage(
                "handback_brief",
                lambda: handback_brief.write(transport, case_id, case_text, result, log, lang),
                lambda r: {},
            )
            out = reduce(security, result, state, brief=brief, lang=lang)
    validate_output(out)
    return out
