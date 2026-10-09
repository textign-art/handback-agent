// Handback Agent 시연 Worker.
// 파이썬 정본(handback_agent/*, demo_server.py)의 흐름을 그대로 옮긴다:
// Security Gate → Handback CSM → Continue View | Handback Brief → Position Tracker.
// 프롬프트·계약 상수·시나리오는 worker/build.py가 파이썬에서 생성한 gen/data.js를 쓴다.
// 대화 상태는 방문자 브라우저가 들고 다니며(session), 서버는 저장하지 않는다.
// 키는 Cloudflare 비밀값 NEBIUS_TOKEN_FACTORY_KEY로만 읽고 응답·로그에 남기지 않는다.
// 언어(영어판 en · 한국어판 ko)는 build.py --lang으로 정해지고, 서버 문장은 TEXT(demo_text.py)에서 온다.
import { AGREED, ENDPOINT, MODEL, OUTCOMES, PLAIN as PLAIN_RULES, SAFE_ALTERNATIVES, SCENARIOS, STAGES, STATUS_SCORE, TEXT } from "./gen/data.js";

const MAX_INPUT = 2000;
const MAX_BODY = 96_000;
const KEEP_TURNS = 8;

class OutputInvalid extends Error {}
class ModelCallFailed extends Error {}

// ---------- 파이썬 출력과 같은 문자열 만들기 ----------
const pyStr = (v) => (v === null || v === undefined ? "None" : String(v));
// json.dumps(..., ensure_ascii=False) 와 같은 구분자(", ", ": ")
function pyDumps(v) {
  if (v === null || v === undefined) return "null";
  if (Array.isArray(v)) return "[" + v.map(pyDumps).join(", ") + "]";
  if (typeof v === "object") return "{" + Object.entries(v).map(([k, x]) => JSON.stringify(k) + ": " + pyDumps(x)).join(", ") + "}";
  return JSON.stringify(v);
}
const sameKeys = (o, keys) => o && typeof o === "object" && !Array.isArray(o)
  && Object.keys(o).length === keys.length && keys.every((k) => k in o);
const nonEmpty = (v) => typeof v === "string" && v.trim().length > 0;
const round = (n, d = 2) => Math.round(n * 10 ** d) / 10 ** d;

// ---------- 모델 호출: json_object + 서버 검증 + 1회 재시도 ----------
async function callValidated(env, log, stage, messages, validate) {
  const cfg = STAGES[stage];
  const msgs = [...messages];
  let lastError = null;
  for (const attempt of [1, 2]) {
    const started = Date.now();
    let res;
    try {
      res = await fetch(ENDPOINT, {
        method: "POST",
        headers: { Authorization: `Bearer ${env.NEBIUS_TOKEN_FACTORY_KEY}`, "Content-Type": "application/json" },
        body: JSON.stringify({
          model: MODEL, messages: msgs, temperature: 0, max_tokens: cfg.max_tokens,
          response_format: { type: "json_object" },
          chat_template_kwargs: cfg.template_kwargs,   // raw HTTP에서는 최상위
        }),
        signal: AbortSignal.timeout(180_000),
      });
    } catch (e) {
      throw new ModelCallFailed(`${stage}: network`);
    }
    if (!res.ok) throw new ModelCallFailed(`${stage}: HTTP ${res.status}`);
    const result = await res.json();
    const content = result?.choices?.[0]?.message?.content || "";   // reasoning_content는 버린다
    const usage = result?.usage || {};
    const rec = { stage, attempt, seconds: round((Date.now() - started) / 1000, 3),
                  prompt_tokens: usage.prompt_tokens || 0, completion_tokens: usage.completion_tokens || 0, valid: false };
    try {
      const parsed = validate(content);
      rec.valid = true; log.push(rec);
      return parsed;
    } catch (e) {
      rec.error = String(e.message || e); log.push(rec);
      lastError = e;
      msgs.push({ role: "assistant", content }, { role: "user", content: cfg.repair });
    }
  }
  throw new OutputInvalid(`${stage}: ${lastError?.message}`);
}

