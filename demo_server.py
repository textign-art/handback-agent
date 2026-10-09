#!/usr/bin/env python3
"""Handback Agent 시연 서버 (로컬 전용).

python3 demo_server.py                 # 실제 API, 응답은 runs/demo_cache.json에 저장해 재사용
python3 demo_server.py --no-cache      # 매번 새로 호출
python3 demo_server.py --prewarm       # 시작 전에 시연 프리셋을 한 번씩 호출해 캐시를 채움
python3 demo_server.py --lang ko       # 한국어판(기본은 영어판 en: demo_scenario_en.py · web/index_en.html)

상태 저장 방식은 미확정(인계서 §14)이라 서버 메모리에만 둔다. 재시작하면 처음 상태로 돌아간다.
"""
import argparse
import dataclasses
import hashlib
import importlib
import json
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from demo_text import LANGS, TEXT
from handback_agent import pipeline, position_tracker
from handback_agent.client import CallLog, OutputInvalid, cached_transport, http_transport, load_key

APP = Path(__file__).resolve().parent
ENV = APP / ".env.local" if (APP / ".env.local").exists() else APP.parent / ".env.local"   # 저장소 안 .env.local 우선, 없으면 상위 폴더(연구 작업본)
RUNS = APP / "runs"
MAX_INPUT = 2000

# 언어(main()에서 --lang으로 정한다). ko=demo_scenario.py · web/index.html, en=demo_scenario_en.py · web/index_en.html
LANG = "en"
T = TEXT[LANG]
DEFAULT_SCENARIO = OUTCOMES = SCENARIO_BY_ID = SCENARIOS = None
WEB_PAGE = {"ko": "index.html", "en": "index_en.html"}


def set_lang(lang: str):
    global LANG, T, DEFAULT_SCENARIO, OUTCOMES, SCENARIO_BY_ID, SCENARIOS
    mod = importlib.import_module("demo_scenario" if lang == "ko" else f"demo_scenario_{lang}")
    LANG, T = lang, TEXT[lang]
    DEFAULT_SCENARIO, OUTCOMES, SCENARIO_BY_ID, SCENARIOS = mod.DEFAULT_SCENARIO, mod.OUTCOMES, mod.SCENARIO_BY_ID, mod.SCENARIOS


set_lang(LANG)


class Session:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self, scenario_id: str | None = None):
        self.scenario = SCENARIO_BY_ID.get(scenario_id or "") or getattr(self, "scenario", None) or SCENARIO_BY_ID[DEFAULT_SCENARIO]
        self.state = self.scenario["state"]
        self.turns: dict[str, dict] = {}  # turn_id -> {case_text, output}
        self.history: list[dict] = []
        self.security_log = pipeline.SecurityLog()
        self.last_prompt: str | None = None  # AI가 참여자에게 마지막으로 물은 것
        self.positions = [dict(row) for row in self.scenario["positions"]]
        self.baselines: dict[str, float] = {}  # 쟁점별 처음 A·B 차이(협의 진행률 기준)
        self.progress = position_tracker.progress(self.positions, self.baselines)


def _append(items: tuple[str, ...], value: str, keep: int = 6) -> tuple[str, ...]:
    return (items + (value,))[-keep:]


def public(out: dict) -> dict:
    """화면으로 보내는 출력. 모델의 보안 판단 문장은 비밀값을 되풀이할 수 있어 보내지 않는다."""
    shown = json.loads(json.dumps(out, ensure_ascii=False))
    shown["security"].pop("brief_basis", None)
    return shown


