"""영어판(--lang en) 테스트 (API 호출 없음).

확인하는 것: 판정 단계(Security Gate·CSM) 호출은 언어와 무관하게 같은 실측 프롬프트를 쓴다,
화면 문장 단계만 영어 프롬프트로 바뀐다, 영어 시나리오가 한국어판과 같은 구조다,
한국어판 상태 요약(build_case_text)은 영어판 추가 전과 글자 단위로 같다.
"""
import dataclasses
import json
import unittest

import demo_scenario
import demo_scenario_en
import demo_server
from demo_text import TEXT
from handback_agent import continue_view, csm, handback_brief, pipeline, position_tracker, security_gate
from helpers import BRIEF, CSM_RECORDED, PASS, SEC_RECORDED, STATE, VIEW, ReplayTransport, recorded


class EnglishReplay(ReplayTransport):
    STAGES = {**ReplayTransport.STAGES,
              continue_view.SYSTEM_EN: "continue_view", handback_brief.SYSTEM_EN: "handback_brief"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.systems = []

    def __call__(self, messages, template_kwargs, max_tokens):
        self.systems.append(messages[0]["content"])
        return super().__call__(messages, template_kwargs, max_tokens)


class EnglishPipeline(unittest.TestCase):
    def setUp(self):
        self.transport = EnglishReplay(recorded(SEC_RECORDED), recorded(CSM_RECORDED))

    def test_continue_uses_same_judgment_prompts(self):
        self.transport.csm["S01"] = self.transport.csm["E04"]
        out = pipeline.run(self.transport, input_text="-", case_text="x", state=STATE, request_id="S01", lang="en")
        self.assertEqual(out["status"], "CONTINUE")
        self.assertEqual(self.transport.systems, [security_gate.SYSTEM, csm.SYSTEM, continue_view.SYSTEM_EN])

    def test_handback_uses_english_brief(self):
        self.transport.csm["S01"] = self.transport.csm["E13"]
        out = pipeline.run(self.transport, input_text="-", case_text="x", state=STATE, request_id="S01", lang="en")
        self.assertEqual(out["status"], "HANDBACK")
        self.assertEqual(out["primary_reason"], "HB_ROLE_TRANSITION")
        self.assertEqual(self.transport.systems, [security_gate.SYSTEM, csm.SYSTEM, handback_brief.SYSTEM_EN])

    def test_block_uses_english_safe_alternative(self):
        out = pipeline.run(self.transport, input_text="-", case_text="x", state=STATE, request_id="S08", lang="en")
        self.assertEqual(out["status"], "SECURITY_BLOCK")
        self.assertEqual(out["safe_alternative"], pipeline.SAFE_ALTERNATIVES_EN[out["security"]["reason"]])
        self.assertEqual(self.transport.systems, [security_gate.SYSTEM])

    def test_safe_alternatives_cover_every_reason(self):
        self.assertEqual(set(pipeline.SAFE_ALTERNATIVES_EN), set(pipeline.SAFE_ALTERNATIVES))

    def test_english_plain_is_identity(self):
        self.assertEqual(pipeline.plain("a nonbinding suggestion", "en"), "a nonbinding suggestion")


class EnglishTracker(unittest.TestCase):
    PREV = [{"issue": "Extra merchandise fee", "A": "$3,000", "B": "$1,000", "proposal": None, "status": "apart"}]

    def test_english_statuses(self):
        validate = position_tracker.validate_against(self.PREV, "en")
        ok = {"issues": [{**self.PREV[0], "B": "$2,000 accepted", "proposal": "$2,000", "status": "apart"}]}
        self.assertEqual(validate(json.dumps(ok))["issues"][0]["B"], "$2,000 accepted")
        with self.assertRaises(ValueError):
            validate(json.dumps({"issues": [{**self.PREV[0], "status": "입장 차이"}]}, ensure_ascii=False))
        for status in position_tracker.STATUSES_EN:
            self.assertIn(f'"{status}"', position_tracker.SYSTEM_EN)

    def test_progress_reads_dollars_and_clock_times(self):
        baselines = {}
        rows = [{"issue": "fee", "A": "$3,000", "B": "$1,000", "proposal": None, "status": "apart"},
                {"issue": "time", "A": "9 pm", "B": "11 pm", "proposal": None, "status": "apart"}]
        self.assertEqual(position_tracker.progress(rows, baselines)["overall"], 0)
        rows[0]["B"] = "$2,000"; rows[1]["A"] = "10:30 pm"
        p = position_tracker.progress(rows, baselines)
        self.assertEqual([i["score"] for i in p["issues"]], [50, 75])
        rows[0]["status"] = "agreed"
        self.assertEqual(position_tracker.progress(rows, baselines)["agreed"], 1)


# 한국어판 id → (영어판 id, 영어판 버튼 id 머리말). 층간소음은 영어판에서 임대 보증금 분쟁으로 바꿨다(2026-10-09)
LOCALIZED = {"noise": ("deposit", "dp_")}


class EnglishScenarios(unittest.TestCase):
    def test_same_structure_as_korean(self):
        self.assertEqual(set(demo_scenario_en.OUTCOMES), set(demo_scenario.OUTCOMES))
        self.assertEqual(demo_scenario_en.DEFAULT_SCENARIO, demo_scenario.DEFAULT_SCENARIO)
        for ko, en in zip(demo_scenario.SCENARIOS, demo_scenario_en.SCENARIOS, strict=True):
            self.assertEqual(set(ko), set(en))
            # 영어판에서 현지화한 시나리오는 id·버튼 id만 다르고 결과 유형·말하는 사람 구성은 같아야 한다
            sid, prefix = LOCALIZED.get(ko["id"], (ko["id"], None))
            self.assertEqual(sid, en["id"])
            self.assertEqual(set(ko["roles"]), set(en["roles"]))
            ren = lambda i: prefix + i.split("_", 1)[1] if prefix else i
            self.assertEqual([(ren(p["id"]), p["outcome"], p["speaker"], p["case_text"] is None) for p in ko["presets"]],
                             [(p["id"], p["outcome"], p["speaker"], p["case_text"] is None) for p in en["presets"]])
            self.assertEqual(ko["opening"]["case_text"] is None, en["opening"]["case_text"] is None)
            self.assertEqual(len(ko["positions"]), len(en["positions"]))
            for row in en["positions"]:
                self.assertIn(row["status"], position_tracker.STATUSES_EN)
            self.assertEqual(len(ko["state"].unresolved_issues), len(en["state"].unresolved_issues))

    def test_text_keys_match(self):
        self.assertEqual(set(TEXT["ko"]), set(TEXT["en"]))


class CaseText(unittest.TestCase):
    def tearDown(self):
        demo_server.set_lang("en")

    def _session(self, lang):
        demo_server.set_lang(lang)
        s = demo_server.Session()
        s.reset("equity")
        dec = TEXT[lang]["decision_fact"]
        s.state = dataclasses.replace(s.state, agreed_facts=s.state.agreed_facts + (f"{dec}: Q → choice",))
        s.last_prompt = "prev?"
        return s

    def test_korean_case_text_unchanged(self):
        s = self._session("ko")
        st = s.state
        # 영어판 추가 전 demo_server.build_case_text 구현을 그대로 옮긴 기대값
        facts = [f for f in st.agreed_facts if not f.startswith("사람의 결정")]
        table = "; ".join(
            f"{r['issue']} — A: {r['A'] or '미제시'}, B: {r['B'] or '미제시'}, AI 제안: {r['proposal'] or '없음'}"
            for r in s.positions)
        want = " / ".join([f"중재 목표: {st.objective}", f"합의된 사실: {'; '.join(facts)}", "사람이 이미 정한 것: choice",
                           f"남은 쟁점: {'; '.join(st.unresolved_issues)}", f"쟁점별 입장: {table}",
                           "AI의 직전 질문: prev?", "A의 새 발화: hi"])
        self.assertEqual(demo_server.build_case_text(s, TEXT["ko"]["new_message"].format(who="A", text="hi")), want)

    def test_english_case_text_has_no_korean(self):
        s = self._session("en")
        text = demo_server.build_case_text(s, TEXT["en"]["new_message"].format(who="A", text="hi"))
        self.assertIn("Already decided by the authorized people: choice", text)
        self.assertFalse(any("가" <= ch <= "힣" for ch in text), text)


if __name__ == "__main__":
    unittest.main()