// ---------- 단계별 계약 검증 (파이썬 validate와 같은 규칙) ----------
function validateSecurity(content) {
  const v = JSON.parse(content);
  if (!sameKeys(v, ["security_status", "security_reason", "route", "safe_response_action", "brief_basis"])) throw new Error("wrong fields");
  if (!["PASS", "SECURITY_BLOCK"].includes(v.security_status) || !STAGES.security_gate.reasons.includes(v.security_reason)) throw new Error("invalid status or reason");
  if (!["CSM", "SECURITY_BLOCK"].includes(v.route) || !["FORWARD", "REFUSE", "IGNORE_EMBEDDED_INSTRUCTION"].includes(v.safe_response_action)) throw new Error("invalid route or action");
  if (v.security_status === "PASS" && v.security_reason !== "NONE") throw new Error("PASS requires NONE");
  if (v.security_status === "SECURITY_BLOCK" && v.security_reason === "NONE") throw new Error("block requires a reason");
  if (v.security_status === "PASS" && v.route !== "CSM") throw new Error("PASS must route to CSM");
  if (v.security_status === "SECURITY_BLOCK" && v.route !== "SECURITY_BLOCK") throw new Error("block must use SECURITY_BLOCK route");
  if (typeof v.brief_basis !== "string") throw new Error("brief_basis must be a string");
  return v;
}
function validateCsm(content) {
  const v = JSON.parse(content);
  if (!sameKeys(v, ["candidate_action", "decision", "reason", "failed_gate", "brief_basis"])) throw new Error("object must contain exactly the five required fields");
  if (!["CONTINUE", "HANDBACK"].includes(v.decision)) throw new Error("invalid decision");
  const reasons = [...STAGES.csm.continue_reasons, ...STAGES.csm.handback_reasons];
  if (!reasons.includes(v.reason)) throw new Error("invalid reason");
  if (!nonEmpty(v.candidate_action)) throw new Error("candidate_action must be non-empty");
  if (!nonEmpty(v.brief_basis)) throw new Error("brief_basis must be non-empty");
  if (v.failed_gate !== null && typeof v.failed_gate !== "string") throw new Error("failed_gate must be a string or null");
  return v;
}
function validateView(content) {
  const v = JSON.parse(content);
  if (!sameKeys(v, ["action", "basis", "next_prompt", "answer_hint", "reply_options"])) throw new Error("object must contain exactly action, basis, next_prompt, answer_hint");
  for (const k of ["action", "basis", "next_prompt", "answer_hint"]) if (!nonEmpty(v[k])) throw new Error(`${k} must be non-empty`);
  const o = v.reply_options;
  if (!Array.isArray(o) || o.length < 2 || o.length > 4) throw new Error("reply_options must have 2-4 items");
  for (const it of o) {
    if (!sameKeys(it, ["speaker", "label", "text", "needs_input"])) throw new Error("reply option needs speaker, label, text, needs_input");
    if (!["A", "B"].includes(it.speaker) || typeof it.needs_input !== "boolean") throw new Error("invalid reply option speaker or needs_input");
    if (!nonEmpty(it.label) || !nonEmpty(it.text) || it.label.length > 20) throw new Error("reply option label/text invalid");
  }
  if (content.includes("OK_") || content.includes("HB_")) throw new Error("reason codes must not appear in user-facing text");
  return v;
}
function validateBrief(content) {
  const v = JSON.parse(content);
  if (!sameKeys(v, ["human_decision_request", "decision_options", "resume_condition", "blocked_action", "safe_support"])) throw new Error("object must contain exactly the five required fields");
  for (const k of ["human_decision_request", "resume_condition", "blocked_action", "safe_support"]) if (!nonEmpty(v[k])) throw new Error(`${k} must be non-empty`);
  const o = v.decision_options;
  if (!Array.isArray(o) || o.length < 2 || o.length > 3 || !o.every(nonEmpty)) throw new Error("decision_options must be 2-3 non-empty strings");
  if (new Set(o).size !== o.length) throw new Error("decision_options must be distinct");
  const text = JSON.stringify(v);
  if (text.includes("HB_") || text.includes("OK_")) throw new Error("reason codes must not appear in user-facing text");
  return v;
}
function validateTracker(previous) {
  const names = previous.map((r) => r.issue);
  const cfg = STAGES.position_tracker;
  const val = (x) => {
    if (x === null || x === undefined) return null;
    if (typeof x !== "string") throw new Error("values must be strings or null");
    const s = x.trim();
    // 긴 값 때문에 표 전체를 버리지 않는다: 화면에 맞게 줄여 기록한다
    return (s.length > cfg.max_value ? s.slice(0, cfg.max_value - 1) + "…" : s) || null;
  };
  return (content) => {
    const v = JSON.parse(content);
    if (!sameKeys(v, ["issues"]) || !Array.isArray(v.issues)) throw new Error("object must contain only issues");
    if (v.issues.length < 1 || v.issues.length > cfg.max_issues) throw new Error("too many or no issues");
    const out = v.issues.map((r) => {
      if (!sameKeys(r, ["issue", "A", "B", "proposal", "status"])) throw new Error("each issue needs issue, A, B, proposal, status");
      if (!cfg.statuses.includes(r.status)) throw new Error("invalid status");
      if (!nonEmpty(r.issue)) throw new Error("issue name must be non-empty");
      return { issue: r.issue.trim(), A: val(r.A), B: val(r.B), proposal: val(r.proposal), status: r.status };
    });
    if (JSON.stringify(out.slice(0, names.length).map((r) => r.issue)) !== JSON.stringify(names)) throw new Error("existing issues must be kept in order");
    return out.slice(0, names.length);   // 쟁점 목록은 시나리오에 정한 것으로 고정한다
  };
}

