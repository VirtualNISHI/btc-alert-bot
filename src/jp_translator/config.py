"""環境変数からデフォルト設定を読む。

全ての値は呼び出し時に上書き可能 (provider モジュール側で kwargs を取る)。
"""
from __future__ import annotations

import logging
import os


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v is not None else default


# API keys
GEMINI_API_KEY = _env("GEMINI_API_KEY")
XAI_API_KEY = _env("XAI_API_KEY") or _env("GROK_API_KEY")  # alias
OPENAI_API_KEY = _env("OPENAI_API_KEY")
DEEPL_API_KEY = _env("DEEPL_API_KEY")

# Models (override via env if a higher-quota or different model is needed)
#
# Gemini default = gemini-2.5-flash:
#   - flash (無印) は thinking モード搭載だが、providers.py 側で
#     thinking_budget=0 を強制しているので reasoning による
#     max_output_tokens 食い潰し問題は無効化済み。
#   - flash は flash-lite より日本語の語彙・敬体・含意表現がワンランク
#     上 (BTC 解説で「観測される」「示唆される」等の慎重な語法を
#     正しく使い分ける)。reasoning 切れば速度差もほぼ無視できる。
#   - flash-lite に戻したい場合は env JP_TRANSLATOR_GEMINI_MODEL で上書き可。
GEMINI_MODEL = _env("JP_TRANSLATOR_GEMINI_MODEL", "gemini-2.5-flash")
# OpenAI/Grok は LLM チェーンの 2 番手/3 番手フォールバック。
# providers.py の reasoning 調整層が上位モデルでも API シグネチャを
# 自動で切り替える (max_completion_tokens / reasoning_effort 等)。
GROK_MODEL = _env("JP_TRANSLATOR_GROK_MODEL", "grok-2-1212")
OPENAI_MODEL = _env("JP_TRANSLATOR_OPENAI_MODEL", "gpt-4o-mini")

# DeepL endpoint: free keys end with ':fx' and need api-free.deepl.com.
DEEPL_HOST = "https://api-free.deepl.com" if DEEPL_API_KEY.endswith(":fx") else "https://api.deepl.com"

# Timeouts
HTTP_TIMEOUT = float(_env("JP_TRANSLATOR_HTTP_TIMEOUT", "20"))

# Logging
LOG_LEVEL = _env("JP_TRANSLATOR_LOG_LEVEL", "INFO").upper()


def get_logger(name: str = "jp_translator") -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        # don't reconfigure root — only set our own level
        log.setLevel(LOG_LEVEL)
    return log
