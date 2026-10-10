# GET /api/data/status 与 POST /api/data/reload

用于查看服务当前使用的行情 release，或手动强制切换到最新 release。两个端点都不修改行情数据文件。

## GET /api/data/status

```http
GET /api/data/status
```

```json
{
  "data_version": "ifind-sync-20260929-093857",
  "release_id": "0f1c8f3a...",
  "data_dir": "/path/to/marketdata"
}
```

只读查询，不改变服务状态：

| 字段 | 含义 |
|---|---|
| `data_version` | 当前 release 清单 `data_manifest.yaml` 中的 `data_version` |
| `release_id` | 服务装载该 release 时按文件大小/修改时间算出的签名；数据文件变化后随之改变 |
| `data_dir` | 当前 release 的物理目录 |

`release_id` 变化即代表服务切换到了新的 release，可用于确认数据更新是否已生效。

## POST /api/data/reload

```http
POST /api/data/reload
```

重新读取并校验 `data_manifest.yaml`，校验通过后原子切换到最新 release，清空行情字段缓存和展示层缓存，返回与 `GET /api/data/status` 相同的结构。

这是运维兜底入口：正常情况下服务会**自动检测**行情发布切换（见下节），无需调用。清单缺失/校验失败或数据目录不可读时返回 `503`，且当前 release 保持不变。

## 自动发布切换

服务在后台按固定间隔（默认 60 秒）重算行情目录的 release 签名，检测到签名变化且连续两次检查一致（说明文件写入已停止）后，自动执行与 `POST /api/data/reload` 相同的校验和切换流程：

- 数据发布完成后约 1~2 分钟自动生效，**不需要重启服务**；
- 写入进行中（签名持续变化）不会切换，避免装载到写了一半的数据；
- 正在运行的回测继续使用其开始时的 release 快照跑完，新请求使用新 release；
- 切换后因子回测结果缓存按新 `release_id` 重新计算，不会复用旧 release 的结果；
- 清单校验失败时记录告警并保持当前 release，等待下一轮检查重试；
- 若配置了 `data.expected_version`，新版本发布后必须同步更新该配置并重启服务，否则自动切换会因版本不匹配而保持旧 release。

## 状态与错误

| HTTP | 含义 |
|---|---|
| `200` | 查询成功或重载成功 |
| `503` | 数据清单缺失/校验失败、数据目录不可读，或 Provider 装载失败 |

当前 API 没有身份认证，两个端点的访问应随服务整体由部署边界限制（见 [API 设计](../../../系统文档/API设计/API设计.md)）。
