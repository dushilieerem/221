"""共享的 OpenAI 兼容 LLM Provider 基础设施。

本模块只负责供应商配置、HTTP 调用、结构化输出降级和统一异常语义；
因子、策略与回测解读的 Prompt、响应模型和业务校验仍由各自应用服务负责。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderSettings:
    """一次供应商调用所需的服务端配置。"""

    base_url: str
    api_key: str
    model: str


class ProviderUnavailable(Exception):
    """供应商不可用或返回内容不符合 Provider 协议。"""


def generation_provider_settings(config: Any) -> ProviderSettings:
    """因子生成与帖子摘要共享现有服务端配置，不向调用方暴露凭据。"""
    key = os.environ.get("AI_FACTOR_API_KEY") or os.environ.get("ARK_API_KEY") or config.ai_api_key
    if not key:
        raise ProviderUnavailable("模型服务尚未配置")
    return ProviderSettings(
        base_url=os.environ.get("AI_FACTOR_BASE_URL", config.ai_base_url).rstrip("/"),
        api_key=key,
        model=os.environ.get("AI_FACTOR_MODEL_ALIAS", config.ai_model),
    )


def complete(
    settings: ProviderSettings,
    messages: list[dict[str, str]],
    *,
    response_format: dict[str, Any] | None = None,
    temperature: float = 0.1,
) -> str:
    return complete_result(settings, messages, response_format=response_format, temperature=temperature)["content"]


def complete_result(
    settings: ProviderSettings,
    messages: list[dict[str, str]],
    *,
    response_format: dict[str, Any] | None = None,
    temperature: float = 0.1,
) -> dict[str, Any]:
    """调用 Chat Completions，并返回消息内容。

    当供应商不支持 ``json_schema`` 时，自动重试一次无结构化输出格式的请求；
    上层服务仍需对返回内容执行自己的 JSON、AST 或领域校验。
    """

    payload: dict[str, Any] = {
        "model": settings.model,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format is not None:
        payload["response_format"] = response_format

    try:
        body = _send_completion(settings, payload)
    except urllib.error.HTTPError as exc:
        if response_format is None or not response_format_is_unsupported(exc):
            raise ProviderUnavailable from exc
        payload.pop("response_format", None)
        try:
            body = _send_completion(settings, payload)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            KeyError,
            IndexError,
            TypeError,
            json.JSONDecodeError,
        ) as retry_exc:
            raise ProviderUnavailable from retry_exc
    except (
        urllib.error.URLError,
        KeyError,
        IndexError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        raise ProviderUnavailable from exc

    try:
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ProviderUnavailable
        usage = body.get("usage") or {}
        return {
            "content": content,
            "model": body.get("model") or settings.model,
            "tokens_in": max(0, int(usage.get("prompt_tokens") or 0)),
            "tokens_out": max(0, int(usage.get("completion_tokens") or 0)),
        }
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ProviderUnavailable from exc


def _send_completion(settings: ProviderSettings, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{settings.base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def response_format_is_unsupported(error: urllib.error.HTTPError) -> bool:
    """判断供应商是否明确拒绝了 ``response_format``。"""

    if error.code != 400:
        return False
    try:
        message = json.loads(error.read().decode("utf-8")).get("error", {}).get("message", "")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return "response_format.type" in message and "not supported" in message


__all__ = ["ProviderSettings", "ProviderUnavailable", "generation_provider_settings", "complete", "complete_result"]
