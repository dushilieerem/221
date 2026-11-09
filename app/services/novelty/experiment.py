"""按显式对照清单复用 DataEngine/FactorEngine/ExperimentManager 计算相关性。"""

import hashlib
import json
import time
from dataclasses import replace
import pandas as pd
from app.models.data import Panel
from app.schemas.backtest import FactorBacktestRequest
from app.services.data.engine import DataEngine
from app.services.experiment.base import Experiment
from app.services.factor.engine import FactorEngine
from app.services.factor.runner import _request_spec
from .comparison import compare_panels


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


class NoveltyExperiment(Experiment):
    """按冻结清单逐参考计算真实因子值相关性；只做比较，不做回测。"""

    def __init__(self, request, progress):
        self.request = request
        self.progress = progress

    def run(self):
        req = self.request
        started = time.monotonic()
        engine = DataEngine.get_instance()
        release = str(engine.release_id)

        def checkpoint():
            if self.progress.get("cancelled"):
                raise RuntimeError(
                    "查重任务在参考计算之间被取消"
                )
            if time.monotonic() - started > 1200:
                raise TimeoutError("查重计算超过 1200 秒")
            if str(engine.release_id) != release:
                raise RuntimeError("比较过程中数据版本发生变化")
            signature = getattr(engine.provider, "current_release_signature", None)
            if callable(signature) and signature() != release:
                raise RuntimeError("比较过程中数据文件发生变化")

        if release != req.expected_data_release_id:
            raise ValueError("数据版本与研发报告不一致")
        spec = _request_spec(req.candidate)
        if spec.factor_source_hash != req.expected_candidate_source_hash:
            raise ValueError("候选源码与研发报告不一致")
        candidate_spec_hash = spec.identity(data_release_id=release)
        if candidate_spec_hash != req.expected_candidate_spec_hash:
            raise ValueError("候选回测规格与研发报告不一致")
        checkpoint()
        base = FactorEngine(spec)
        values = base.compute()
        if values.empty:
            raise ValueError("候选因子面板为空")
        checkpoint()
        # The candidate uses precisely the backtest window. References may need older
        # observations, but comparison and eligibility remain on the candidate dates.
        start = pd.Timestamp(spec.start)
        end = spec.end
        warmup = max(
            req.reference_warmup_sessions,
            max((r.max_window or 0 for r in req.references), default=0),
        )
        lookup = (start - pd.Timedelta(days=warmup * 2 + 60)).date().isoformat()
        prior = engine.get_task_panels(lookup, end, symbols=list(values.columns))[
            "close"
        ]
        dates = prior.index[prior.index < start][-warmup:]
        warm_start = str(pd.Timestamp(dates[0]).date()) if len(dates) else spec.start
        fields = engine.get_task_panels(warm_start, end, symbols=list(values.columns))
        panel = Panel(
            fields,
            symbols=list(values.columns),
            index_loader=lambda name: engine.get_index(name, warm_start, end),
        )
        rows = []
        for reference in req.references:
            checkpoint()
            row = {
                "reference_id": reference.reference_id,
                "factor_name": reference.factor_name,
                "status": "failed",
                "source_hash": None,
            }
            try:
                if len(dates) < warmup:
                    raise ValueError("参考因子预热历史不足")
                source_request = FactorBacktestRequest(
                    factor_name=reference.factor_name,
                    factor_code=reference.factor_code,
                    library_name=reference.library_name,
                )
                source_spec = _request_spec(source_request)
                row["source_hash"] = source_spec.factor_source_hash
                if (
                    reference.source_hash
                    and source_spec.factor_source_hash != reference.source_hash
                ):
                    raise ValueError("参考因子源码哈希不匹配")
                ref_spec = replace(
                    spec,
                    factor_name=source_spec.factor_name,
                    factor_code=source_spec.factor_code,
                    library_names=source_spec.library_names,
                    factor_source_hash=source_spec.factor_source_hash,
                )
                factor_engine = FactorEngine()
                raw = factor_engine.compute_from_spec(ref_spec, panel).reindex(
                    index=values.index, columns=values.columns
                )
                ref_values = factor_engine.preprocess(
                    raw,
                    eligibility_mask=base.eligibility_mask,
                    winsorize=spec.winsorize,
                    standardize=spec.standardize,
                )
                if (
                    _request_spec(source_request).factor_source_hash
                    != source_spec.factor_source_hash
                ):
                    raise ValueError("参考因子源码在计算过程中变化")
                row.update(compare_panels(values, ref_values, req.min_pairs))
            except Exception as exc:
                row.update(
                    status="failed",
                    error=type(exc).__name__ + ": " + str(exc)[:400],
                    mean_abs_spearman=None,
                    valid_days=0,
                    min_pairs=0,
                    coverage=0.0,
                    series=[],
                )
            rows.append(row)
            self.progress.update(
                processed_references=len(rows), current_reference=reference.reference_id
            )
        checkpoint()
        identity = digest(req.model_dump(mode="json"))
        source_hash = digest([(r["reference_id"], r["source_hash"]) for r in rows])
        meta = {
            "version": "factor-novelty-v1",
            "data_release_id": release,
            "candidate_source_hash": spec.factor_source_hash,
            "request_hash": identity,
            "reference_source_hash": source_hash,
            "factor_price_adjustment": "hfq",
            "reference_warmup_sessions_requested": req.reference_warmup_sessions,
            "reference_warmup_sessions_effective": warmup,
            "reference_warmup_sessions_available": len(dates),
            "candidate_warmup_sessions": 0,
            "reference_warmup_start": warm_start,
            "candidate_backtest_spec_hash": candidate_spec_hash,
            "filter_diagnostics": base.result_diagnostics(),
            "method": "daily_abs_spearman",
            "coverage_definition": "共同股票日 / 有效相关日上的候选有效股票日",
            "limitations": [
                "候选取研发回测窗口；参考因子预热历史不计入评分日期。",
                "coverage 以有效相关日期计算；valid_days 是独立诊断，不是硬门槛。",
                "预热默认 252 个交易日；更长依赖须用 reference.max_window 声明。递归滤波可能需要更多历史。",
                "残差 RankIC 与组合增量收益未实现。",
            ],
        }
        meta["spec_hash"] = digest(
            {
                "request_hash": identity,
                "reference_source_hash": source_hash,
                "data_release_id": release,
                "version": meta["version"],
            }
        )
        return {
            "assessment_status": (
                "complete"
                if all(r["status"] == "succeeded" for r in rows)
                else "insufficient_data"
            ),
            "meta": meta,
            "expected_count": len(req.references),
            "comparisons": rows,
            "residual": {"status": "not_supported"},
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
