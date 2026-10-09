"""Position Tracker v0.1 — 턴이 끝난 뒤 쟁점별 A·B 입장과 AI 제안을 기록한다(시연 기준).

판정에는 관여하지 않는다. CSM·Security Gate 결과를 바꾸지 않고, 화면의 입장표와
다음 턴 CSM 입력의 상태 요약에만 쓴다. 보안 차단된 입력에는 호출하지 않는다.
"""
import json
import re

from .client import CallLog, Transport, call_validated

TEMPLATE_KWARGS = {"enable_thinking": False}
MAX_TOKENS = 512
MAX_ISSUES = 6
MAX_VALUE = 40
STATUSES = ("미제시", "한쪽 제시", "입장 차이", "합의")
REPAIR = (
    'Return only one JSON object {"issues":[{"issue":"...","A":string or null,"B":string or null,'
    '"proposal":string or null,"status":"미제시|한쪽 제시|입장 차이|합의"}]} that keeps every existing issue.'
)

SYSTEM = """You keep a position table for a mediation between two parties, A and B. You do not judge or decide anything.

Update the table using only the newest turn:
- Change a party's position only when that party explicitly states, changes, accepts, or rejects something in the newest message. Use the speaker label to know whose words they are. If the speaker is unclear, do not change any party's position.
- Record the AI's nonbinding proposal for an issue only from the given AI action text, and only when it states a concrete proposal.
- A human decision that was just made may be recorded under the issue it settles, for the parties who made it.
- A party's position is the outcome that party now wants. If a party accepts a proposal, their position becomes the accepted value, for example "35% 수락". If a party rejects it without a new number, keep their own value first and add only the latest response in short parentheses, for example "30% (제안 거절)". Never accumulate earlier responses or list several proposals in one value. When they state a new number, that number comes first.
- Never infer, guess, or invent a position or a number. Keep unchanged values exactly as they are.
- status for each issue: "미제시" (neither party stated a position), "한쪽 제시" (only one did), "입장 차이" (both did and they differ), "합의" (both explicitly agreed to the same outcome). Use "합의" only when both parties have explicitly accepted the same outcome; never because positions look close. The AI proposal is not a party position: an issue where only the AI has proposed something is still "미제시".
- Keep every existing issue in the same order. Add a new issue only if a party explicitly raises a new topic.
- Values are short Korean phrases of at most 16 characters, such as "40%", "33%면 수용", "35% 수락", "밤 11시 (제안 거절)". Use null when nothing has been stated.

Return exactly one JSON object:
{"issues":[{"issue":"string","A":"string or null","B":"string or null","proposal":"string or null","status":"미제시 | 한쪽 제시 | 입장 차이 | 합의"}]}
"""


# 영어판(시연 --lang en): 같은 규칙, 영어 값과 영어 상태 이름. 실측되지 않았다.
STATUSES_EN = ("not stated", "one side stated", "apart", "agreed")
REPAIR_EN = (
    'Return only one JSON object {"issues":[{"issue":"...","A":string or null,"B":string or null,'
    '"proposal":string or null,"status":"not stated|one side stated|apart|agreed"}]} that keeps every existing issue.'
)

SYSTEM_EN = """You keep a position table for a mediation between two parties, A and B. You do not judge or decide anything.

Update the table using only the newest turn:
- Change a party's position only when that party explicitly states, changes, accepts, or rejects something in the newest message. Use the speaker label to know whose words they are. If the speaker is unclear, do not change any party's position.
- Record the AI's suggestion for an issue only from the given AI action text, and only when it states a concrete proposal.
- A human decision that was just made may be recorded under the issue it settles, for the parties who made it.
- A party's position is the outcome that party now wants. If a party accepts a proposal, their position becomes the accepted value, for example "35% accepted". If a party rejects it without a new number, keep their own value first and add only the latest response in short parentheses, for example "30% (rejected)". Never accumulate earlier responses or list several proposals in one value. When they state a new number, that number comes first.
- Never infer, guess, or invent a position or a number. Keep unchanged values exactly as they are.
- status for each issue: "not stated" (neither party stated a position), "one side stated" (only one did), "apart" (both did and they differ), "agreed" (both explicitly agreed to the same outcome). Use "agreed" only when both parties have explicitly accepted the same outcome; never because positions look close. The AI suggestion is not a party position: an issue where only the AI has proposed something is still "not stated".
- Keep every existing issue in the same order. Add a new issue only if a party explicitly raises a new topic.
- Values are short English phrases of at most 20 characters, such as "40%", "OK at 33%", "35% accepted", "11 pm (rejected)", "$2,000". Use null when nothing has been stated.

Return exactly one JSON object:
{"issues":[{"issue":"string","A":"string or null","B":"string or null","proposal":"string or null","status":"not stated | one side stated | apart | agreed"}]}
"""
PROMPTS = {"ko": (SYSTEM, REPAIR, STATUSES), "en": (SYSTEM_EN, REPAIR_EN, STATUSES_EN)}

