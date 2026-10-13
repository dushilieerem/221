"""``DataEngine`` - 应用级市场数据快照与统一读取引擎。

依据 ``docs/99-历史归档/FEAT-001-QuantCore基础能力/04-class-design.md`` DataEngine 节：

    属性：
      provider: IDataProvider    -- 当前已加载 release 的底层来源

    方法：
      set_provider(provider: IDataProvider)
      get_bars(symbol, start, end, fields) -> DataFrame
      get_universe(date) -> List[str]

FastAPI 进程在启动期创建唯一的 DataEngine 并加载活动 release；Experiment
只从它读取共享只读数据或取得任务私有切片。DataEngine 不属于
ExperimentManager，Manager 只负责调度。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
import inspect
import threading
from pathlib import Path
from typing import Any, List, Optional, Sequence, TypeVar

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from app.database.data.interface import IDataProvider
from app.database.data.field_cache import FieldFrameCache
from app.models.market_data import INVESTABLE_LISTED_STATUS


_T = TypeVar("_T")
_CORE_PROVIDER_METHODS = (
    "get_bars",
    "get_universe",
    "get_symbols",
)


def _align_to(frame: pd.DataFrame, index: pd.Index, columns: pd.Index) -> pd.DataFrame:
    """把面板对齐到目标 index/columns；形状一致时直接返回，避免空拷贝。

    全市场面板的 ``reindex`` 是纯拷贝（2600×5900 约 0.8 秒），而调用方拿到的
    字段大多本来就已经是对的形状，先比较再决定是否重排能省掉这笔开销。
    """

    if frame.index.equals(index) and frame.columns.equals(columns):
        return frame
    return frame.reindex(index=index, columns=columns)


def default_market_data_dir() -> Path:
    """返回配置指定的外部行情根目录。"""

    from app.config import get_runtime_settings

    return get_runtime_settings().data_dir


class DataEngine:
    """统一数据读取入口。

    上层（app）只与本类交互，不接触任何具体数据源实现。
    切换数据源只需调用 :meth:`set_provider`。
    """

    def __init__(self, provider: Optional[IDataProvider] = None) -> None:
        self.provider: Optional[IDataProvider] = None
        self._cache: FieldFrameCache | None = None
        self._closed = False
        # 上一次构造的退市掩码与其身份。退市剔除恒开启，掩码构造（全市场约
        # 6 秒）必须能在同一 release 的重复任务间复用 —— 批量回测逐个因子都
        # 用同一股票池和区间，命中率接近 100%。
        self._delisted_mask_cache: tuple[str, pd.Index, pd.Index, pd.DataFrame] | None = None
        if provider is not None:
            self.load(provider)

    @classmethod
    def get_instance(cls) -> "DataEngine":
        """返回本 ResearchEngine 进程唯一的 DataEngine。"""

        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def configure_instance(cls, instance: "DataEngine") -> "DataEngine":
        """由应用启动装配全局实例；测试可注入隔离实例。"""

        with cls._instance_lock:
            cls._instance = instance
        return instance

    @staticmethod
    def _validate_provider(provider: Any) -> None:
        """在切换数据源时尽早检查核心读取契约。

        这里使用结构化检查而不是强制 ``isinstance(IDataProvider)``，既能
        拦截误传的普通对象，也保留测试替身和外部 provider 的鸭子类型兼容性。
        ``get_panels``、``get_index`` 与 ``get_universe_at`` 属于可选能力，
        分别在调用时使用默认实现或给出 provider 的明确错误/回退行为。
        """

        missing = [
            name
            for name in _CORE_PROVIDER_METHODS
            if not callable(getattr(provider, name, None))
        ]
        if missing:
            raise TypeError(
                "provider 不满足 IDataProvider 读取契约，缺少方法: "
                f"{missing}"
            )

    def load(self, provider: IDataProvider) -> None:
        """加载一个数据 release，成为本 DataEngine 的唯一读取来源。

        DataEngine 只保存数据来源；字段由任务按时间范围读取，不做全量预热。
        """
        if self._closed:
            raise RuntimeError("DataEngine 已关闭")
        if provider is None:
            self.provider = None
            return
        self._validate_provider(provider)
        self.provider = provider
    def load_parquet(
        self,
        data_dir: str | Path,
    ) -> None:
        """从 MarketData Parquet 加载当前 release。

        字段缓存是 DataEngine 私有状态，不再由 provider 或 Manager 取得
        进程全局缓存。先构建新 provider，成功后才替换当前 release；构建
        失败时保持原 release 与缓存不变（供发布监视器安全重试）。
        """
        from app.database.data.providers.field_parquet_provider import FieldParquetProvider

        cache = FieldFrameCache()
        provider = FieldParquetProvider(data_dir, cache=cache)
        self._cache = cache
        self.load(provider)

    # ``set_provider`` 是已有低层使用方的同义入口；新代码应使用 ``load``，
    # 以强调这是一次 release 装载而非任务期间的任意切换。
    def set_provider(self, provider: IDataProvider) -> None:
        self.load(provider)

    def _require_provider(self) -> IDataProvider:
        if self._closed:
            raise RuntimeError("DataEngine 已关闭")
        if self.provider is None:
            raise RuntimeError(
                "DataEngine 尚未设置数据源，请先调用 set_provider(...)。"
            )
        return self.provider

    def _call(self, method: str, *args: Any, **kwargs: Any) -> _T:
        """统一调用 provider，避免各公开方法重复实现委托和契约检查。"""

        provider = self._require_provider()
        operation = getattr(provider, method, None)
        if operation is None and method == "get_panels":
            # 允许只实现逐标的核心契约的轻量 provider 复用
            # IDataProvider 的默认宽表拼装逻辑。
            return IDataProvider.get_panels(provider, *args, **kwargs)  # type: ignore[arg-type]
        if not callable(operation):
            raise TypeError(
                f"当前 provider {type(provider).__name__} 未实现 {method}(...)"
            )
        return operation(*args, **kwargs)

    @property
    def provider_name(self) -> str | None:
        """当前数据源名称，未配置 provider 时返回 ``None``。"""

        provider = self.provider
        if provider is None:
            return None
        return str(getattr(provider, "name", type(provider).__name__))

    @property
    def data_dir(self) -> Path | None:
        """当前 release 的物理目录；非文件型 provider 时可能不存在。"""

        value = getattr(self.provider, "data_dir", None)
        return Path(value).resolve() if value is not None else None

    @property
    def release_id(self) -> str | None:
        """当前已加载 release 的稳定身份。"""

        value = getattr(self.provider, "release_id", None)
        return str(value) if value is not None else None

    @property
    def data_engine(self) -> "DataEngine":
        """只读上下文消费方取得自身，不复制或创建新 Engine。"""

        return self

    def cache_stats(self) -> dict[str, int]:
        """返回本 DataEngine 私有字段缓存状态。"""

        if self._cache is not None:
            return self._cache.stats()
        stats = getattr(self.provider, "cache_stats", None)
        return stats() if callable(stats) else {}

    def close(self) -> None:
        """释放当前 release；应在所有 Experiment 停止后由应用调用。"""

        self.provider = None
        if self._cache is not None:
            self._cache.clear()
        self._delisted_mask_cache = None
        self._closed = True

    def get_bars(
        self,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        fields: Optional[Sequence[str]] = None,
    ) -> pd.DataFrame:
        """读取单个标的行情，委托给当前 provider。"""
        return self._call("get_bars", symbol, start, end, fields)

    def get_universe(self, date: Optional[str] = None) -> List[str]:
        """获取股票池，委托给当前 provider。"""
        return self._call("get_universe", date)

    def get_universe_at(self, name: str, as_of: str | pd.Timestamp) -> List[str]:
        """获取指定时点的命名股票池；不支持历史快照时兼容旧 provider。"""
        provider = self._require_provider()
        resolver = getattr(provider, "get_universe_at", None)
        if callable(resolver):
            return resolver(name, as_of)
        return self._call("get_universe", name)

    def get_symbols(self) -> List[str]:
        """获取全部已知标的，委托给当前 provider。"""
        return self._call("get_symbols")

    def get_panels(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        fields: Optional[Sequence[str]] = None,
    ) -> Mapping[str, pd.DataFrame]:
        """获取多字段宽表面板，委托给当前 provider。"""
        return self._call("get_panels", start, end, fields)

    def get_task_panels(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        fields: Optional[Sequence[str]] = None,
        symbols: Optional[Sequence[str]] = None,
    ) -> Mapping[str, pd.DataFrame]:
        """返回当前 Experiment 的私有行情面板。

        ``symbols`` 是可选的任务级列裁剪；宽表 provider 可以在读盘时只投影
        指定列，再为任务深拷贝，从而避免先复制全市场再由 Panel 重切一次。
        """

        provider = self._require_provider()
        task_panels = getattr(provider, "get_task_panels", None)
        if callable(task_panels):
            if symbols is None:
                return task_panels(start, end, fields)
            # ``get_task_panels`` 是宽表 provider 的扩展能力，不属于旧版
            # IDataProvider 必选接口。旧 provider 可能仍只有三参数签名；在
            # 这种情况下保留它的任务隔离语义，让 Panel 在访问时再裁剪列。
            try:
                parameters = inspect.signature(task_panels).parameters.values()
            except (TypeError, ValueError):
                parameters = ()
            accepts_symbols = any(
                parameter.name == "symbols"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            if accepts_symbols:
                return task_panels(start, end, fields, symbols=symbols)
            legacy_panels = task_panels(start, end, fields)
            if isinstance(legacy_panels, Mapping):
                requested = pd.Index(symbols)
                return {
                    name: frame.reindex(columns=requested).copy(deep=True)
                    for name, frame in legacy_panels.items()
                }
            return legacy_panels
        panels = self.get_panels(start, end, fields)
        requested = None if symbols is None else pd.Index(symbols)
        return {
            name: (
                frame.reindex(columns=requested).copy(deep=True)
                if requested is not None
                else frame.copy(deep=True)
            )
            for name, frame in panels.items()
        }

    def get_research_data(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        fields: Optional[Sequence[str]] = None,
    ):
        """返回按当前 MarketData 业务域建模的研究数据视图。

        此入口不改变 ``get_panels`` 的兼容契约。业务域对象中的 ``close``、
        ``pb_mrq``、``up_and_down_status`` 等字段仍是原生 DataFrame，可直接参与
        因子和策略的 Pandas 表达式。
        """

        from app.models.market_data import MarketDataRelease
        from app.services.data.panel import ResearchDataPanel

        provider = self._require_provider()
        release = MarketDataRelease.from_panels(
            self.get_panels(start, end, fields),
            data_version=getattr(provider, "data_version", None),
        )
        return ResearchDataPanel(release)

    def get_index(
        self,
        name: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """读取指数行情，委托给当前 provider。"""
        return self._call("get_index", name, start, end)

    def get_industry_map(self, data_dir: str | None = None) -> pd.Series | None:
        """读取可选股票行业映射。

        行业元数据不是行情面板，仍由 DataEngine 负责解析；找不到文件或
        缺少行业列时返回 ``None``，由 full 分析按可选能力降级。
        """

        from pathlib import Path

        root = Path(data_dir) if data_dir else getattr(self.provider, "data_dir", None)
        if root is None:
            return None
        root = Path(root)
        if (root / "current").is_dir():
            root = root / "current"
        path = root / "metadata" / "stock_info.parquet"
        if not path.exists():
            path = root / "stock_info.parquet"
        if not path.exists():
            return None
        try:
            frame = pd.read_parquet(path)
            if "industry" not in frame.columns:
                return None
            for column in ("code", "symbol", "ts_code"):
                if column in frame.columns:
                    return frame.set_index(column)["industry"]
            return frame["industry"]
        except (OSError, ValueError, KeyError):
            return None

    def build_status_masks(
        self,
        panels: Mapping[str, pd.DataFrame] | Any,
        *,
        needed: Collection[str] | None = None,
    ) -> dict[str, pd.DataFrame]:
        """从证券状态原始文本构造日期×标的布尔掩码。

        当前数据源的 ``listed_status`` 和 ``up_and_down_status`` 都是原始文本
        面板，不是布尔字段。返回的掩码包括 ``is_st``、``is_limit_up``、
        ``is_limit_down``、``is_limit``、``is_suspended`` 和 ``is_delisted``。
        除 ``is_delisted`` 外，字段缺失与单元格缺失一律按 ``False`` 处理，由
        调用方决定是否需要对覆盖不足发出告警。

        ``needed`` 可限定只构造其中若干键，其余键返回全 False 的占位掩码。
        退市剔除恒开启，构造掩码不再是可选开销，因此调用方应按需索取，
        避免为了没开的过滤项白算 ``up_and_down_status``（全市场约 6 秒）。

        ``is_delisted`` 的口径与其他掩码相反：``listed_status`` 不在
        :data:`app.models.market_data.INVESTABLE_LISTED_STATUS` 白名单内即为
        ``True``，**缺失值也按退市处理**。``up_and_down_status`` 无法用于
        识别退市（退市后该字段绝大多数为缺失），不要用它做退市判定。
        """

        def has_field(name: str) -> bool:
            try:
                return name in panels
            except TypeError:
                return name in getattr(panels, "fields", ())

        def get_field(name: str) -> pd.DataFrame:
            try:
                return panels[name]
            except (AttributeError, KeyError, TypeError):
                return getattr(panels, name)

        reference = None
        if has_field("close"):
            reference = get_field("close")
        else:
            for name in getattr(panels, "fields", panels):
                if has_field(str(name)):
                    reference = get_field(str(name))
                    break
        if reference is None:
            raise ValueError("无法构造状态掩码：面板没有可用字段")

        index = reference.index
        columns = reference.columns
        # 未启用的过滤项与缺失字段都用这一个全 False 占位掩码，不再逐项复制：
        # 全市场尺寸下每次复制约 0.3 秒，而占位掩码恒为全 False、语义相同，
        # 返回同一个实例是安全的（消费方只读取，不会原地改写）。
        empty = pd.DataFrame(False, index=index, columns=columns)

        def status_coverage(name: str) -> pd.Series | None:
            """该状态字段在原始面板里的列覆盖情况（整列为空 = 未覆盖该标的）。"""

            if not has_field(name):
                return None
            return get_field(name).notna().any()

        def aligned(frame: pd.DataFrame) -> pd.DataFrame:
            return _align_to(frame, index, columns)

        def trimmed_flat(frame: pd.DataFrame) -> object | None:
            """把状态面板压成一维、按 Arrow 向量化去空白。

            pandas 会**逐列**执行 ``DataFrame.isin`` 与 ``Series.str.strip``，
            全市场 5946 列时每次约 1.6 / 5.4 秒。压成一维后 trim 与集合判定
            各自只需一次调用，整表不到 1 秒。非字符串后端返回 ``None``，由
            调用方回退到逐列实现。
            """

            try:
                table = pa.Table.from_pandas(frame, preserve_index=False)
                flat = pa.concat_arrays(
                    [chunk for column in table.columns for chunk in column.chunks]
                )
                return pc.utf8_trim_whitespace(flat)
            except (pa.ArrowException, ValueError, TypeError):
                return None

        # 同一状态字段会被多个谓词复用（up_and_down_status 最多三次），
        # 对齐后的面板与去空白结果各算一次即可。
        field_state: dict[str, tuple[pd.DataFrame, object | None]] = {}

        def aligned_status(name: str) -> tuple[pd.DataFrame, object | None]:
            if name not in field_state:
                frame = aligned(get_field(name))
                field_state[name] = (frame, trimmed_flat(frame))
            return field_state[name]

        def status_mask(name: str, matching: frozenset[str], *, negate: bool = False) -> pd.DataFrame:
            """构造「规范文本属于 ``matching``」的布尔掩码。

            缺失值与无效值一律为 ``False``（``negate`` 时取反前仍是 ``False``）。
            """

            if not has_field(name):
                return empty
            frame, trimmed = aligned_status(name)
            if trimmed is None:
                # 回退：小面板或非 Arrow 后端，逐列 strip 后比较。
                plain = frame.astype("string").apply(lambda column: column.str.strip())
                result = plain.isin(matching).fillna(False).astype(bool)
            else:
                membership = pc.is_in(trimmed, value_set=pa.array(sorted(matching)))
                array = membership.fill_null(False).to_numpy(zero_copy_only=False)
                array = array.reshape(frame.shape[1], frame.shape[0]).T
                if negate:
                    array = ~array
                return pd.DataFrame(array, index=frame.index, columns=frame.columns)
            return ~result if negate else result

        def wanted(key: str) -> bool:
            return needed is None or key in needed

        def cached_delisted_mask() -> pd.DataFrame:
            """构造 ``is_delisted`` 掩码，按 release + 面板身份复用上一次结果。

            只有当 DataEngine 绑定到真实 release 时才缓存：此时同一 release
            的同一 (index, columns) 切片恒有相同内容，缓存键是完备的。测试与
            轻量 provider 直接传 dict 时没有 release 身份，不做缓存，避免
            相同形状但内容不同的面板之间串味。
            """

            release = self.release_id
            if release is not None and self._delisted_mask_cache is not None:
                cached_release, cached_index, cached_columns, cached = (
                    self._delisted_mask_cache
                )
                if (
                    cached_release == release
                    and cached_index.equals(index)
                    and cached_columns.equals(columns)
                ):
                    return cached
            delisted_mask = status_mask(
                "listed_status", INVESTABLE_LISTED_STATUS, negate=True
            )
            # 按任务股票池做列投影时，池内但状态面板未覆盖的标的会得到一整列
            # 空值。这类「整列无覆盖」不等于退市（生产 release 每个标的都有
            # 状态记录，这里防的是投影产物与外部数据源），按可投资处理；只有
            # 确实被覆盖、但该日状态缺失的单元格才按不可投资处理。
            listed_coverage = status_coverage("listed_status")
            if listed_coverage is not None:
                covered = listed_coverage.reindex(columns).fillna(False).astype(bool)
                if not covered.all():
                    delisted_mask.loc[:, covered[~covered].index] = False
            if release is not None:
                self._delisted_mask_cache = (release, index, columns, delisted_mask)
            return delisted_mask

        delisted = cached_delisted_mask() if wanted("is_delisted") else empty

        return {
            "is_st": (
                status_mask("listed_status", frozenset({"ST", "*ST"}))
                if wanted("is_st")
                else empty
            ),
            "is_delisted": delisted,
            "is_limit_up": (
                status_mask("up_and_down_status", frozenset({"涨停"}))
                if wanted("is_limit_up")
                else empty
            ),
            "is_limit_down": (
                status_mask("up_and_down_status", frozenset({"跌停"}))
                if wanted("is_limit_down")
                else empty
            ),
            "is_limit": (
                status_mask("up_and_down_status", frozenset({"涨停", "跌停"}))
                if wanted("is_limit")
                else empty
            ),
            "is_suspended": (
                status_mask("up_and_down_status", frozenset({"停牌"}))
                if wanted("is_suspended")
                else empty
            ),
        }

    def build_filter_mask(
        self,
        panels: Mapping[str, pd.DataFrame] | Any,
        *,
        filter_suspended: bool = False,
        filter_st: bool = False,
        filter_limit_up: bool = False,
        filter_limit_down: bool = False,
    ) -> dict[str, Any]:
        """根据可选行情状态构造因子研究有效池。

        返回普通映射 ``{"mask", "status", "warnings", "status_masks"}``，其中 mask 为
        ``True=允许进入研究样本``。该方法只读取状态，不删除行情行，也不
        修改传入面板；缺少可选状态字段时跳过对应过滤并留下可序列化告警。
        ``Panel`` 和 provider 返回的字段映射都可直接传入。

        与四个可选开关不同，**退市剔除恒开启且没有开关**：``listed_status`` 不在
        白名单内的标的（含缺失值）一律排除出研究样本，避免退市后被前值填充
        冻结的行情污染因子。调用方无法关闭它。
        """

        def has_field(name: str) -> bool:
            try:
                return name in panels
            except TypeError:
                return name in getattr(panels, "fields", ())

        def get_field(name: str) -> pd.DataFrame:
            try:
                return panels[name]
            except (KeyError, TypeError):
                return getattr(panels, name)

        reference = None
        if has_field("close"):
            reference = get_field("close")
        else:
            for name in getattr(panels, "fields", panels):
                if has_field(str(name)):
                    reference = get_field(str(name))
                    break
        if reference is None:
            raise ValueError("无法构造过滤掩码：面板没有可用字段")

        # ``mask`` 懒创建：退市剔除恒开启，四项可选开关全关时最终掩码就等于
        # ``~is_delisted``，没必要先铺一个全 True 的同尺寸面板再与一次。
        mask: pd.DataFrame | None = None
        # 退市剔除恒开启，所以至少要构造 is_delisted；其余掩码只在对应开关
        # 打开时才构造，避免为没开的过滤项白算整个状态面板。
        needed = {"is_delisted"}
        if filter_suspended:
            needed.add("is_suspended")
        if filter_st:
            needed.add("is_st")
        if filter_limit_up:
            needed.add("is_limit_up")
        if filter_limit_down:
            needed.add("is_limit_down")
        status_masks = self.build_status_masks(panels, needed=needed)
        status: dict[str, str] = {}
        warnings: list[str] = []

        def apply_status_filter(
            option: bool,
            *,
            status_key: str,
            source_field: str,
            mask_key: str,
        ) -> None:
            """按原始状态字段把不合格标的排除出研究样本。

            ``source_field`` 是真实面板字段，用来判断该数据版本是否提供状态；
            ``mask_key`` 是 ``build_status_masks`` 产出的内存掩码键。两者必须分开
            ——内存掩码不是面板字段，直接拿它做存在性判断会被误判成字段缺失。
            原始状态缺失的单元格按放行处理。
            """

            nonlocal mask
            if not option:
                status[status_key] = "disabled"
                return

            def ensure_mask() -> pd.DataFrame:
                nonlocal mask
                if mask is None:
                    mask = pd.DataFrame(
                        True, index=reference.index, columns=reference.columns
                    )
                return mask

            if not has_field(source_field):
                status[status_key] = "skipped_missing_field"
                warnings.append(
                    f"已跳过{status_key}过滤：当前数据版本缺少 {source_field} 状态字段"
                )
                return
            # 用 reference 的轴而不是 mask：mask 是懒创建的，首次过滤时可能
            # 尚未分配，而两者的 index/columns 本就同源。
            raw_status = _align_to(
                get_field(source_field), reference.index, reference.columns
            )
            excluded = _align_to(
                status_masks[mask_key], reference.index, reference.columns
            ).mask(raw_status.isna())
            missing = int(raw_status.isna().sum().sum())
            if missing:
                status[status_key] = "enabled_partial"
                warnings.append(
                    f"{source_field} 状态覆盖不足（{status_key}）："
                    f"{missing} 个值缺失，按放行处理"
                )
            else:
                status[status_key] = "enabled"
            target = ensure_mask()
            target &= ~excluded.fillna(False).astype(bool)

        def apply_delisted_exclusion() -> None:
            """无条件把退市（不可投资）标的排除出研究样本。

            与 ``apply_status_filter`` 的关键区别：原始状态**缺失时按剔除处理**，
            不做 ``raw_status.isna()`` 放行，也不记为 ``enabled_partial``。退市后
            被前值填充冻结的行情会污染因子，缺失不能等同于放行。

            字段整体缺失时按既有降级口径跳过（生产 release 恒提供该字段，见
            ``data_manifest.yaml``），状态记为 ``skipped_missing_field`` 并留告警。
            单元格级缺失不告警：那都是标的上市前的空档，数量以百万计且是
            预期状态，逐次告警只会淹没真正的信号。
            """

            nonlocal mask
            if not has_field("listed_status"):
                status["is_delisted"] = "skipped_missing_field"
                warnings.append(
                    "已跳过退市剔除：当前数据版本缺少 listed_status 状态字段"
                )
                return
            status["is_delisted"] = "enabled"
            # 掩码与 mask 同源于 reference 的 index/columns，无需再对齐一次。
            # 首个生效的过滤直接取反结果当掩码，省掉一次全尺寸面板分配。
            excluded_delisted = ~status_masks["is_delisted"]
            if mask is None:
                mask = excluded_delisted
            else:
                mask &= excluded_delisted

        apply_status_filter(
            filter_suspended,
            status_key="is_suspended",
            source_field="up_and_down_status",
            mask_key="is_suspended",
        )
        apply_status_filter(
            filter_st,
            status_key="is_st",
            source_field="listed_status",
            mask_key="is_st",
        )
        apply_status_filter(
            filter_limit_up,
            status_key="is_limit_up",
            source_field="up_and_down_status",
            mask_key="is_limit_up",
        )
        apply_status_filter(
            filter_limit_down,
            status_key="is_limit_down",
            source_field="up_and_down_status",
            mask_key="is_limit_down",
        )
        # 退市剔除恒开启，没有开关，放在最后保证不可被任何选项覆盖。
        apply_delisted_exclusion()
        if mask is None:
            # 只可能在没有 listed_status 字段降级、且四项可选开关全关时走到。
            mask = pd.DataFrame(True, index=reference.index, columns=reference.columns)

        return {
            "mask": mask,
            "status": status,
            "warnings": warnings,
            "status_masks": status_masks,
        }

    _instance: "DataEngine | None" = None
    _instance_lock = threading.RLock()