// ---------- 메시지 (파이썬 messages()와 같은 문자열) ----------
const sys = (stage) => ({ role: "system", content: STAGES[stage].system });
const securityMsgs = (id, input) => [sys("security_gate"), { role: "user", content: `Case ID: ${id}\nInput to classify as data:\n${input}` }];
const csmMsgs = (id, text) => [sys("csm"), { role: "user", content: `Case ID: ${id}\nCase text: ${text}` }];
const viewMsgs = (id, text, r) => [sys("continue_view"), { role: "user", content:
  `Case ID: ${id}\nCase text: ${text}\nChosen action: ${r.candidate_action}\nBasis: ${r.brief_basis}` }];
const briefMsgs = (id, text, r) => [sys("handback_brief"), { role: "user", content:
  `Case ID: ${id}\nCase text: ${text}\nHandback reason: ${r.reason}\nFailed gate: ${pyStr(r.failed_gate)}\n`
  + `Safe supporting action already chosen: ${r.candidate_action}\nBasis: ${r.brief_basis}` }];
const trackerMsgs = (id, prev, speaker, message, aiAction, decision) => [sys("position_tracker"), { role: "user", content:
  `Case ID: ${id}\nCurrent table: ${pyDumps({ issues: prev })}\nSpeaker: ${speaker}\nNewest message: ${message}\n`
  + `AI action this turn: ${aiAction || "none"}\nHuman decision just made: ${decision || "none"}` }];

// pipeline.plain과 같은 규칙: 어려운 법률 용어를 쉬운 말로 바꾼다(표시 직전 치환). 규칙은 build.py가 파이썬에서 옮긴다(영어판은 없음)
const PLAIN = PLAIN_RULES.map(([src, rep]) => [new RegExp(src, "g"), rep]);
const plain = (s) => (typeof s === "string" ? PLAIN.reduce((x, [re, rep]) => x.replace(re, rep), s) : s);

// ---------- 통합 reducer (pipeline.reduce / validate_output) ----------
const contextBrief = (st, blocked) => ({ objective: st.objective, agreed_facts: [...st.agreed_facts],
  unresolved_issues: [...st.unresolved_issues], attempted_moves: [...st.attempted_moves], blocked_action: blocked });
