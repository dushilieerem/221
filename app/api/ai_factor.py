"""受控的自然语言因子生成服务。

本模块只生成并静态校验 ``FactorLibrary`` 的单个方法；不会执行模型输出。
共享供应商地址、密钥和模型可从 config.yaml 或环境变量读取；调用使用 OpenAI 兼容协议。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import time
import urllib.request
from threading import Lock
from uuid import uuid4

from fastapi import HTTPException

from app.api import llm_provider
from app.config import get_runtime_settings
from app.schemas.backtest import (
    FactorGenerationMeta,
    FactorGenerationRequest,
    FactorGenerationResponse,
    FactorGenerationValidation,
    GeneratedFactor,
)


# 保留历史模块路径上的名称，避免现有测试和内部调用方被提取动作打断。
ProviderSettings = llm_provider.ProviderSettings
ProviderUnavailable = llm_provider.ProviderUnavailable
_send_completion = llm_provider._send_completion
_response_format_is_unsupported = llm_provider.response_format_is_unsupported


CATALOG_VERSION = "2026-07-01"
PROMPT_VERSION = "v2"
ALLOWED_FIELDS = frozenset({"open", "high", "low", "close", "volume", "amount"})
ALLOWED_METHODS = frozenset({
    "abs", "clip", "diff", "fillna", "mean", "median", "min", "max", "pct_change",
    "rank", "rolling", "shift", "std", "sum", "where",
})
_FACTOR_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "svquant_factor_generation",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "factor_name", "factor_code", "description", "used_fields",
                "used_library_methods", "assumptions", "warnings", "outcome", "reason",
                "minimum_history", "missing_value_handling",
            ],
            "properties": {
                "factor_name": {"type": "string"},
                "factor_code": {"type": "string"},
                "description": {"type": "string"},
                "used_fields": {"type": "array", "items": {"type": "string"}},
                "used_library_methods": {"type": "array", "items": {"type": "string"}},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "outcome": {"type": "string", "enum": ["factor", "unsupported", "needs_clarification"]},
                "reason": {"type": "string"},
                "minimum_history": {"type": "string"},
                "missing_value_handling": {"type": "string"},
            },
        },
    },
}
_IDEMPOTENCY: dict[str, tuple[str, FactorGenerationResponse]] = {}
_IDEMPOTENCY_LOCK = Lock()


def generate_factor(req: FactorGenerationRequest) -> FactorGenerationResponse:
    """调用 Provider，解析输出，并在返回前执行静态校验。"""
    request_hash = _request_hash(req)
    key = str(req.idempotency_key)
    with _IDEMPOTENCY_LOCK:
        existing = _IDEMPOTENCY.get(key)
        if existing is not None:
            if existing[0] != request_hash:
                raise HTTPException(409, "idempotency_key 已用于不同请求")
            return existing[1]

    settings = _settings_from_env()
    started_at = time.perf_counter()
    try:
        messages = _messages(req)
        raw_content = _call_provider(settings, messages)
    except TimeoutError as exc:
        raise HTTPException(504, "AI 供应商请求超时") from exc
    except ProviderUnavailable as exc:
        raise HTTPException(503, "AI 供应商暂不可用") from exc

    response = _validate_provider_output(raw_content, settings.model, started_at)
    # 仅对可执行性失败做一次受控修正；数据不支持/需求不清楚时直接说明原因。
    if response.status == "rejected" and not _is_request_rejection(response.validation.errors):
        repair_messages = messages + [
            {"role": "assistant", "content": raw_content},
            {"role": "user", "content": (
                "草稿未通过平台静态校验：" + "；".join(response.validation.errors)
                + "。请保持原需求的公式、周期和缺失值规则，仅修正不兼容语法，重新输出完整JSON。"
                "不要删除安全校验，不要改用替代数据；无法满足则返回unsupported及reason。"
            )},
        ]
        try:
            repaired = _call_provider(settings, repair_messages)
        except (TimeoutError, ProviderUnavailable):
            # 保留清楚的失败响应，不因修正请求超时而丢掉首次校验信息。
            response.validation.warnings.append("自动修正暂未完成，请稍后重试")
        else:
            response = _validate_provider_output(repaired, settings.model, started_at)
    with _IDEMPOTENCY_LOCK:
        # 竞争请求也只保留首次完成的结果，避免重复计费结果不一致。
        existing = _IDEMPOTENCY.setdefault(key, (request_hash, response))
    if existing[0] != request_hash:
        raise HTTPException(409, "idempotency_key 已用于不同请求")
    return existing[1]


def _settings_from_env() -> ProviderSettings:
    # 环境变量优先，未设置时使用 config.yaml 中的共享 AI 配置。
    config = get_runtime_settings()
    api_key = (
        os.environ.get("AI_FACTOR_API_KEY")
        or os.environ.get("ARK_API_KEY")
        or config.ai_api_key
    )
    if not api_key:
        raise HTTPException(503, "AI 因子生成服务尚未配置")
    return ProviderSettings(
        base_url=os.environ.get("AI_FACTOR_BASE_URL", config.ai_base_url).rstrip("/"),
        api_key=api_key,
        model=os.environ.get("AI_FACTOR_MODEL_ALIAS", config.ai_model),
    )


def _messages(req: FactorGenerationRequest) -> list[dict[str, str]]:
    libraries = ", ".join(req.context.allowed_library_names) or "无"
    system = (
        "你是 SVQuant 因子生成器。只输出 JSON 对象，不要 Markdown。"
        "JSON 必须有 factor_name、factor_code、description、used_fields、"
        "used_library_methods、assumptions、warnings、outcome、reason、minimum_history、missing_value_handling。"
        "outcome为factor表示可生成，reason为空字符串；"
        "若需求与因子研究无关或缺少计算逻辑，outcome为needs_clarification并用reason说明需补充什么；"
        "若需要未支持的数据或操作，outcome为unsupported并用reason明确说明能力边界。"
        "后两种情况factor_name、factor_code、description、minimum_history、missing_value_handling为空字符串，数组为空；"
        "不要通过raise、返回常数或替代数据伪造可用因子。"
        "可生成时factor_code 必须只含一个 "
        "def 方法，第一个参数为 self。只可读取字段："
        f"{', '.join(sorted(ALLOWED_FIELDS))}。禁止 import、文件、网络、动态执行、"
        "双下划线属性和投资建议。不要调用 self.get_data、self.get_field 或任何未列出的 "
        "self 方法。允许的面板方法仅为："
        f"{', '.join(sorted(ALLOWED_METHODS))}。"
        "不允许使用np、pd、math、sqrt、pow方法、astype或外部函数；平方根使用 ** 0.5。"
        "允许简单赋值、算术、比较及return，不使用if、raise、循环或辅助函数。"
        "只使用当前及历史数据；shift和pct_change的periods必须是可确定的非负整数，"
        "可以使用正整数默认参数及其简单算术，不得用负周期或rolling(center=True)读取未来数据。"
        "缺失值保留为空；pct_change需显式fill_method=None；滚动窗口明确min_periods，"
        "前值为0时用where置空。description解释计算公式、输出单位和含义；"
        "minimum_history必须具体解释最少历史观察值，missing_value_handling必须解释缺失值及零分母处理，"
        "两者都不能留空；assumptions补充其他假设，覆盖用户明确提出的说明要求。"
        "例如20个日收益率需至少21个收盘价，rolling(20,min_periods=20).std(ddof=1)，"
        "在价格完整时前20个观察位置为空，第21个可输出；不能误写前21日均为空。"
        "不是跳过缺失值向更早日期凑20条。不得声称未做的处理。"
        "合法示例：def momentum_20(self, window=20):\n"
        "    previous = self.close.shift(window)\n"
        "    return self.close / previous.where(previous != 0) - 1"
    )
    user = (
        f"研究想法：{req.prompt}\n"
        f"频率：{req.context.frequency}\n"
        f"股票池：{req.context.universe or '未指定'}\n"
        f"内置库上下文：{libraries}（本接口仍只允许上述面板表达式，不调用内置库方法）\n"
        f"偏好名称：{req.context.preferred_factor_name or '由你命名'}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _call_provider(settings: ProviderSettings, messages: list[dict[str, str]]) -> str:
    return llm_provider.complete(
        settings,
        messages,
        response_format=_FACTOR_RESPONSE_SCHEMA,
    )


def _validate_provider_output(raw: str, model: str, started_at: float) -> FactorGenerationResponse:
    latency_ms = round((time.perf_counter() - started_at) * 1000)
    generation_id = f"fg_{uuid4().hex}"
    try:
        data = json.loads(raw.strip())
        outcome = data.get("outcome") if isinstance(data, dict) else None
        if outcome in ("unsupported", "needs_clarification"):
            label = "需求暂不支持" if outcome == "unsupported" else "请补充因子需求"
            reason = str(data.get("reason") or "请说明可用数据、计算公式和周期").strip()
            errors = [f"{label}：{reason}"]
            factor = None
        elif outcome != "factor":
            errors = ["模型返回了无效的需求处理状态"]
            factor = None
        else:
            factor = GeneratedFactor.model_validate(data)
            errors = _validate_factor_code(factor)
            # 模型协议必填；映射到已有 assumptions，公开接口与前端无需迁移。
            for field, label in (("minimum_history", "历史数据要求"), ("missing_value_handling", "缺失值处理")):
                value = data.get(field)
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"生成说明缺少{label}")
                else:
                    note = f"{label}：{value.strip()}"
                    if note not in factor.assumptions:
                        factor.assumptions.append(note)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        errors = [f"模型输出不是有效的因子草稿：{exc}"]
        factor = None

    validation = FactorGenerationValidation(
        status="passed" if not errors else "failed",
        capability_catalog_version=CATALOG_VERSION,
        errors=errors,
    )
    return FactorGenerationResponse(
        generation_id=generation_id,
        status="ready" if factor is not None and not errors else "rejected",
        factor=factor if not errors else None,
        validation=validation,
        meta=FactorGenerationMeta(
            model_alias=model, prompt_version=PROMPT_VERSION, latency_ms=latency_ms,
        ),
    )


def _is_request_rejection(errors: list[str]) -> bool:
    return any(error.startswith(("需求暂不支持：", "请补充因子需求：")) for error in errors)


def _validate_factor_code(factor: GeneratedFactor) -> list[str]:
    unknown_fields = sorted(set(factor.used_fields) - ALLOWED_FIELDS)
    if unknown_fields:
        return [f"used_fields 包含未登记字段：{', '.join(unknown_fields)}"]
    try:
        tree = ast.parse(factor.factor_code)
    except SyntaxError as exc:
        return [f"因子代码语法错误：{exc.msg}"]
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        return ["因子代码必须且只能包含一个函数定义"]
    function = tree.body[0]
    if function.name != factor.factor_name or not function.args.args or function.args.args[0].arg != "self":
        return ["函数名必须与 factor_name 一致，且第一个参数必须为 self"]
    if function.decorator_list:
        return ["因子代码不允许使用装饰器"]

    validator = _FactorAstValidator()
    validator.visit(tree)
    if validator.errors:
        return validator.errors
    try:
        # 因子回测实际会把函数缩进并包装为 FactorLibrary 子类；这里复用相同规则。
        from app.services.factor.runner import _wrap_factor_code

        compile(_wrap_factor_code(factor.factor_code), "<generated_factor>", "exec")
    except SyntaxError as exc:
        return [f"因子代码无法编译：{exc.msg}"]
    return []


class _FactorAstValidator(ast.NodeVisitor):
    """默认拒绝的 AST 检查器，仅接受简单面板计算表达式。"""

    _ALLOWED_NODES = {
        ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
        ast.Name, ast.Load, ast.Store, ast.Attribute, ast.Call, ast.Constant,
        ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
        ast.USub, ast.Compare, ast.Eq, ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE,
        ast.keyword,
    }

    def __init__(self):
        self.errors: list[str] = []
        self.scalar_values: dict[str, object] = {}

    def _scalar(self, node):
        """仅推导简单参数/赋值常量，不执行模型表达式。"""
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self.scalar_values.get(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            value = self._scalar(node.operand)
            return -value if type(value) in (int, float) else None
        if isinstance(node, ast.BinOp):
            left, right = self._scalar(node.left), self._scalar(node.right)
            if type(left) not in (int, float) or type(right) not in (int, float):
                return None
            if abs(left) > 1_000_000 or abs(right) > 1_000_000:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
        return None

    def visit_FunctionDef(self, node: ast.FunctionDef):
        args = node.args.posonlyargs + node.args.args
        for arg, default in zip(args[len(args) - len(node.args.defaults):], node.args.defaults):
            self.scalar_values[arg.arg] = self._scalar(default)
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            self.scalar_values[arg.arg] = self._scalar(default)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        self.visit(node.value)
        value = self._scalar(node.value)
        for target in node.targets:
            self.visit(target)
            if isinstance(target, ast.Name):
                self.scalar_values[target.id] = value

    def generic_visit(self, node):
        if type(node) not in self._ALLOWED_NODES:
            self.errors.append(f"不允许的代码结构：{type(node).__name__}")
            return
        super().generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if node.attr.startswith("__"):
            self.errors.append("不允许访问双下划线属性")
        if isinstance(node.value, ast.Name) and node.value.id == "self" and node.attr not in ALLOWED_FIELDS:
            self.errors.append(f"不允许的字段：{node.attr}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in ALLOWED_METHODS:
            self.errors.append("只允许调用白名单中的面板方法")
        elif node.func.attr in ("shift", "pct_change"):
            period_node = next((kw.value for kw in node.keywords if kw.arg == "periods"), node.args[0] if node.args else ast.Constant(1))
            period = self._scalar(period_node)
            if type(period) is not int or period < 0:
                self.errors.append("禁止未来数据：shift/pct_change回看周期须为可确定的非负整数")
            if any(kw.arg == "freq" and not (isinstance(kw.value, ast.Constant) and kw.value.value is None) for kw in node.keywords):
                self.errors.append("禁止未来数据：请使用交易行回看，不使用freq日期偏移")
            if any(kw.arg == "fill_method" and self._scalar(kw.value) in ("bfill", "backfill") for kw in node.keywords):
                self.errors.append("禁止未来数据：不允许用未来价格反向填充缺失值")
        elif node.func.attr == "rolling":
            centered = next((kw.value for kw in node.keywords if kw.arg == "center"), node.args[2] if len(node.args) > 2 else ast.Constant(False))
            if self._scalar(centered) is not False:
                self.errors.append("禁止未来数据：rolling必须使用非居中窗口center=False")
        elif node.func.attr == "fillna":
            if any(kw.arg == "method" and self._scalar(kw.value) != "ffill" and self._scalar(kw.value) != "pad" for kw in node.keywords):
                self.errors.append("禁止未来数据：不允许反向填充或无法确定方向的缺失值填充")
        self.generic_visit(node)


def _request_hash(req: FactorGenerationRequest) -> str:
    content = json.dumps(req.model_dump(mode="json", exclude={"idempotency_key"}), sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()
