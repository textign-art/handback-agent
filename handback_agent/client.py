"""Token Factory client: json_object 호출 + 서버 검증 + 1회 재시도.

- 키는 .env.local에서 읽기만 하고 출력·기록하지 않는다.
- 응답의 reasoning_content는 버린다. content만 검증에 쓴다.
- 호출 로그에는 시간·토큰·비용·재시도만 남기고 원문은 남기지 않는다.
"""
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MODEL = "nvidia/Nemotron-3-Ultra-550b-a55b"
ENDPOINT = "https://api.tokenfactory.nebius.com/v1/chat/completions"
KEY_NAME = "NEBIUS_TOKEN_FACTORY_KEY"

# eval 스크립트와 같은 추정식 (prompt $1/M, completion $3/M)
PROMPT_USD_PER_TOKEN = 1 / 1_000_000
COMPLETION_USD_PER_TOKEN = 3 / 1_000_000


def load_key(path: Path) -> str:
    """환경 변수 NEBIUS_TOKEN_FACTORY_KEY가 있으면 그것을, 없으면 .env.local 파일에서 읽는다."""
    env = os.environ.get(KEY_NAME, "").strip()
    if env:
        return env
    if not Path(path).exists():
        raise RuntimeError(f"{KEY_NAME} not found (set the environment variable or create {path})")
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == KEY_NAME:
            return value.strip().strip('"').strip("'")
    raise RuntimeError(f"{KEY_NAME} not found")


@dataclass
class CallRecord:
    stage: str
    attempt: int
    elapsed_seconds: float
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    valid: bool
    validation_error: str | None = None

    @property
    def estimated_cost_usd(self) -> float:
        return round(
            self.prompt_tokens * PROMPT_USD_PER_TOKEN
            + self.completion_tokens * COMPLETION_USD_PER_TOKEN,
            6,
        )


@dataclass
class CallLog:
    records: list[CallRecord] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "calls": len(self.records),
            "retries": sum(r.attempt > 1 for r in self.records),
            "seconds": round(sum(r.elapsed_seconds for r in self.records), 3),
            "prompt_tokens": sum(r.prompt_tokens for r in self.records),
            "completion_tokens": sum(r.completion_tokens for r in self.records),
            "estimated_cost_usd": round(sum(r.estimated_cost_usd for r in self.records), 6),
        }


class OutputInvalid(Exception):
    """첫 시도와 1회 재시도 모두 서버 검증에 실패했다."""


# (messages, chat_template_kwargs, max_tokens) -> (raw API result dict, elapsed seconds)
Transport = Callable[[list[dict], dict, int], tuple[dict, float]]


def http_transport(key: str, timeout: int = 180) -> Transport:
    def send(messages: list[dict], template_kwargs: dict, max_tokens: int) -> tuple[dict, float]:
        payload = {
            "model": MODEL,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            # raw HTTP에서는 extra_body가 아니라 최상위 chat_template_kwargs
            "chat_template_kwargs": template_kwargs,
        }
        request = urllib.request.Request(
            ENDPOINT,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {error.code}: {body[:500]}") from error
        return result, time.perf_counter() - started

    return send


def call_validated(
    transport: Transport,
    *,
    stage: str,
    messages: list[dict],
    template_kwargs: dict,
    max_tokens: int,
    validate: Callable[[str], dict],
    repair_message: str,
    log: CallLog,
) -> dict:
    messages = list(messages)
    last_error = None
    for attempt in (1, 2):
        result, elapsed = transport(messages, template_kwargs, max_tokens)
        content = result["choices"][0]["message"].get("content") or ""
        usage = result.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        record = CallRecord(
            stage=stage,
            attempt=attempt,
            elapsed_seconds=round(elapsed, 3),
            prompt_tokens=usage.get("prompt_tokens") or 0,
            completion_tokens=usage.get("completion_tokens") or 0,
            reasoning_tokens=details.get("reasoning_tokens") or 0,
            valid=False,
        )
        try:
            parsed = validate(content)
        except Exception as error:
            record.validation_error = str(error)
            log.records.append(record)
            last_error = error
            messages += [
                {"role": "assistant", "content": content},
                {"role": "user", "content": repair_message},
            ]
            continue
        record.valid = True
        log.records.append(record)
        return parsed
    raise OutputInvalid(f"{stage}: {last_error}")


def cached_transport(inner: Transport, path: Path) -> Transport:
    """시연용: 같은 요청이면 저장해 둔 응답을 재사용한다. 키·원문 입력은 저장하지 않고 해시만 쓴다."""
    import hashlib

    path = Path(path)
    store = json.loads(path.read_text("utf-8")) if path.exists() else {}

    def send(messages: list[dict], template_kwargs: dict, max_tokens: int) -> tuple[dict, float]:
        digest = hashlib.sha256(
            json.dumps([messages, template_kwargs, max_tokens, MODEL], ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        if digest in store:
            return store[digest], 0.0
        result, elapsed = inner(messages, template_kwargs, max_tokens)
        message = result["choices"][0]["message"]
        store[digest] = {
            "choices": [{"message": {"content": message.get("content")}}],
            "usage": result.get("usage", {}),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store, ensure_ascii=False), "utf-8")
        return result, elapsed

    return send
