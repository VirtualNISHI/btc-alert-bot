"""各 LLM/翻訳プロバイダ呼び出し。

例外は外に投げず、失敗時は ``None`` を返す。これにより上位のフォールバック
ロジック (`core.translate` 等) は単純な ``if x: return x`` で済む。
"""
from __future__ import annotations

import json
from typing import Any, Type

import httpx

from . import config

log = config.get_logger(__name__)


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def gemini_generate_text(
    system: str,
    user: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> str | None:
    """Gemini に system + user を投げて生成テキストを返す。失敗時 None。"""
    api_key = api_key or config.GEMINI_API_KEY
    model = model or config.GEMINI_MODEL
    if not api_key:
        log.debug("gemini: no api key, skip")
        return None
    try:
        from google import genai  # type: ignore
        from google.genai import types as genai_types  # type: ignore
    except ImportError:
        log.warning("gemini: google-genai SDK not installed, skip")
        return None
    try:
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=model,
            contents=user,
            config=genai_types.GenerateContentConfig(
                system_instruction=system or None,
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            log.warning("gemini: empty response")
            return None
        log.debug("gemini ok (%d chars)", len(text))
        return text
    except Exception as exc:  # noqa: BLE001 - intentional: any failure -> fallback
        log.warning("gemini failed: %s", exc)
        return None


def gemini_generate_structured(
    system: str,
    user: str,
    schema: Type[Any],
    *,
    api_key: str | None = None,
    model: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> Any | None:
    """Gemini で Pydantic schema に基づく JSON 出力を取得。失敗時 None。"""
    api_key = api_key or config.GEMINI_API_KEY
    model = model or config.GEMINI_MODEL
    if not api_key:
        log.debug("gemini structured: no api key, skip")
        return None
    try:
        from google import genai  # type: ignore
        from google.genai import types as genai_types  # type: ignore
    except ImportError:
        log.warning("gemini structured: google-genai not installed, skip")
        return None
    try:
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=model,
            contents=user,
            config=genai_types.GenerateContentConfig(
                system_instruction=system or None,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=temperature,
                max_output_tokens=max_tokens,
            ),
        )
        parsed = getattr(resp, "parsed", None)
        if parsed is not None:
            if isinstance(parsed, schema):
                return parsed
            if isinstance(parsed, dict):
                try:
                    return schema(**parsed)
                except Exception as exc:  # noqa: BLE001
                    log.warning("gemini structured: schema(**dict) failed: %s", exc)
        # Fall back to raw text parse
        text = getattr(resp, "text", None)
        if text:
            try:
                data = json.loads(text)
                return schema(**data)
            except Exception as exc:  # noqa: BLE001
                log.warning("gemini structured: json parse failed: %s", exc)
        log.warning("gemini structured: no parseable response")
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("gemini structured failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Grok (xAI) — OpenAI-compatible REST API
# ---------------------------------------------------------------------------

_GROK_URL = "https://api.x.ai/v1/chat/completions"


def grok_generate_text(
    system: str,
    user: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> str | None:
    api_key = api_key or config.XAI_API_KEY
    model = model or config.GROK_MODEL
    if not api_key:
        log.debug("grok: no api key, skip")
        return None
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        with httpx.Client(timeout=config.HTTP_TIMEOUT) as client:
            r = client.post(
                _GROK_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if not text:
            log.warning("grok: empty response")
            return None
        log.debug("grok ok (%d chars)", len(text))
        return text
    except Exception as exc:  # noqa: BLE001
        log.warning("grok failed: %s", exc)
        return None


def grok_generate_structured(
    system: str,
    user: str,
    schema: Type[Any],
    *,
    api_key: str | None = None,
    model: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> Any | None:
    """Grok の JSON モード経由で構造化出力を試みる。失敗時 None。

    Grok は OpenAI 互換の `response_format={"type":"json_object"}` をサポート
    するが、schema 強制機能は限定的なので、出力 JSON を Pydantic で再検証する。
    """
    api_key = api_key or config.XAI_API_KEY
    model = model or config.GROK_MODEL
    if not api_key:
        return None
    # 既存の system に「以下のJSONスキーマに従って出力」を追記。
    try:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    except Exception:
        schema_json = "(unavailable)"
    sys_aug = (
        (system or "") +
        "\n\n出力は次の JSON Schema に正確に従う有効な JSON のみで返してください:\n"
        + schema_json
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_aug},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=config.HTTP_TIMEOUT) as client:
            r = client.post(
                _GROK_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if not text:
            log.warning("grok structured: empty response")
            return None
        try:
            return schema(**json.loads(text))
        except Exception as exc:  # noqa: BLE001
            log.warning("grok structured: schema parse failed: %s; raw=%s", exc, text[:200])
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("grok structured failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# OpenAI — Chat Completions API
# ---------------------------------------------------------------------------

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def openai_generate_text(
    system: str,
    user: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> str | None:
    """OpenAI Chat Completions で system+user からテキスト生成。失敗時 None。

    Grok と同じ OpenAI 互換 schema を使うため処理はほぼ共通だが、
    ``response_format`` の表現力が違うので structured 側は別実装。
    """
    api_key = api_key or config.OPENAI_API_KEY
    model = model or config.OPENAI_MODEL
    if not api_key:
        log.debug("openai: no api key, skip")
        return None
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        with httpx.Client(timeout=config.HTTP_TIMEOUT) as client:
            r = client.post(
                _OPENAI_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if not text:
            log.warning("openai: empty response")
            return None
        log.debug("openai ok (%d chars)", len(text))
        return text
    except Exception as exc:  # noqa: BLE001
        log.warning("openai failed: %s", exc)
        return None


def openai_generate_structured(
    system: str,
    user: str,
    schema: Type[Any],
    *,
    api_key: str | None = None,
    model: str | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> Any | None:
    """OpenAI で JSON Schema 強制出力を試みる。失敗時 None。

    ``response_format={"type":"json_schema", ...}`` を使うと OpenAI 側で
    schema 違反を弾いてくれる。失敗時は念のため Pydantic で再検証。
    """
    api_key = api_key or config.OPENAI_API_KEY
    model = model or config.OPENAI_MODEL
    if not api_key:
        return None
    try:
        schema_json = schema.model_json_schema()
    except Exception:
        schema_json = None
    if schema_json is not None:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": schema_json,
                "strict": False,  # strict=True は schema 制約が厳しいので緩めに
            },
        }
    else:
        response_format = {"type": "json_object"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system or ""},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": response_format,
    }
    try:
        with httpx.Client(timeout=config.HTTP_TIMEOUT) as client:
            r = client.post(
                _OPENAI_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        r.raise_for_status()
        data = r.json()
        text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if not text:
            log.warning("openai structured: empty response")
            return None
        try:
            return schema(**json.loads(text))
        except Exception as exc:  # noqa: BLE001
            log.warning("openai structured: schema parse failed: %s; raw=%s", exc, text[:200])
            return None
    except Exception as exc:  # noqa: BLE001
        log.warning("openai structured failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# DeepL — 直訳のみ。生成には使えない。
# ---------------------------------------------------------------------------

def deepl_translate(
    text: str,
    *,
    api_key: str | None = None,
    source_lang: str | None = "EN",
    target_lang: str = "JA",
) -> str | None:
    api_key = api_key or config.DEEPL_API_KEY
    if not api_key or not text:
        log.debug("deepl: no api key or empty text, skip")
        return None
    host = "https://api-free.deepl.com" if api_key.endswith(":fx") else "https://api.deepl.com"
    data: dict[str, str] = {"text": text, "target_lang": target_lang}
    if source_lang:
        data["source_lang"] = source_lang
    try:
        with httpx.Client(timeout=config.HTTP_TIMEOUT) as client:
            r = client.post(
                f"{host}/v2/translate",
                headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
                data=data,
            )
        r.raise_for_status()
        translations = r.json().get("translations", [])
        if not translations:
            log.warning("deepl: empty translations")
            return None
        out = (translations[0].get("text") or "").strip()
        log.debug("deepl ok (%d chars)", len(out))
        return out or None
    except Exception as exc:  # noqa: BLE001
        log.warning("deepl failed: %s", exc)
        return None


def deepl_translate_batch(
    texts: list[str],
    *,
    api_key: str | None = None,
    source_lang: str | None = "EN",
    target_lang: str = "JA",
) -> list[str] | None:
    """DeepL バッチ翻訳。失敗時 None。"""
    api_key = api_key or config.DEEPL_API_KEY
    texts = [t for t in texts if t]
    if not api_key or not texts:
        return None
    host = "https://api-free.deepl.com" if api_key.endswith(":fx") else "https://api.deepl.com"
    fields: list[tuple[str, str]] = [("text", t) for t in texts]
    fields.append(("target_lang", target_lang))
    if source_lang:
        fields.append(("source_lang", source_lang))
    try:
        with httpx.Client(timeout=config.HTTP_TIMEOUT) as client:
            r = client.post(
                f"{host}/v2/translate",
                headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
                data=fields,
            )
        r.raise_for_status()
        translations = r.json().get("translations", [])
        if len(translations) != len(texts):
            log.warning(
                "deepl batch: count mismatch in=%d out=%d", len(texts), len(translations)
            )
            return None
        out = [(t.get("text") or "").strip() for t in translations]
        log.debug("deepl batch ok (%d items)", len(out))
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("deepl batch failed: %s", exc)
        return None