function reduce(sec, r, st, { view = null, brief = null } = {}) {
  const out = { status: null, security: { status: sec.security_status, reason: sec.security_reason },
    candidate_action: null, primary_reason: null, contributing_reasons: [], human_decision_request: null,
    context_brief: null, resume_condition: null, safe_alternative: null, decision_options: [], continue_view: null };
  if (sec.security_status !== "PASS") {
    return { ...out, status: "SECURITY_BLOCK", primary_reason: sec.security_reason, safe_alternative: SAFE_ALTERNATIVES[sec.security_reason] };
  }
  if (r.decision === "CONTINUE") {
    return { ...out, status: "CONTINUE", candidate_action: r.candidate_action, primary_reason: r.reason,
      context_brief: contextBrief(st, null), continue_view: view && { action: plain(view.action), basis: plain(view.basis),
        next_prompt: plain(view.next_prompt), answer_hint: plain(view.answer_hint),
        reply_options: view.reply_options.map((o) => ({ ...o, label: plain(o.label), text: plain(o.text) })) } };
  }
  return { ...out, status: "HANDBACK", safe_alternative: plain(brief.safe_support), primary_reason: r.reason,
    human_decision_request: plain(brief.human_decision_request), decision_options: brief.decision_options.map(plain),
    context_brief: contextBrief(st, plain(brief.blocked_action)), resume_condition: plain(brief.resume_condition) };
}
function validateOutput(o) {
  if (o.status === "SECURITY_BLOCK") {
    if (!o.safe_alternative || o.human_decision_request || o.resume_condition || o.candidate_action) throw new OutputInvalid("SECURITY_BLOCK shape");
  } else if (o.status === "CONTINUE") {
    if (!o.candidate_action) throw new OutputInvalid("CONTINUE requires candidate_action");
  } else if (!(o.human_decision_request && o.context_brief && o.resume_condition)) {
    throw new OutputInvalid("HANDBACK requires human_decision_request, context_brief, resume_condition");
  }
}

// ---------- 파이프라인 (pipeline.run) ----------
async function runPipeline(env, emit, caseId, inputText, caseText, st, log) {
  async function stage(name, fn, summarize) {
    emit({ type: "stage", stage: name, state: "running" });
    const start = log.length;
    const value = await fn();
    const recs = log.slice(start);
    emit({ type: "stage", stage: name, state: "done", seconds: round(recs.reduce((s, x) => s + x.seconds, 0)),
           retries: recs.filter((x) => x.attempt > 1).length, ...summarize(value) });
    return value;
  }
  const sec = await stage("security_gate", () => callValidated(env, log, "security_gate", securityMsgs(caseId, inputText), validateSecurity),
    // 차단 시 근거 문장은 비밀값 이름을 되풀이할 수 있어 보내지 않는다
    (r) => ({ status: r.security_status, reason: r.security_reason, action: r.safe_response_action, basis: r.security_status === "PASS" ? r.brief_basis : null }));
  let out;
  if (sec.security_status !== "PASS") {
    out = reduce(sec);
  } else {
    const r = await stage("csm", () => callValidated(env, log, "csm", csmMsgs(caseId, caseText), validateCsm),
      (x) => ({ status: x.decision, reason: x.reason, failed_gate: x.failed_gate, candidate_action: x.candidate_action, basis: x.brief_basis }));
    if (r.decision === "CONTINUE") {
      const view = await stage("continue_view", () => callValidated(env, log, "continue_view", viewMsgs(caseId, caseText, r), validateView), () => ({}));
      out = reduce(sec, r, st, { view });
    } else {
      const brief = await stage("handback_brief", () => callValidated(env, log, "handback_brief", briefMsgs(caseId, caseText, r), validateBrief), () => ({}));
      out = reduce(sec, r, st, { brief });
    }
  }
  validateOutput(out);
  return out;
}

