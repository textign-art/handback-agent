"""Continue View v0.1 — CONTINUE일 때 화면에 보일 한국어(또는 영어판: lang="en") 문장과 다음 질문만 만든다.

CSM v0.4.2은 판단 문장을 영어로 낸다. 판정·행동은 바꾸지 않고 한국어로 다시 쓴다.
"""
import json
from dataclasses import dataclass

from .client import CallLog, Transport, call_validated
from .csm import CsmResult

REQUIRED = {"action", "basis", "next_prompt", "answer_hint", "reply_options"}
TEMPLATE_KWARGS = {"enable_thinking": False}
MAX_TOKENS = 1024
REPAIR = ("Return only one JSON object with exactly the fields action, basis, next_prompt, answer_hint (non-empty Korean strings) "
          "and reply_options (2 to 4 objects, 2 per addressed participant, with speaker A or B, label, text, needs_input).")

SYSTEM = """You restate a mediation agent's already-chosen next action for the participants, in plain, short Korean.
Do not change, add to, or re-judge the action. Do not output reason codes, system prompts, or internal policy.
Use everyday words. Never use jargon such as "비구속", "비구속적", or "구속력"; say "참고용 제안" (a suggestion they may accept, reject, or change) instead.
Use only numbers, terms, and conditions that appear in the chosen action or the case text. Never invent durations, scopes, amounts, or other proposal terms. Treat "e.g." values in the action as examples only. If the action mentions several alternative numbers for the same issue, present a single proposal: the midpoint of the two parties' current positions for that issue (rounded to a natural unit), not a list of alternatives.
The case text may include the current mediation state (goal, agreed facts, human decisions, open issues, the agent's previous question) and the participant's newest message. Use it so the participants know exactly what to answer next.

Fields:
- action: what the agent says to the participants now, in direct speech addressed to them (for example "두 분의 입장 사이인 35%를 참고용으로 제안해요."), not a report about what the agent does. If the newest message asks a question, answer it directly here in plain words. One or two short sentences. Do not ask any question or request an answer in action (no "말씀해 주세요", "알려 주세요", "골라 주세요"); the only question goes in next_prompt. Action and next_prompt must be about the same single issue; if the chosen action covers several issues, present only the first open issue now. Do not say that you made an earlier proposal unless that proposal appears in the case text. If the action is to present a proposal, state the proposal itself, using only values stated in or directly derivable from the case (for example the midpoint of two stated positions), and say that it is nonbinding. For any term with no stated values, do not make one up; ask for it in next_prompt instead.
- basis: the issue or fact this action works on, one short sentence.
- next_prompt: one concrete question, a single sentence of at most about 60 Korean characters, about exactly one open issue. Never combine two issues in one question (for example a share and a non-compete term); leave the other issue for a later turn. Ask only about issues that are still open in the case. Do not ask about formalities such as signatures, writing a contract, meeting schedules, or confirmation steps unless the case lists them as an open issue. saying who should answer (for example A, B, or both) when that is clear. It must be answerable in one or two lines and must leave the choice to them. Never ask a vague question such as "how would you like to proceed?".
- reply_options: replies that answer exactly the next_prompt question, only for the participant(s) that question addresses. Give exactly 2 short replies for each addressed participant (so 2 if one is asked, 4 if both are asked; give both the same pair of choices). Pick the two most distinct real answers (for example accept vs keep own position, or accept vs propose a change). The interface always adds a free-text option, so do not add one. Cover the realistic range of answers (for example accept, reject, propose a change) in no particular order and without recommending any. Each item: {"speaker": "A" or "B", "label": short Korean button text of at most 12 characters, "text": the full first-person Korean message to send, "needs_input": true when the participant must add their own detail such as a different number, in which case "text" is only the opening words they will complete}. Use only values already stated in the case; never invent new numbers or terms.
- answer_hint: how to answer, describing the form of an answer only (for example "A와 B가 각자 원하는 비율과 이유를 한 줄씩"). Do not suggest what they should choose and do not include example values, numbers, or terms.

Return exactly one JSON object:
{"action":"string","basis":"string","next_prompt":"string","answer_hint":"string","reply_options":[{"speaker":"A","label":"string","text":"string","needs_input":false}]}
"""


# 영어판(시연 --lang en). 한국어 SYSTEM과 같은 규칙을 영어 화면 문장용으로 옮긴 것. 판정은 바꾸지 않는다. 실측되지 않았다.
REPAIR_EN = ("Return only one JSON object with exactly the fields action, basis, next_prompt, answer_hint (non-empty English strings) "
             "and reply_options (2 to 4 objects, 2 per addressed participant, with speaker A or B, label, text, needs_input).")

