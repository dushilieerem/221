# GET /api/market-data/daily/{symbol}

返回当前已发布市场数据中指定股票的完整日线 OHLC，供 WebApp 渲染日线图。

```http
GET /api/market-data/daily/000001.SZ?start=2024-01-01&end=2024-06-30
```

| 参数 | 位置 | 类型 | 必填 | 说明 |
|------|------|------|------|------|
| `symbol` | 路径 | string | 是 | 交易所代码，例如 `000001.SZ`；服务向后兼容六位代码。 |
| `start` | 查询参数 | string | 否 | 起始日期，`YYYY-MM-DD`，包含该日。 |
| `end` | 查询参数 | string | 否 | 结束日期，`YYYY-MM-DD`，包含该日。 |

也可只传一个日期边界；参数不放在请求体中。未传日期时返回全部可用区间。

```json
{
  "symbol": "000001.SZ",
  "name": "平安银行",
  "bars": [
    {
      "date": "2024-01-02",
      "open": 10.1,
      "high": 10.5,
      "low": 10.0,
      "close": 10.3,
      "is_show": true
    }
  ],
  "date_range": {"start": "2024-01-02", "end": "2024-06-28"}
}
```

| `bars` 字段 | 类型 | 说明 |
|------|------|------|
| `date` | string | 交易日，`YYYY-MM-DD`。 |
| `open` / `high` / `low` / `close` | number | 未复权 OHLC。 |
| `is_show` | boolean | 当日成交量大于 0 为 `true`，成交量为 0 或缺失为 `false`。 |

只返回 OHLC 均完整的交易日。`is_show` 只判断当日是否有成交：读取同一 release 研究行情面板 `market_data_hfq.parquet` 的 `volume`，`volume > 0` 为 `true`，`volume == 0` 或缺失为 `false`。未复权行情在停牌和退市后仍会按前值填充，不能凭价格是否存在判断交易日；因此停牌日若仍有一部分成交会返回 `true`，退市整理期有成交也返回 `true`，退市后价格虽被冻结但成交量为空，返回 `false`。

代码不存在或区间内无完整日线返回 `404`；倒置日期返回 `422`；数据目录、证券主数据、名称映射、任一 OHLC 字段或成交量字段不可读返回 `503`。本端点不提供实时行情、复权切换、成交量明细或技术指标。
