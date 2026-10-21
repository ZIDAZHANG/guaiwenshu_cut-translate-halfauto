#!/usr/bin/env python3
"""Sakura (llama.cpp) HTTP client: server lifecycle and batch translation."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

from voice_common import hidden_process_kwargs
from task_state import atomic_write_json, now_iso


def http_json(url: str, payload: dict | None = None, timeout: int | None = 10,
              *, audit_dir: Path | None = None, on_stream: Callable[[str], None] | None = None) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    exchange_dir = None
    metadata = {"started_at": now_iso(), "url": url, "method": request.get_method(),
                "request_bytes": len(data) if data is not None else 0,
                "request_sha256": hashlib.sha256(data).hexdigest() if data is not None else None,
                "state": "REQUESTED"}
    if audit_dir is not None:
        exchange_dir = Path(audit_dir) / uuid.uuid4().hex
        exchange_dir.mkdir(parents=True, exist_ok=False)
        if data is not None:
            (exchange_dir / "request.json").write_bytes(data)
        atomic_write_json(exchange_dir / "exchange.json", metadata)

    def save_response(body: bytes, response) -> None:
        metadata.update(status=response.status, content_type=response.headers.get("Content-Type"),
                        response_bytes=len(body), response_sha256=hashlib.sha256(body).hexdigest())
        if exchange_dir is not None:
            (exchange_dir / "response.body").write_bytes(body)

    try:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if "text/event-stream" in response.headers.get("Content-Type", "").lower():
                    result = read_chat_stream(response, exchange_dir, metadata, on_stream)
                    metadata["state"] = "PARSED"
                    return result
                body = response.read()
                save_response(body, response)
        except urllib.error.HTTPError as exc:
            save_response(exc.read(), exc)
            raise
        result = json.loads(body.decode("utf-8"))
        metadata["state"] = "PARSED"
        return result
    except BaseException as exc:
        metadata.update(state="FAILED", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        if exchange_dir is not None:
            metadata["finished_at"] = now_iso()
            atomic_write_json(exchange_dir / "exchange.json", metadata)


def read_chat_stream(response, exchange_dir: Path | None, metadata: dict,
                     on_stream: Callable[[str], None] | None) -> dict:
    """Retain received SSE bytes before parsing, including interrupted output."""
    digest, received, parts, event = hashlib.sha256(), 0, [], []
    done, finish_reason = False, None
    metadata.update(state="STREAMING", status=response.status,
                    content_type=response.headers.get("Content-Type"), response_complete=False)
    body_path = exchange_dir / "response.body" if exchange_dir is not None else None

    def consume():
        nonlocal done, finish_reason
        if not event:
            return
        value = "\n".join(event)
        event.clear()
        if value == "[DONE]":
            done = True
            return
        if done:
            raise ValueError("Chat stream included data after its completion marker")
        packet = json.loads(value)
        for choice in packet.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            content = choice.get("delta", {}).get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ValueError("Chat stream content is not text")
                parts.append(content)
                if on_stream is not None and content:
                    on_stream(content)
            if choice.get("finish_reason") is not None:
                finish_reason = choice["finish_reason"]

    with body_path.open("wb") if body_path is not None else nullcontext() as body_file:
        for line in response:
            digest.update(line)
            received += len(line)
            if body_file is not None:
                body_file.write(line)
                body_file.flush()
            metadata.update(response_bytes=received, response_sha256=digest.hexdigest())
            if exchange_dir is not None:
                atomic_write_json(exchange_dir / "exchange.json", metadata)
            text = line.decode("utf-8").rstrip("\r\n")
            if not text:
                consume()
            elif text.startswith("data:"):
                event.append(text[5:].removeprefix(" "))
        consume()
    if not done or finish_reason is None:
        raise ValueError("Chat stream ended before a complete final response")
    metadata["response_complete"] = True
    return {"choices": [{"message": {"content": "".join(parts)}, "finish_reason": finish_reason}]}


def endpoint_ready(endpoint: str) -> bool:
    base = endpoint.rstrip("/")
    for suffix in ("/health", "/v1/models"):
        try:
            http_json(base + suffix, timeout=3)
            return True
        except Exception:
            continue
    return False


def wait_for_endpoint(endpoint: str, timeout: int | None, process: subprocess.Popen[str] | None) -> None:
    deadline = time.time() + timeout if timeout is not None else None
    while deadline is None or time.time() < deadline:
        if endpoint_ready(endpoint):
            return
        if process is not None and process.poll() is not None:
            raise SystemExit(f"Sakura model server exited early with code {process.returncode}")
        time.sleep(2)
    raise SystemExit(f"Sakura model server was not ready within {timeout} seconds: {endpoint}")


def require_server_model(endpoint: str, expected: str) -> None:
    try:
        response = http_json(endpoint.rstrip("/") + "/v1/models", timeout=5)
        ids = [str(item.get("id", "")) for item in response.get("data", []) if isinstance(item, dict)]
    except Exception as exc:
        raise SystemExit(f"Cannot verify the model at {endpoint}; existing server was left untouched: {exc}") from exc
    basename = lambda value: value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if not expected or not any(basename(item) == basename(expected) for item in ids):
        raise SystemExit(f"Wrong model at {endpoint}: expected {expected}, found {ids}; existing server was left untouched")


def start_server(settings: dict, endpoint: str, log_path: Path) -> subprocess.Popen[str] | None:
    expected_model = str(settings.get("server_model_alias") or settings.get("model") or "Sakura-Galtransl-7B-v3.7.gguf")
    if endpoint_ready(endpoint):
        require_server_model(endpoint, expected_model)
        print(f"[voice] using existing Sakura server: {endpoint}")
        return None
    executable_value = str(settings.get("server_executable", "")).strip()
    if not executable_value:
        raise SystemExit("Sakura server is not configured. Select the VoiceTransl installation folder in the desktop app.")
    executable = Path(executable_value)
    model = Path(str(settings.get("model", executable.parent / "Sakura-Galtransl-7B-v3.7.gguf")))
    if not executable.exists() or not model.exists():
        raise SystemExit(f"Sakura server or model does not exist: {executable}, {model}")
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Automatic Sakura server startup requires a loopback endpoint")
    port = parsed.port or 8989
    gpu_layers = int(settings.get("gpu_layers", 100))
    context_size = int(settings.get("context_size", 16384))
    if context_size < 0:
        raise SystemExit("Sakura context size must be nonnegative (0 uses the model default)")
    command = [str(executable), "-m", str(model), "-ngl", str(gpu_layers), "--port", str(port),
               "--host", "127.0.0.1", "--ctx-size", str(context_size), "--parallel", "1"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8", errors="replace")
    process: subprocess.Popen[str] | None = None
    try:
        print(f"[voice] starting Sakura server on port {port}; context={context_size}; parallel=1", flush=True)
        process = subprocess.Popen(
            command,
            cwd=executable.parent,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            **hidden_process_kwargs(),
        )
        setattr(process, "_roast_log", log)
        wait_for_endpoint(endpoint, settings.get("startup_timeout_seconds"), process)
        require_server_model(endpoint, expected_model)
        return process
    except BaseException:
        if process is None:
            log.close()
        else:
            stop_server(process)
        raise


def stop_server(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    try:
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        except Exception:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    finally:
        log = getattr(process, "_roast_log", None)
        if log is not None and not log.closed:
            log.close()


def extract_json(text: str) -> object:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = cleaned.find(start_char)
        end = cleaned.rfind(end_char)
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise ValueError("model response did not contain valid JSON")


def parse_translations(content: str, expected_ids: list[int]) -> dict[int, str]:
    parsed = extract_json(content)
    if isinstance(parsed, dict):
        parsed = parsed.get("translations", parsed.get("items", parsed))
    result: dict[int, str] = {}
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict) and "id" in item:
                result[int(item["id"])] = str(item.get("text", item.get("translation", ""))).strip()
    elif isinstance(parsed, dict):
        for key, value in parsed.items():
            if str(key).isdigit():
                result[int(key)] = str(value).strip()
    missing = [item_id for item_id in expected_ids if not result.get(item_id)]
    if missing:
        raise ValueError(f"model response missed subtitle ids: {missing}")
    return result


def _parse_partial(content: str, expected_ids: list[int]) -> dict[int, str]:
    """Parse whatever ids the model did return; used to shrink retry batches."""
    try:
        allowed = set(expected_ids)
        return {
            item_id: text
            for item_id, text in parse_translations(content, []).items()
            if item_id in allowed and text.strip()
        }
    except Exception:
        return {}


def translation_response_schema(expected_ids: list[int]) -> dict:
    return {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "minItems": len(expected_ids),
                "maxItems": len(expected_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "enum": expected_ids},
                        "text": {"type": "string", "minLength": 1},
                    },
                    "required": ["id", "text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["translations"],
        "additionalProperties": False,
    }


def translate_json_batch(endpoint: str, model_name: str, system_prompt: str, batch: list[dict], retries: int = 2,
                    *, audit_dir: Path | None = None) -> dict[int, str]:
    expected = [int(item["id"]) for item in batch]
    remaining = batch

    def build_payload(items: list[dict]) -> dict:
        ids = [int(item["id"]) for item in items]
        return {
            "model": model_name,
            "temperature": 0.15,
            "top_p": 0.9,
            "response_format": {
                "type": "json_object",
                "schema": translation_response_schema(ids),
            },
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": "逐条翻译下面的日文字幕。只返回 JSON：{\"translations\":[{\"id\":1,\"text\":\"译文\"}]}。不得合并、拆分或遗漏。\n" + json.dumps(items, ensure_ascii=False),
                },
            ],
        }

    payload = build_payload(remaining)
    translated: dict[int, str] = {}
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if not remaining:
            break
        content: object | None = None
        try:
            response = http_json(endpoint.rstrip("/") + "/v1/chat/completions", payload, timeout=None,
                                 **({"audit_dir": audit_dir} if audit_dir is not None else {}))
            choice = response["choices"][0]
            content = choice["message"]["content"]
            if choice.get("finish_reason") == "length":
                raise ValueError("model response was truncated by its generation limit")
            content_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            remaining_ids = [int(item["id"]) for item in remaining]
            parsed = _parse_partial(content_text, remaining_ids)
            # Sakura occasionally omits one id; retry only the missing items.
            missing = [item for item in remaining if int(item["id"]) not in parsed]
            if parsed:
                translated.update(parsed)
            if not missing:
                if not parsed:
                    raise ValueError("model response contained no subtitle ids")
                return translated
            remaining = missing
            payload = build_payload(remaining)
            last_error = ValueError(f"model response missed subtitle ids: {[int(m['id']) for m in missing]}")
            if attempt < retries:
                time.sleep(2)
        except Exception as exc:
            if content is None:
                last_error = exc
            else:
                content_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                response_hash = hashlib.sha256(content_text.encode("utf-8", errors="replace")).hexdigest()
                last_error = ValueError(
                    f"{exc}; response_chars={len(content_text)}; response_sha256={response_hash}"
                )
            if isinstance(exc, urllib.error.HTTPError) and exc.code in {400, 422}:
                payload.pop("response_format", None)
            if attempt < retries:
                time.sleep(2)
    if last_error is not None:
        raise RuntimeError(f"Sakura translation batch failed: {last_error}")
    return translated


def plain_translation_payload(model_name: str, system_prompt: str, items: list[dict],
                              reference: list[dict] | None = None, *,
                              temperature: float = 0.3, top_p: float = 0.8,
                              glossary_text: str = "") -> dict:
    reference = items if reference is None else reference
    source_body = "".join(str(item["text"]) for item in items)
    def context_text(value: object) -> str:
        if isinstance(value, list):
            rows = []
            for parent in value:
                if isinstance(parent, dict) and "text" in parent:
                    # The entire parent is already present in this request's
                    # body. Do not instruct the fine-tuned model with a second
                    # copy of the same Japanese text in the system message.
                    if str(parent["text"]) and str(parent["text"]) in source_body:
                        continue
                    label = "画面 OCR 参考" if parent.get("kind") == "visual_ocr" else "语音父句"
                    rows.append(f"{label}：{parent['text']}")
                else:
                    rows.append(str(parent))
            return "\n".join(rows)
        return str(value)
    contexts = []
    for item in reference:
        if item.get("context"):
            text = context_text(item["context"])
            if text:
                contexts.append(f"字幕{item['id']}：{text}")
    system_prompt = ("你是一个视觉小说翻译模型，可以通顺地使用给定的术语表以指定的风格将日文翻译成简体中文，并联系上下文正确使用人称代词，注意不要混淆使役态和被动态的主语和宾语，不要擅自添加原文中没有的特殊符号，也不要擅自增加或减少换行。\n" + system_prompt)
    sections = []
    if contexts:
        sections.append("参考上下文（只帮助理解，不属于待翻译正文，不要输出）：\n" + "\n".join(contexts))
        if any(isinstance(parent, dict) and parent.get("kind") == "visual_ocr"
               for item in reference for parent in (item["context"] if isinstance(item.get("context"), list) else [])):
            sections.append("语音和画面 OCR 都可能识别错误。结合两者校验人名、词义和语气；只翻译当前待译片段，不补入画面上的后文。证据不能确定时保留原词并标记 [待确认]。")
    if reference != items:
        sections.append("同批原文参考（只帮助理解，不要输出）：\n" +
                        "\n".join(f"字幕{item['id']}：{item['text']}" for item in reference))
    if sections:
        system_prompt += "\n\n" + "\n\n".join(sections)
    system_prompt += "\n调用方负责保存编号、时间码和片段身份。只输出本轮用户正文的译文，不输出上述参考文字或编号。"
    body = "参考以下术语表（可为空，格式为 src->dst #备注）：\n" + glossary_text + "\n根据以上术语表的对应关系和备注，结合历史剧情和上下文，将下面的文本从日文翻译成简体中文：\n" + "\n".join(str(item["text"]) for item in items)
    return {"model": model_name, "temperature": temperature, "top_p": top_p, "stream": True,
            "messages": [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": body}]}


def translate_batch(endpoint: str, model_name: str, system_prompt: str, batch: list[dict], retries: int = 2,
                    *, audit_dir: Path | None = None,
                    on_mapped: Callable[[dict[int, str]], None] | None = None,
                    on_stream: Callable[[str], None] | None = None,
                    temperature: float = 0.3, top_p: float = 0.8,
                    glossary_text: str = "") -> dict[int, str]:
    """Use Sakura's text body, retaining stable IDs outside model generation.

    A multiline cue is one translation unit. Ambiguous batch line counts cause
    individual requests with the complete original batch as reference; no
    missing line is guessed or applied to a following subtitle.
    """
    units: list[list[dict]] = []
    current: list[dict] = []
    for item in batch:
        if "\n" in str(item["text"]) or "\r" in str(item["text"]):
            if current:
                units.append(current)
                current = []
            units.append([item])
        else:
            current.append(item)
    if current:
        units.append(current)
    translated: dict[int, str] = {}
    while units:
        items = units.pop(0)
        payload = plain_translation_payload(model_name, system_prompt, items, batch,
                                            temperature=temperature, top_p=top_p,
                                            glossary_text=glossary_text)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            request_dir = Path(audit_dir) / uuid.uuid4().hex if audit_dir is not None else None
            record = {"started_at": now_iso(), "items": items, "reference": batch,
                      "attempt": attempt + 1, "state": "REQUESTED"}
            if request_dir is not None:
                atomic_write_json(request_dir / "translation.json", record)
            try:
                response = http_json(endpoint.rstrip("/") + "/v1/chat/completions", payload, timeout=None,
                                     **({"audit_dir": request_dir} if request_dir is not None else {}),
                                     **({"on_stream": on_stream} if on_stream is not None else {}))
                choice = response["choices"][0]
                record["finish_reason"] = choice.get("finish_reason")
                if choice.get("finish_reason") == "length":
                    raise ValueError("model response was truncated by its generation limit")
                content = choice["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("model response did not contain translation text")
                text = content.strip()
                lines = text.splitlines()
                if len(items) == 1:
                    parsed = {int(items[0]["id"]): text}
                    record["mapping"] = "single_cue_complete_body"
                elif len(lines) == len(items) and all(line.strip() for line in lines):
                    parsed = {int(item["id"]): line.strip() for item, line in zip(items, lines)}
                    record["mapping"] = "input_line_order_not_semantic_verification"
                else:
                    record.update(state="AMBIGUOUS_LINES", line_count=len(lines), expected_lines=len(items))
                    units = [[item] for item in items] + units
                    break
                translated.update(parsed)
                record.update(state="MAPPED", translations=parsed)
                break
            except KeyboardInterrupt:
                record.update(state="INTERRUPTED", error_type="KeyboardInterrupt")
                raise
            except Exception as exc:
                last_error = exc
                record.update(state="FAILED", error_type=type(exc).__name__, error=str(exc))
                if attempt < retries:
                    time.sleep(2)
            finally:
                if request_dir is not None:
                    record["finished_at"] = now_iso()
                    atomic_write_json(request_dir / "translation.json", record)
        else:
            raise RuntimeError(f"Sakura translation batch failed: {last_error}")
        if record["state"] == "MAPPED" and on_mapped is not None:
            on_mapped(parsed)
    return translated
