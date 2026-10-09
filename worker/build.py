#!/usr/bin/env python3
"""Worker 빌드: 파이썬 정본에서 프롬프트·계약 상수·시나리오를 JS 데이터로 옮기고 화면을 복사한다.

python3 worker/build.py             (app 폴더에서 실행, 영어판 — 기본)
python3 worker/build.py --lang ko   (한국어판으로 되돌리기)
프롬프트를 손으로 옮기지 않는다. 실측한 지시문과 글자 단위로 같아야 하기 때문이다.
키·비밀값은 다루지 않는다.
"""
import argparse
import dataclasses
import importlib
import json
import shutil
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

from demo_text import LANGS, TEXT  # noqa: E402
from handback_agent import client, continue_view, csm, handback_brief, pipeline, position_tracker, security_gate  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--lang", choices=LANGS, default="en", help="화면·시나리오 언어 (기본 en, 한국어판은 ko)")
LANG = parser.parse_args().lang
scenario_mod = importlib.import_module("demo_scenario" if LANG == "ko" else f"demo_scenario_{LANG}")
OUTCOMES, SCENARIOS = scenario_mod.OUTCOMES, scenario_mod.SCENARIOS
WEB_PAGE = {"ko": "index.html", "en": "index_en.html"}[LANG]


def stage(mod, **extra):
    # 판정 단계(security_gate·csm)는 언어와 무관하게 실측 프롬프트 그대로. 화면 문장 단계만 언어별 프롬프트를 쓴다.
    system, repair = (mod.PROMPTS[LANG][:2] if hasattr(mod, "PROMPTS") else (mod.SYSTEM, mod.REPAIR))
    return {
        "system": system,
        "repair": repair,
        "max_tokens": mod.MAX_TOKENS,
        "template_kwargs": mod.TEMPLATE_KWARGS,
        **extra,
    }


data = {
    "MODEL": client.MODEL,
    "ENDPOINT": client.ENDPOINT,
    "STAGES": {
        "security_gate": stage(security_gate, reasons=list(security_gate.REASONS)),
        "csm": stage(csm, continue_reasons=list(csm.CONTINUE_REASONS), handback_reasons=list(csm.HANDBACK_REASONS)),
        "continue_view": stage(continue_view),
        "handback_brief": stage(handback_brief),
        "position_tracker": stage(position_tracker, statuses=list(position_tracker.PROMPTS[LANG][2]),
                                  max_issues=position_tracker.MAX_ISSUES, max_value=position_tracker.MAX_VALUE),
    },
    "LANG": LANG,
    "TEXT": TEXT[LANG],
    "SAFE_ALTERNATIVES": pipeline.SAFE_ALTERNATIVES_BY_LANG[LANG],
    # pipeline.plain과 같은 치환 규칙(파이썬 \1 → JS $1)
    "PLAIN": [[pat.pattern, repl.replace("\\1", "$1")] for pat, repl in pipeline.PLAIN_BY_LANG[LANG]],
    "STATUS_SCORE": position_tracker.STATUS_SCORE,
    "AGREED": list(position_tracker.AGREED),
    "OUTCOMES": OUTCOMES,
    "SCENARIOS": [{**s, "state": dataclasses.asdict(s["state"])} for s in SCENARIOS],
}

out = APP / "worker" / "src" / "gen" / "data.js"
out.write_text(
    "// 자동 생성 파일 — worker/build.py가 파이썬 정본에서 만든다. 직접 고치지 않는다.\n"
    + "".join(f"export const {k} = {json.dumps(v, ensure_ascii=False, indent=1)};\n" for k, v in data.items()),
    encoding="utf-8",
)
shutil.copyfile(APP / "web" / WEB_PAGE, APP / "worker" / "public" / "index.html")
# 화면을 고쳐 다시 배포하면 바로 보이도록 브라우저가 매번 새로 확인하게 한다
(APP / "worker" / "public" / "_headers").write_text("/*\n  Cache-Control: no-cache\n", encoding="utf-8")
print(f"[{LANG}] wrote {out.relative_to(APP)} and worker/public/index.html (from web/{WEB_PAGE})")
