# ResearchEngine

ResearchEngine 是独立的量化投研服务。它通过 HTTP API 为仍位于 SVQuant
仓库中的 WebApp 提供因子/策略回测和市场数据查询能力；WebApp 不导入本项目
的 Python 包，也不读取本项目的本地资产。

本仓库采用从 SVQuant 当前版本抽取的独立历史。两个仓库当前仍按锁步方式发布，
API 契约和契约测试由 ResearchEngine 开发人员维护。

## 目录

```text
app/                 # FastAPI 应用及回测运行时
app/assets/          # 因子 SQLite 库、公共算子和策略资产
scripts/              # 后台启动/停止/状态脚本
tests/                # 单元测试和 API 契约测试
docs/                 # ResearchEngine 设计与功能文档
config.example.yaml   # 配置模板（不含真实部署配置）
environment.yml       # Conda 环境定义（Python 3.11）
pyproject.toml        # Python 包和依赖定义
```

## 人工部署

不提供 `install.sh` 或自动部署脚本。首次部署由人工执行：

```bash
conda env create -f environment.yml
conda activate researchengine
python -m pip install -e ".[all,dev]"
cp config.example.yaml config.yaml
```

然后编辑根目录 `config.yaml`：

- `data.dir` 指向外部只读行情目录；行情目录不属于本仓库，也不要复制到仓库内。
- `data_manifest.yaml` 必须位于该目录，并包含 `schema_version`、`data_version`、资产路径和
  SHA-256 声明。
- `data.expected_version` 必须与清单中的 `data_version` 一致。切换数据发布时，先人工确认
  清单，再更新这一项；需要逐文件完整性校验时将 `data.verify_hashes` 改为 `true`。
- `config.yaml`、日志、PID、缓存和回测输出均不会提交到 Git。

启动前必须激活 `researchengine` 环境：

```bash
./scripts/research-engine.sh start
./scripts/research-engine.sh status
./scripts/research-engine.sh logs --no-follow
```

前台调试：

```bash
conda activate researchengine
python -m app.main
```

服务启动时会依次加载配置、校验外部数据目录和清单、校验数据版本，然后装载只读
Parquet provider。任一步失败，服务都不会进入可用状态。

默认健康检查地址为 `http://127.0.0.1:9527/api/health`。当前部署约束为单实例、单
Worker；后台脚本的默认日志为 `logs/app.log`，PID 文件为 `run/app.pid`。

## API 与批量回测

主要接口：

```text
POST /api/backtest/factor
POST /api/backtest/strategy
POST /api/internal/factor-backtest-batches
GET  /api/factor-backtest-batch-tasks/{task_id}
GET  /api/factor-backtest-batches
GET  /api/factor-libraries
GET  /api/strategies
```

批量 CLI 只调用运行中的 HTTP 服务：

```bash
python -m app.cli.factor_backtest_batch run --as-of YYYY-MM-DD
python -m app.cli.factor_backtest_batch run --as-of YYYY-MM-DD --wait
```

AI 适配层需要的访问密钥仍作为敏感凭据由对应环境变量提供；密钥不写入
`config.yaml`，也不提交到仓库。

## 开发与测试

```bash
conda activate researchengine
pytest -q
```

若只使用已有的兼容开发环境，也可在 `svquant` 环境中运行测试；正式服务使用
`researchengine` 环境和 Python 3.11。