def _value(v):
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError("values must be strings or null")
    v = v.strip()
    # 긴 값 때문에 표 전체를 버리지 않는다: 화면에 맞게 줄여 기록한다
    return (v[: MAX_VALUE - 1] + "…" if len(v) > MAX_VALUE else v) or None


def validate_against(previous: list[dict], lang: str = "ko"):
    statuses = PROMPTS[lang][2]
    names = [row["issue"] for row in previous]

    def validate(content: str) -> dict:
        parsed = json.loads(content)
        if not isinstance(parsed, dict) or set(parsed) != {"issues"} or not isinstance(parsed["issues"], list):
            raise ValueError("object must contain only issues")
        rows = parsed["issues"]
        if not 1 <= len(rows) <= MAX_ISSUES:
            raise ValueError("too many or no issues")
        out = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"issue", "A", "B", "proposal", "status"}:
                raise ValueError("each issue needs issue, A, B, proposal, status")
            if row["status"] not in statuses:
                raise ValueError("invalid status")
            if not isinstance(row["issue"], str) or not row["issue"].strip():
                raise ValueError("issue name must be non-empty")
            out.append({"issue": row["issue"].strip(), "A": _value(row["A"]), "B": _value(row["B"]),
                        "proposal": _value(row["proposal"]), "status": row["status"]})
        if [r["issue"] for r in out[: len(names)]] != names:
            raise ValueError("existing issues must be kept in order")
        # 시연에서는 쟁점 목록을 시나리오에 정한 것으로 고정한다(사람의 결정 등이 새 쟁점으로 붙지 않게)
        return {"issues": out[: len(names)]}

    return validate


def update(
    transport: Transport,
    case_id: str,
    previous: list[dict],
    speaker: str,
    message: str,
    ai_action: str | None,
    decision: str | None,
    log: CallLog,
    lang: str = "ko",
) -> list[dict]:
    user = (
        f"Case ID: {case_id}\n"
        f"Current table: {json.dumps({'issues': previous}, ensure_ascii=False)}\n"
        f"Speaker: {speaker}\n"
        f"Newest message: {message}\n"
        f"AI action this turn: {ai_action or 'none'}\n"
        f"Human decision just made: {decision or 'none'}"
    )
    parsed = call_validated(
        transport,
        stage="position_tracker",
        messages=[{"role": "system", "content": PROMPTS[lang][0]}, {"role": "user", "content": user}],
        template_kwargs=TEMPLATE_KWARGS,
        max_tokens=MAX_TOKENS,
        validate=validate_against(previous, lang),
        repair_message=PROMPTS[lang][1],
        log=log,
    )
    return parsed["issues"]


def _pct(value):
    # 괄호 앞 "지금 입장"의 첫 숫자(%, 만원, 시 등). "10시 30분"은 10.5로 읽는다. 숫자가 없으면 None
    # 영어판: "10:30 pm"은 10.5, "$3,000"은 3000(천 단위 쉼표를 뺀다)
    main = (value or "").split("(")[0].replace(",", "")
    hm = re.search(r"(\d+)\s*시\s*(\d+)\s*분", main) or re.search(r"(\d+):(\d+)", main)
    if hm:
        return int(hm.group(1)) + int(hm.group(2)) / 60
    match = re.search(r"(\d+(?:\.\d+)?)", main)
    return float(match.group(1)) if match else None


STATUS_SCORE = {"미제시": 0, "한쪽 제시": 25, "입장 차이": 50, "합의": 100,
                "not stated": 0, "one side stated": 25, "apart": 50, "agreed": 100}
AGREED = ("합의", "agreed")


def progress(rows: list[dict], baselines: dict) -> dict:
    """입장표에서 계산하는 시연용 협의 진행률. 모델 판단이 아니라 계산식이다.

    숫자 쟁점: 처음 A·B 차이 대비 지금 차이가 줄어든 비율(합의 전 최대 95).
    그 밖의 쟁점: 미제시 0 · 한쪽 제시 25 · 입장 차이 50 · 합의 100을 시작 상태 대비로 환산(시작=0).
    baselines는 쟁점별 처음 차이를 기록하며 이 함수가 갱신한다.
    """
    issues = []
    for row in rows:
        a, b = _pct(row["A"]), _pct(row["B"])
        status = row.get("status", "미제시")
        item = {"issue": row["issue"], "status": status}
        if status in AGREED:
            item["score"] = 100
        elif a is not None and b is not None:
            gap = abs(a - b)
            base = baselines.setdefault(row["issue"], gap)
            item.update(gap=gap, base=base)
            item["score"] = 50 if base == 0 else round(min(95, max(0, (1 - gap / base) * 100)))
        else:
            # 숫자가 아닌 쟁점은 시작 상태 대비 얼마나 나아갔는지로 센다(시작 0)
            raw = STATUS_SCORE.get(status, 0)
            start = baselines.setdefault(f"{row['issue']}#status", raw)
            item["score"] = 100 if start >= 100 else round(max(0, (raw - start) / (100 - start) * 100))
        issues.append(item)
    overall = round(sum(i["score"] for i in issues) / len(issues)) if issues else 0
    return {"overall": overall, "agreed": sum(i["status"] in AGREED for i in issues), "total": len(issues), "issues": issues}
