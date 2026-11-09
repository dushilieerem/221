# 因子值查重（novelty）接口说明

2026-11-09。分支 `codex/factor-novelty-r4`，基于当前 master；对应 RE 评审 4 项 P1 的整改。尚未合入共享 ResearchEngine、未部署共享 9527。

## 接口

新增服务间认证的 `POST/GET/DELETE /api/factor/novelty`。BFF 提供显式、经权限筛选的对照清单；RE 不查询其他用户私有库。计算复用 DataEngine、FactorEngine、Panel 和 ExperimentManager，不另建回测实现。

请求：`candidate` 为现有 FactorBacktestRequest（必须有明确起止日期）；`references` 每项有唯一 `reference_id`、`factor_name`，且只能二选一提供 `library_name` 或 `factor_code`；自定义源码提供 `source_hash` 时校验；`max_window` 声明更长历史依赖。调用方必须提供 `expected_data_release_id`、`expected_candidate_source_hash` 和 `expected_candidate_spec_hash`；最后一个与 RE 实际计算的候选回测 spec 比较，不回填请求值冒充核验。`min_pairs` 默认 30，方法为 `daily_abs_spearman`。

## 计算口径

- 候选使用原研发的同一数据快照、源码、窗口、股票池、预处理和筛选语义。
- 参考因子预热：`reference_warmup_sessions` 允许 252–5000，实际预热取 `max(请求值, reference.max_window)`；请求值与实际值分别记录。历史不足明确报错，不静默缩短窗口。
- 统计量：逐日、同一股票编码对齐；双方共同有限值子集内分别重新排名（并列平均秩），计算 Pearson 相关即当日 Spearman；当日不足 `min_pairs`、常量、全缺失或含无穷值则无定义；先对每日 rho 取绝对值，再在有效日期上等权平均。
- 负号、正比例缩放不改变结果；不把全部日期股票拼成长列，不用收益曲线相关替代。
- 覆盖：既有 `coverage` 分母仍是“有效相关日期上的候选有效股票日数”，这是条件于有效日期的股票覆盖率；新增 `date_coverage = valid_days / trading_days_total` 仅作诊断，不暗改旧硬门槛。
- 逐参考返回：成功、all-NaN、finite 值不足、常量、无配对、无有效截面及错误原因；失败参考保留证据，全部失败时任务置 failed。

## 请求身份与并发

- 路由去重 key 与 meta `request_hash` 都使用 `model_dump(mode='json')`，避免同一逻辑请求产生两个哈希。
- 异步任务支持逐参考进度与协作取消；DELETE 不会立即打断正在计算的单个参考。
- 队列上限、服务认证和任务基础设施复用现有运行时。

## 鉴权

路由依赖应用级 `server.service_token`。共享 9527 部署前必须配置该 token，或明确限制网络边界；否则该路由在 9527 上实际无鉴权。本分支不修改共享服务配置。

## 未做 / 边界

- 不证明全球原创、组合增量收益或 residual IC；不替代人工复核。
- 自定义源码使用窄 AST 白名单，但 rolling/ewm 等窗口没有资源上限，不等同于系统级沙箱。
- 334 参考私有诊断、长时间负载未在本分支复跑；共享部署前需 RE 维护方评估。