// ---------- 협의 진행률 (position_tracker.progress) ----------
// position_tracker._pct와 같은 규칙: 괄호 앞 값의 첫 숫자, "10시 30분"·"10:30 pm"은 10.5, "$3,000"은 3000
const firstNum = (v) => {
  const main = String(v || "").split("(")[0].replace(/,/g, "");
  const hm = /(\d+)\s*시\s*(\d+)\s*분/.exec(main) || /(\d+):(\d+)/.exec(main);
  if (hm) return parseInt(hm[1], 10) + parseInt(hm[2], 10) / 60;
  const m = /(\d+(?:\.\d+)?)/.exec(main); return m ? parseFloat(m[1]) : null;
};
function progress(rows, baselines) {
  const issues = rows.map((row) => {
    const a = firstNum(row.A), b = firstNum(row.B), status = row.status || STAGES.position_tracker.statuses[0];
    const item = { issue: row.issue, status };
    if (AGREED.includes(status)) item.score = 100;
    else if (a !== null && b !== null) {
      const gap = Math.abs(a - b);
      if (!(row.issue in baselines)) baselines[row.issue] = gap;
      const base = baselines[row.issue];
      Object.assign(item, { gap, base, score: base === 0 ? 50 : Math.round(Math.min(95, Math.max(0, (1 - gap / base) * 100))) });
    } else {
      const raw = STATUS_SCORE[status] ?? 0, key = `${row.issue}#status`;
      if (!(key in baselines)) baselines[key] = raw;
      const start = baselines[key];
      item.score = start >= 100 ? 100 : Math.round(Math.max(0, (raw - start) / (100 - start) * 100));
    }
    return item;
  });
  const overall = issues.length ? Math.round(issues.reduce((s, i) => s + i.score, 0) / issues.length) : 0;
  return { overall, agreed: issues.filter((i) => AGREED.includes(i.status)).length, total: issues.length, issues };
}

// ---------- 세션 (방문자 브라우저가 보관) ----------
const scenarioById = (id) => SCENARIOS.find((s) => s.id === id) || SCENARIOS[0];
function newSession(id) {
  const sc = scenarioById(id);
  const sess = { scenario: sc.id, state: structuredClone(sc.state), positions: structuredClone(sc.positions),
    baselines: {}, progress: null, last_prompt: null, turns: {}, history: [] };
  sess.progress = progress(sess.positions, sess.baselines);
  return sess;
}
function loadSession(raw, fallbackScenario) {
  if (!raw || typeof raw !== "object" || !SCENARIOS.some((s) => s.id === raw.scenario)
      || !raw.state || !Array.isArray(raw.positions)) return newSession(fallbackScenario);
  return { baselines: {}, turns: {}, history: [], last_prompt: null, ...raw };
}
function snapshot(sess) {
  return {
    state: sess.state, history: sess.history.slice(-30), positions: sess.positions, progress: sess.progress,
    scenario: sess.scenario, outcomes: OUTCOMES,
    scenarios: SCENARIOS.map((sc) => ({ id: sc.id, title: sc.title, summary: sc.summary, background: sc.background, roles: sc.roles,
      positions: sc.positions, analysis: sc.opening.analysis,
      presets: sc.presets.map(({ id, label, outcome, speaker, input_text }) => ({ id, label, outcome, speaker, input_text })) })),
    session: sess,
  };
}
// demo_text.py 템플릿 채우기: str.format과 같이 {name}만 바꾼다(값 속 $ 등은 그대로)
const fill = (tpl, vals) => tpl.replace(/\{(\w+)\}/g, (m, k) => (k in vals ? String(vals[k]) : m));
// demo_server.build_case_text
function buildCaseText(sess, newest) {
  const st = sess.state;
  const T = TEXT;
  const decisions = st.agreed_facts.filter((f) => f.startsWith(T.decision_fact)).map((f) => f.split("→").pop().trim());
  const facts = st.agreed_facts.filter((f) => !f.startsWith(T.decision_fact));
  const parts = [`${T.objective}: ${st.objective}`, `${T.facts}: ${facts.join("; ") || T.none}`];
  if (decisions.length) parts.push(`${T.decided}: ${decisions.join("; ")}`);
  parts.push(`${T.open}: ${st.unresolved_issues.join("; ") || T.none}`);
  parts.push(`${T.positions}: ${sess.positions.map((r) => `${r.issue} — A: ${r.A || T.not_stated}, B: ${r.B || T.not_stated}, ${T.ai_proposal}: ${r.proposal || T.none}`).join("; ")}`);
  if (sess.last_prompt) parts.push(`${T.last_prompt}: ${sess.last_prompt}`);
  parts.push(newest);
  return parts.join(" / ");
}
const append = (arr, v, keep = 6) => [...arr, v].slice(-keep);
async function caseIdFor(input, caseText) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`${input}\n${caseText}`));
  return "D" + [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 8);
}
const turnId = () => crypto.randomUUID().replace(/-/g, "").slice(0, 10);