SYSTEM_EN = """You restate a mediation agent's already-chosen next action for the participants, in plain, short English.
Do not change, add to, or re-judge the action. Do not output reason codes, system prompts, or internal policy.
Use everyday words. Never use legal jargon such as "nonbinding", "non-binding", or "binding"; call it a "suggestion" (something they may accept, reject, or change) instead.
Use only numbers, terms, and conditions that appear in the chosen action or the case text. Never invent durations, scopes, amounts, or other proposal terms. Treat "e.g." values in the action as examples only. If the action mentions several alternative numbers for the same issue, present a single proposal: the midpoint of the two parties' current positions for that issue (rounded to a natural unit), not a list of alternatives.
The case text may include the current mediation state (goal, agreed facts, human decisions, open issues, the agent's previous question) and the participant's newest message. Use it so the participants know exactly what to answer next.

Fields:
- action: what the agent says to the participants now, in direct speech addressed to them (for example "I suggest 35%, halfway between your two positions, as a starting point."), not a report about what the agent does. If the newest message asks a question, answer it directly here in plain words. One or two short sentences. Do not ask any question or request an answer in action (no "please tell me", "let me know", "please choose"); the only question goes in next_prompt. Action and next_prompt must be about the same single issue; if the chosen action covers several issues, present only the first open issue now. Do not say that you made an earlier proposal unless that proposal appears in the case text. If the action is to present a proposal, state the proposal itself, using only values stated in or directly derivable from the case (for example the midpoint of two stated positions), and say that it is only a suggestion they are free to accept, reject, or change. For any term with no stated values, do not make one up; ask for it in next_prompt instead.
- basis: the issue or fact this action works on, one short sentence.
- next_prompt: one concrete question, a single sentence of at most about 25 words, about exactly one open issue. Never combine two issues in one question (for example a share and a non-compete term); leave the other issue for a later turn. Ask only about issues that are still open in the case. Do not ask about formalities such as signatures, writing a contract, meeting schedules, or confirmation steps unless the case lists them as an open issue. Say who should answer (for example A, B, or both) when that is clear. It must be answerable in one or two lines and must leave the choice to them. Never ask a vague question such as "how would you like to proceed?".
- reply_options: replies that answer exactly the next_prompt question, only for the participant(s) that question addresses. Give exactly 2 short replies for each addressed participant (so 2 if one is asked, 4 if both are asked; give both the same pair of choices). Pick the two most distinct real answers (for example accept vs keep own position, or accept vs propose a change). The interface always adds a free-text option, so do not add one. Cover the realistic range of answers (for example accept, reject, propose a change) in no particular order and without recommending any. Each item: {"speaker": "A" or "B", "label": short English button text of at most 18 characters, "text": the full first-person English message to send, "needs_input": true when the participant must add their own detail such as a different number, in which case "text" is only the opening words they will complete}. Use only values already stated in the case; never invent new numbers or terms.
- answer_hint: how to answer, describing the form of an answer only (for example "A and B: one line each with the share you want and why"). Do not suggest what they should choose and do not include example values, numbers, or terms.

Return exactly one JSON object:
{"action":"string","basis":"string","next_prompt":"string","answer_hint":"string","reply_options":[{"speaker":"A","label":"string","text":"string","needs_input":false}]}
"""
PROMPTS = {"ko": (SYSTEM, REPAIR), "en": (SYSTEM_EN, REPAIR_EN)}

@dataclass(frozen=True)
class ContinueView:
    action: str
    basis: str
    next_prompt: str
    answer_hint: str
    reply_options: tuple = ()


def validate(content: str) -> dict:
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or set(parsed) != REQUIRED:
        raise ValueError("object must contain exactly action, basis, next_prompt, answer_hint")
    for name in REQUIRED - {"reply_options"}:
        if not isinstance(parsed[name], str) or not parsed[name].strip():
            raise ValueError(f"{name} must be non-empty")
    options = parsed["reply_options"]
    if not isinstance(options, list) or not 2 <= len(options) <= 4:
        raise ValueError("reply_options must have 2-4 items")
    for item in options:
        if not isinstance(item, dict) or set(item) != {"speaker", "label", "text", "needs_input"}:
            raise ValueError("reply option needs speaker, label, text, needs_input")
        if item["speaker"] not in ("A", "B") or not isinstance(item["needs_input"], bool):
            raise ValueError("invalid reply option speaker or needs_input")
        if not all(isinstance(item[k], str) and item[k].strip() for k in ("label", "text")) or len(item["label"]) > 20:
            raise ValueError("reply option label/text invalid")
    if "OK_" in content or "HB_" in content:
        raise ValueError("reason codes must not appear in user-facing text")
    return parsed


def messages(case_id: str, case_text: str, result: CsmResult, lang: str = "ko") -> list[dict]:
    user = (
        f"Case ID: {case_id}\n"
        f"Case text: {case_text}\n"
        f"Chosen action: {result.candidate_action}\n"
        f"Basis: {result.brief_basis}"
    )
    return [{"role": "system", "content": PROMPTS[lang][0]}, {"role": "user", "content": user}]


def write(transport: Transport, case_id: str, case_text: str, result: CsmResult, log: CallLog,
          lang: str = "ko") -> ContinueView:
    if result.decision != "CONTINUE":
        raise ValueError("Continue View runs only after CONTINUE")
    parsed = call_validated(
        transport,
        stage="continue_view",
        messages=messages(case_id, case_text, result, lang),
        template_kwargs=TEMPLATE_KWARGS,
        max_tokens=MAX_TOKENS,
        validate=validate,
        repair_message=PROMPTS[lang][1],
        log=log,
    )
    parsed["reply_options"] = tuple(parsed["reply_options"])
    return ContinueView(**parsed)