def log_calls(turn_id: str, kind: str, out: dict | None, log: CallLog, error: str | None = None):
    RUNS.mkdir(exist_ok=True)
    entry = {
        "turn_id": turn_id,
        "kind": kind,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": out["status"] if out else None,
        "primary_reason": out["primary_reason"] if out else None,
        "error": error,
        "summary": log.summary(),
        "calls": [
            {**dataclasses.asdict(r), "estimated_cost_usd": r.estimated_cost_usd} for r in log.records
        ],
    }
    with (RUNS / "demo_calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def build_case_text(session: "Session", newest: str) -> str:
    """직접 입력·재개 때 CSM에 넘길 상태 요약. 실측 fixture는 한 줄이었으므로 이 형식은 시연 기준이다."""
    st = session.state
    dec = T["decision_fact"]
    decisions = [f.split("→")[-1].strip() for f in st.agreed_facts if f.startswith(dec)]
    facts = [f for f in st.agreed_facts if not f.startswith(dec)]
    parts = [f"{T['objective']}: {st.objective}", f"{T['facts']}: {'; '.join(facts) or T['none']}"]
    if decisions:
        parts.append(f"{T['decided']}: {'; '.join(decisions)}")
    parts.append(f"{T['open']}: {'; '.join(st.unresolved_issues) or T['none']}")
    table = "; ".join(
        f"{row['issue']} — A: {row['A'] or T['not_stated']}, B: {row['B'] or T['not_stated']}, "
        f"{T['ai_proposal']}: {row['proposal'] or T['none']}"
        for row in session.positions
    )
    parts.append(f"{T['positions']}: {table}")
    if session.last_prompt:
        parts.append(f"{T['last_prompt']}: {session.last_prompt}")
    parts.append(newest)
    return " / ".join(parts)


def run_turn(app, input_text: str, case_text: str, kind: str, on_event=None, speaker: str = "") -> tuple[dict, str]:
    session: Session = app.session
    turn_id = uuid.uuid4().hex[:10]
    log = CallLog()
    try:
        out = pipeline.run(
            app.transport,
            input_text=input_text,
            case_text=case_text,
            state=session.state,
            log=log,
            security_log=session.security_log,
            request_id=turn_id,
            on_event=on_event,
            lang=LANG,
            # 같은 입력이면 같은 Case ID를 써서 시연 캐시가 맞도록 한다
            case_id="D" + hashlib.sha256(f"{input_text}\n{case_text}".encode()).hexdigest()[:8],
        )
    except (OutputInvalid, pipeline.IncompleteHandback, RuntimeError) as error:
        log_calls(turn_id, kind, None, log, error=type(error).__name__)
        raise
    log_calls(turn_id, kind, out, log)
    session.turns[turn_id] = {"case_text": case_text, "output": out}
    if out["status"] == "CONTINUE" and out["continue_view"]:
        session.last_prompt = out["continue_view"]["next_prompt"]
    elif out["status"] == "HANDBACK":
        session.last_prompt = out["human_decision_request"]
    if out["status"] == "CONTINUE" and out["continue_view"]:
        session.state = dataclasses.replace(
            session.state, attempted_moves=_append(session.state.attempted_moves, out["continue_view"]["action"])
        )
    session.history.append({"turn_id": turn_id, "kind": kind, "speaker": speaker, "input": input_text, "status": out["status"],
                            "reason": out["primary_reason"], "seconds": log.summary()["seconds"]})
    return out, turn_id


def track_positions(app, turn_id: str, speaker: str, message: str, out: dict, decision: str | None, emit) -> list[str]:
    """턴이 끝난 뒤 입장표 갱신. 실패해도 턴은 유지하고 표만 그대로 둔다. 바뀐 칸 목록을 돌려준다."""
    session: Session = app.session
    log = CallLog()
    emit({"type": "stage", "stage": "position_tracker", "state": "running"})
    ai_action = (out.get("continue_view") or {}).get("action") if out["status"] == "CONTINUE" else None
    before = [dict(row) for row in session.positions]
    try:
        rows = position_tracker.update(
            app.transport, "P" + turn_id, before, speaker, message, ai_action, decision, log, LANG
        )
    except (OutputInvalid, RuntimeError) as error:
        log_calls(turn_id, "positions", None, log, error=type(error).__name__)
        emit({"type": "stage", "stage": "position_tracker", "state": "done", "failed": True,
              "seconds": log.summary()["seconds"], "retries": log.summary()["retries"]})
        return []
    log_calls(turn_id, "positions", None, log)
    session.positions = rows
    session.progress = position_tracker.progress(rows, session.baselines)
    old = {row["issue"]: row for row in before}
    changed = [
        f"{row['issue']}|{side}" for row in rows for side in ("A", "B", "proposal")
        if (old.get(row["issue"]) or {}).get(side) != row[side]
    ]
    emit({"type": "stage", "stage": "position_tracker", "state": "done", "changed": len(changed),
          "seconds": log.summary()["seconds"], "retries": log.summary()["retries"]})
    return changed


class Handler(BaseHTTPRequestHandler):
    server_version = "HandbackDemo/0.1"

    def log_message(self, fmt, *args):  # 요청 원문을 콘솔에 남기지 않는다
        pass

    def _json(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _stream_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def _send_event(self, event: dict):
        self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()

    def _stream_turn(self, input_text: str, case_text: str, kind: str, extra: dict | None = None,
                     speaker: str = "", decision: str | None = None):
        self._stream_start()
        self._send_event({"type": "accepted", "kind": kind, "speaker": speaker, "input": input_text, "case_text": case_text})
        try:
            out, turn_id = run_turn(self.server, input_text, case_text, kind, on_event=self._send_event, speaker=speaker)
        except (OutputInvalid, RuntimeError) as error:
            if kind == "resume":  # 실패한 재개는 결정으로 남기지 않는다
                s = self.server.session
                s.state = dataclasses.replace(s.state, agreed_facts=s.state.agreed_facts[:-1])
            message = T["err_invalid"] if isinstance(error, OutputInvalid) else T["err_call"]
            return self._send_event({"type": "error", "error": message})
        if kind == "resume" and out["status"] == "SECURITY_BLOCK":  # 차단된 입력은 결정으로 남기지 않는다
            s = self.server.session
            s.state = dataclasses.replace(s.state, agreed_facts=s.state.agreed_facts[:-1])
        self._send_event({"type": "result", "turn_id": turn_id, "output": public(out), **(extra or {}), **self._snapshot()})
        # 차단된 입력과, AI에게 결정을 맡기려는 요청(HANDBACK)은 입장으로 기록하지 않는다
        if out["status"] not in ("SECURITY_BLOCK", "HANDBACK"):
            changed = track_positions(self.server, turn_id, speaker, input_text, out, decision, self._send_event)
            self._send_event({"type": "positions", "positions": self.server.session.positions, "changed": changed,
                              "progress": self.server.session.progress})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 16_000:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def _snapshot(self) -> dict:
        s = self.server.session
        return {
            "state": dataclasses.asdict(s.state),
            "history": s.history[-30:],
            "positions": s.positions,
            "progress": s.progress,
            "scenario": s.scenario["id"],
            "outcomes": OUTCOMES,
            "scenarios": [
                {"id": sc["id"], "title": sc["title"], "summary": sc["summary"], "background": sc["background"], "roles": sc["roles"], "positions": sc["positions"],
                 "analysis": sc["opening"]["analysis"],
                 "presets": [{k: p[k] for k in ("id", "label", "outcome", "speaker", "input_text")} for p in sc["presets"]]}
                for sc in SCENARIOS
            ],
        }

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            data = (APP / "web" / WEB_PAGE[LANG]).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/session":
            with self.server.session.lock:
                self._json(200, self._snapshot())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": T["err_bad_request"]})
        session = self.server.session
        with session.lock:
            try:
                if self.path == "/api/reset":
                    session.reset(body.get("scenario"))
                    return self._json(200, self._snapshot())
                if self.path == "/api/turn":
                    return self._turn(body)
                if self.path == "/api/decide":
                    return self._decide(body)
            except OutputInvalid:
                return self._json(502, {"error": T["err_invalid"]})
            except RuntimeError:
                return self._json(502, {"error": T["err_call"]})
        self._json(404, {"error": "not found"})

    def _turn(self, body: dict):
        if body.get("opening"):
            # 시나리오 시작 시 AI가 먼저 양측 입장 범위를 분석한다(참여자 발화가 아니다)
            opening = self.server.session.scenario["opening"]
            case_text = opening["case_text"] or build_case_text(
                self.server.session, T["opening"])
            return self._stream_turn(opening["input_text"], case_text, "opening", speaker="AI")
        presets = self.server.session.scenario["presets"]
        preset = next((p for p in presets if p["id"] == body.get("preset_id")), None) or next(
            (p for p in presets if p["input_text"] == str(body.get("input_text") or "").strip()), None
        )
        if preset:
            input_text = preset["input_text"]
            speaker = preset["speaker"]
            case_text = preset["case_text"] or build_case_text(
                self.server.session, T["new_message"].format(who=speaker, text=input_text))
        else:
            input_text = str(body.get("input_text") or "").strip()
            if not input_text or len(input_text) > MAX_INPUT:
                return self._json(400, {"error": T["err_input_len"].format(max=MAX_INPUT)})
            speaker = body.get("speaker") if body.get("speaker") in ("A", "B", "A·B") else "A"
            # A·B: 둘 다에게 물은 질문에 두 사람이 함께 답한 것("A: … / B: …")
            who = T["both"] if speaker == "A·B" else speaker
            case_text = build_case_text(self.server.session, T["new_message"].format(who=who, text=input_text))
        self._stream_turn(input_text, case_text, "turn", speaker=speaker)

    def _decide(self, body: dict):
        session = self.server.session
        turn = session.turns.get(str(body.get("turn_id") or ""))
        choice = str(body.get("choice") or "").strip()
        if not turn or turn["output"]["status"] != "HANDBACK":
            return self._json(400, {"error": T["err_no_handback"]})
        if not choice or len(choice) > MAX_INPUT:
            return self._json(400, {"error": T["err_empty_decision"]})
        # 사람의 결정도 Security Gate를 다시 지난다. PASS면 결정을 상태에 남기고 CSM부터 다시 시작한다.
        decided = f"{turn['output']['human_decision_request']} → {choice}"
        session.state = dataclasses.replace(
            session.state, agreed_facts=_append(session.state.agreed_facts, f"{T['decision_fact']}: {decided}", keep=8)
        )
        # 원래 요청 문장을 그대로 넘기면 같은 Handback이 반복된다. 이미 사람이 정한 상태로 넘긴다.
        blocked = (turn["output"]["context_brief"] or {}).get("blocked_action") or ""
        case_text = build_case_text(session, T["resume"].format(choice=choice, blocked=blocked))
        self._stream_turn(choice, case_text, "resume", {"resumed_from": body["turn_id"]},
                          speaker="A·B", decision=decided)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--prewarm", action="store_true")
    parser.add_argument("--lang", choices=LANGS, default="en", help="화면·시나리오 언어 (기본 en, 한국어판은 ko)")
    args = parser.parse_args()
    set_lang(args.lang)

    transport = http_transport(load_key(ENV))
    if not args.no_cache:
        transport = cached_transport(transport, RUNS / "demo_cache.json")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.transport = transport
    server.session = Session()

    if args.prewarm:
        for sc in SCENARIOS:
            for preset in sc["presets"]:
                server.session.reset(sc["id"])
                case_text = preset["case_text"] or build_case_text(
                    server.session, T["new_message"].format(who=preset["speaker"], text=preset["input_text"]))
                out, _ = run_turn(server, preset["input_text"], case_text, "prewarm")
                print(f"{preset['id']}: {out['status']} {out['primary_reason']}", flush=True)
        server.session.reset(DEFAULT_SCENARIO)

    print(f"http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