// ---------- 한 턴 (demo_server._stream_turn) ----------
async function streamTurn(env, emit, sess, { inputText, caseText, kind, speaker, decision = null, extra = {} }) {
  emit({ type: "accepted", kind, speaker, input: inputText, case_text: caseText });
  const log = [];
  const id = turnId();
  let out;
  try {
    out = await runPipeline(env, emit, await caseIdFor(inputText, caseText), inputText, caseText, sess.state, log);
  } catch (e) {
    if (kind === "resume") sess.state.agreed_facts = sess.state.agreed_facts.slice(0, -1);   // 실패한 재개는 결정으로 남기지 않는다
    console.log(JSON.stringify({ kind, error: e.constructor.name, calls: log.length }));
    emit({ type: "error", error: e instanceof OutputInvalid ? TEXT.err_invalid : TEXT.err_call, session: sess });
    return;
  }
  console.log(JSON.stringify({ kind, status: out.status, reason: out.primary_reason,
    calls: log.map(({ stage, attempt, seconds, prompt_tokens, completion_tokens, valid }) => ({ stage, attempt, seconds, prompt_tokens, completion_tokens, valid })) }));
  if (kind === "resume" && out.status === "SECURITY_BLOCK") sess.state.agreed_facts = sess.state.agreed_facts.slice(0, -1);
  sess.turns[id] = { case_text: caseText, output: out };
  const ids = Object.keys(sess.turns); if (ids.length > KEEP_TURNS) for (const k of ids.slice(0, -KEEP_TURNS)) delete sess.turns[k];
  if (out.status === "CONTINUE" && out.continue_view) {
    sess.last_prompt = out.continue_view.next_prompt;
    sess.state.attempted_moves = append(sess.state.attempted_moves, out.continue_view.action);
  } else if (out.status === "HANDBACK") sess.last_prompt = out.human_decision_request;
  sess.history.push({ turn_id: id, kind, speaker, input: inputText, status: out.status, reason: out.primary_reason,
    seconds: round(log.reduce((s, x) => s + x.seconds, 0), 3) });
  sess.history = sess.history.slice(-30);
  emit({ type: "result", turn_id: id, output: out, ...extra, ...snapshot(sess) });

  // 차단된 입력과, AI에게 결정을 맡기려는 요청(HANDBACK)은 입장으로 기록하지 않는다
  if (out.status === "SECURITY_BLOCK" || out.status === "HANDBACK") return;
  // 입장 기록: 실패해도 턴은 유지하고 표만 그대로 둔다
  const tlog = [];
  emit({ type: "stage", stage: "position_tracker", state: "running" });
  const before = structuredClone(sess.positions);
  const aiAction = out.status === "CONTINUE" ? out.continue_view?.action : null;
  let changed = [];
  try {
    const rows = await callValidated(env, tlog, "position_tracker", trackerMsgs("P" + id, before, speaker, inputText, aiAction, decision), validateTracker(before));
    sess.positions = rows;
    sess.progress = progress(rows, sess.baselines);
    const old = Object.fromEntries(before.map((r) => [r.issue, r]));
    changed = rows.flatMap((r) => ["A", "B", "proposal"].filter((s) => (old[r.issue] || {})[s] !== r[s]).map((s) => `${r.issue}|${s}`));
    emit({ type: "stage", stage: "position_tracker", state: "done", changed: changed.length, seconds: round(tlog.reduce((s, x) => s + x.seconds, 0)), retries: tlog.filter((x) => x.attempt > 1).length });
  } catch (e) {
    emit({ type: "stage", stage: "position_tracker", state: "done", failed: true, seconds: round(tlog.reduce((s, x) => s + x.seconds, 0)), retries: 0 });
  }
  emit({ type: "positions", positions: sess.positions, changed, progress: sess.progress, session: sess });
}

// ---------- HTTP ----------
const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" } });

function ndjson(ctx, run) {
  const { readable, writable } = new TransformStream();
  const writer = writable.getWriter(), enc = new TextEncoder();
  const emit = (e) => writer.write(enc.encode(JSON.stringify(e) + "\n")).catch(() => {});
  ctx.waitUntil((async () => {
    try { await run(emit); } catch (e) { emit({ type: "error", error: TEXT.err_internal }); }
    finally { await writer.close().catch(() => {}); }
  })());
  return new Response(readable, { headers: { "Content-Type": "application/x-ndjson; charset=utf-8", "Cache-Control": "no-store" } });
}

