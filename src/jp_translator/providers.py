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
                # gemini-2.5-flash 系は thinking モード搭載で、
                # 既定だと max_output_tokens の大半が reasoning に消費され
                # 本文が途中で切れる (24字で打ち切られた事例あり)。
                # 翻訳/要約は reasoning 不要なので明示的に OFF。
                # この設定は thinking 非搭載モデル (flash-lite / 2.0系) では無視される。
                thinking_config=_thinking_off(genai_types),
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


def _thinking_off(genai_types) -> Any | None:
    """ThinkingConfig(thinking_budget=0) を返す。SDK 旧版で型が無ければ None。

    `google-genai` の比較的新しいバージョンにしか ``ThinkingConfig`` は無い。
    SDK が古い環境では AttributeError が出るので None を返して通常動作に戻す
    (その場合は thinking が効いたまま — 古い SDK は基本 2.5-flash 自体使えない)。
    """
    try:
        return genai_types.ThinkingConfig(thinking_budget=0)
    except AttributeError:
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
                # thinking で max_output_tokens を食い潰されないように OFF。
                # 構造化出力でも同じ罠が成立する (JSON 出力前に reasoning 終了せず切れる)。
                thinking_config=_thinking_off(genai_types),
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
# Reasoning-model adjusters
# ---------------------------------------------------------------------------
#
# OpenAI / Grok の上位モデルは「reasoning tokens」(thinking) を消費し、素朴に
# max_tokens を送ると本文が空になる事故が起きる (Gemini 2.5 flash と同じ症状)。
# 加えて API シグネチャ自体が異なる:
#
#   OpenAI o系 / gpt-5:
#     - `max_tokens` 不可 → `max_completion_tokens` 必須
#     - `temperature` 固定 (1) → 送ると 400
#     - `reasoning_effort` で reasoning 量を絞れる (gpt-5 は "minimal" も可)
#
#   xAI Grok 3 / 4:
#     - `reasoning_effort=low` で thinking を最小化 (grok-3 系)
#     - grok-4 は reasoning 強制 ON 不可 → max_tokens を増やして対応するしかない
#
# 下の関数群はモデル名を見て payload を整形する。デフォルトモデル
# (gpt-4o-mini / grok-2-1212) はどちらも reasoning 無しなので noop。


def _openai_is_reasoning(model: str) -> bool:
    """o1/o3/o4/o5/gpt-5 系なら True。これらは API シグネチャが違う。"""
    m = (model or "").lower()
    if m.startswith(("o1", "o3", "o4", "o5")):
        return True
    if m.startswith("gpt-5"):
        return True
    return False


def _openai_apply_reasoning_adjustments(payload: dict, *, model: str) -> dict:
    """OpenAI reasoning モデル用に payload を変換。non-reasoning モデルは無加工。

    - ``max_tokens`` → ``max_completion_tokens`` にリネーム
    - ``temperature`` を削除 (o系は 1 固定、指定不可)
    - ``reasoning_effort`` を追記 (gpt-5: "minimal" / o系: "low" — トークン節約)
    """
    if not _openai_is_reasoning(model):
        return payload
    if "max_tokens" in payload:
        payload["max_completion_tokens"] = payload.pop("max_tokens")
    payload.pop("temperature", None)
    if model.lower().startswith("gpt-5"):
        # gpt-5 は "minimal" でほぼ thinking なし (= 翻訳/要約に最適)
        payload.setdefault("reasoning_effort", "minimal")
    else:
        # o-series は "low" が最小 ("minimal" 非対応)
        payload.setdefault("reasoning_effort", "low")
    return payload


def _grok_is_reasoning(model: str) -> bool:
    """grok-3 系以降は reasoning 持ち。grok-2 系は無し。"""
    m = (model or "").lower()
    # grok-3, grok-3-mini, grok-3-fast, grok-4, grok-4-fast …
    if m.startswith(("grok-3", "grok-4", "grok-5")):
        return True
    return False


def _grok_apply_reasoning_adjustments(payload: dict, *, model: str) -> dict:
    """Grok reasoning モデル用に payload を補正。non-reasoning モデルは無加工。

    - grok-3 系: ``reasoning_effort="low"`` で thinking を最小化
    - grok-4 系: thinking 強制 ON のため reasoning_effort は効かない。max_tokens
      を 1.5x に増やしてバッファを確保 (それでも超えれば本文切れる)
    """
    if not _grok_is_reasoning(model):
        return payload
    m = model.lower()
    if m.startswith(("grok-3", "grok-5")):
        payload.setdefault("reasoning_effort", "low")
    elif m.startswith("grok-4"):
        # grok-4 は reasoning 制御不能 → トークン枠を増やすしかない
        if "max_tokens" in payload:
            payload["max_tokens"] = int(payload["max_tokens"] * 1.5) + 512
    return payload


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
    payload = _grok_apply_reasoning_adjustments(payload, model=model)
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
    payload = _grok_apply_reasoning_adjustments(payload, model=model)
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
    payload = _openai_apply_reasoning_adjustments(payload, model=model)
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
    payload = _openai_apply_reasoning_adjustments(payload, model=model)
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