async function readBody(request) {
  const text = await request.text();
  if (text.length > MAX_BODY) throw new Error("too large");
  return text ? JSON.parse(text) : {};
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) return env.ASSETS.fetch(request);

    if (url.pathname === "/api/session" && request.method === "GET") {
      return json(200, snapshot(newSession(url.searchParams.get("scenario"))));
    }
    if (request.method !== "POST") return json(404, { error: "not found" });

    let body;
    try { body = await readBody(request); } catch { return json(400, { error: TEXT.err_bad_request }); }

    if (url.pathname === "/api/reset") return json(200, snapshot(newSession(body.scenario || body.session?.scenario)));

    // 모델을 부르는 요청만 접속자별로 제한한다(누구나 쓰는 공개 링크의 비용 보호)
    if (env.LIMITER) {
      const { success } = await env.LIMITER.limit({ key: request.headers.get("CF-Connecting-IP") || "unknown" });
      if (!success) return json(429, { error: TEXT.err_rate });
    }
    if (!env.NEBIUS_TOKEN_FACTORY_KEY) return json(500, { error: TEXT.err_no_key });

    const sess = loadSession(body.session, body.scenario);
    const sc = scenarioById(sess.scenario);

    if (url.pathname === "/api/turn") {
      if (body.opening) {
        // 시나리오 시작 시 AI가 먼저 양측 입장 범위를 분석한다(참여자 발화가 아니다)
        const o = sc.opening;
        const caseText = o.case_text || buildCaseText(sess, TEXT.opening);
        return ndjson(ctx, (emit) => streamTurn(env, emit, sess, { inputText: o.input_text, caseText, kind: "opening", speaker: "AI" }));
      }
      const preset = sc.presets.find((p) => p.id === body.preset_id) || sc.presets.find((p) => p.input_text === String(body.input_text || "").trim());
      let inputText, speaker, caseText;
      if (preset) {
        inputText = preset.input_text; speaker = preset.speaker;
        caseText = preset.case_text || buildCaseText(sess, fill(TEXT.new_message, { who: speaker, text: inputText }));
      } else {
        inputText = String(body.input_text || "").trim();
        if (!inputText || inputText.length > MAX_INPUT) return json(400, { error: fill(TEXT.err_input_len, { max: MAX_INPUT }) });
        speaker = ["A", "B", "A·B"].includes(body.speaker) ? body.speaker : "A";
        // A·B: 둘 다에게 물은 질문에 두 사람이 함께 답한 것("A: … / B: …")
        caseText = buildCaseText(sess, fill(TEXT.new_message, { who: speaker === "A·B" ? TEXT.both : speaker, text: inputText }));
      }
      return ndjson(ctx, (emit) => streamTurn(env, emit, sess, { inputText, caseText, kind: "turn", speaker }));
    }

    if (url.pathname === "/api/decide") {
      const turn = sess.turns[String(body.turn_id || "")];
      const choice = String(body.choice || "").trim();
      if (!turn || turn.output.status !== "HANDBACK") return json(400, { error: TEXT.err_no_handback });
      if (!choice || choice.length > MAX_INPUT) return json(400, { error: TEXT.err_empty_decision });
      // 사람의 결정도 Security Gate를 다시 지난다. 결정을 상태에 남기고 CSM부터 다시 시작한다.
      const decided = `${turn.output.human_decision_request} → ${choice}`;
      sess.state.agreed_facts = append(sess.state.agreed_facts, `${TEXT.decision_fact}: ${decided}`, 8);
      const blocked = turn.output.context_brief?.blocked_action || "";
      const caseText = buildCaseText(sess, fill(TEXT.resume, { choice, blocked }));
      return ndjson(ctx, (emit) => streamTurn(env, emit, sess, { inputText: choice, caseText, kind: "resume", speaker: "A·B",
        decision: decided, extra: { resumed_from: body.turn_id } }));
    }
    return json(404, { error: "not found" });
  },
};
