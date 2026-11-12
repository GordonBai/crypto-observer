# Crypto Observer

Base 早期代币的可追溯观察与前瞻研究基础设施。Python 3.11+，运行无需第三方依赖。

## 快速运行

```bash
python3 -m crypto_observer collect --pages 2
python3 -m crypto_observer cycle
python3 -m crypto_observer audit --output reports/audit.md
python3 -m unittest discover -s tests -v
```

默认数据库 `data/observer.sqlite3`。全局 `--db PATH` 放在子命令之前。
`collect` 通过 GeckoTerminal 发现 Base 新池，按合约/池地址用 DEX Screener 刷新。
`--pages` 为 1–10，`--refresh-limit` 控制轮转预算；分页满额会保留覆盖警告。
`loop --cycles 4 --interval 300` 运行有限轮次，进程退出后不会继续采集。

## 研究模块

```bash
python3 -m crypto_observer scan --limit 20
python3 -m crypto_observer probe --limit 3
python3 -m crypto_observer holder-index --limit 1 --chunks 4
python3 -m crypto_observer holder-report --output reports/holders.md
python3 -m crypto_observer cases --output reports/cases.md
python3 -m crypto_observer review-quotes --limit 20
python3 -m crypto_observer paper-open --limit 2
python3 -m crypto_observer paper-evaluate
python3 -m crypto_observer paper-report --output reports/paper.md
python3 -m crypto_observer evaluate-report --output reports/evaluation.md
```

- `scan`：GoPlus 风险字段与持有人元数据，未知不视为通过，限流停止。
- `probe`：仅允许只读 RPC，固定区块读取代码、EIP-1967 槽与 owner；不是权限完整性审计。
- `holder-index`：从 archive code 推导的部署区块开始，按 Transfer 事件重建余额；保存断点并交叉核对 Base RPC / PublicNode 区块 hash。
- 持有人推导要求总余额与链上 totalSupply 一致、无负余额；新增前三名 balanceOf 抽查。不支持由此证明所有地址余额正确、实际控制人身份或非标准代币安全。
- `review-quotes`：固定首批 20 个观察合约的独立金额报价检查，与策略账本隔离。
- `paper-*`：固定规则、匹配对照和 7/30/90 天只读报价实验；不构建、签名或广播交易。

## 持续采集与审计

```bash
python3 scripts/manage_service.py install --interval 900
python3 scripts/manage_service.py status
python3 scripts/manage_service.py stop
```

macOS 用户级 launchd 服务 `com.gordonbai.crypto-observer` 每 15 分钟触发一轮 `cycle`。
一轮依次执行采集、到期/每日估值、固定选样、风险扫描、新持有人索引、历史刷新、独立权限核查和报告。
数据库锁防止重叠运行。电脑休眠或未登录时不保证采集，覆盖审计必须保留缺口。

- `reports/health.json`：当前步骤、完成时间、错误、磁盘余量与数据库检查。
- `reports/cycles.jsonl`：已完成周期证据；审计与数据库中的实际采集 run 对照。
- `reports/audit.md`：初审证据引用、成熟期限、覆盖和未完成要求。
- `data/service.stdout.log` / `service.stderr.log`：本地服务输出。

当前线程另设每天 09:00 的应用内跟进（automation id `crypto`），检查服务、数据和到期样本。
它与采集服务不同，运行需桌面应用和电脑可用；不替代底层采集。

## 实验与数据口径

v1 保留首批 6 个诊断模拟持仓。v2 独立冻结每日选样、无放回匹配对照、15 分钟入场窗口、
24 小时评估宽限与预声明的第 15 天时间切分。实验开始 30 天后停止新增样本，继续跟踪原样本；
到第 121 天停止网络采集，保留服务注册与健康状态。

100 USDC 是模拟报价金额。每个版本/策略/持仓期限分别构成一个模拟 $10,000 账户，
入场扣除名义金额和来源估计 Gas。每日估值缺失不沿用旧价格；未知归零仅为压力场景。
组合回撤是每日报价采样回撤，可能漏掉盘中低点。资本超限会单独标记。

**两次独立报价没有应用第一次买入对池储备的影响，不能称为已验证的顺序往返成本。**
薄池、不同路由、税费、Gas、MEV 和市场变化都可能使报价与实际成交不同。报价实验不是实际收益记录。

所有原始响应、请求时间、来源与 SHA256 保留在 SQLite；规范化快照通过 request_id 回溯。
核心 schema / normalizer 现为 2。HTTP 物理重试自 v2 起逐次记录在 request_attempts，旧历史不补造。
新池不等于新项目，来源未验证的流通市值不能用 FDV 填充。失踪不自动认定归零或 rug。
数据与 raw payload 随时间增长，不自动删除失败样本。

## 当前证据与边界

计划与开发记录：[PLAN.md](PLAN.md)、[DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md)。
首批 20 案例初审：[research/initial_review_20261024.md](research/initial_review_20261024.md)。
冻结证据、检索记录与决定分别位于同目录 JSON 文件，通过 quote_id/request_id 回溯数据库。

M1–M5 已有测试和实测证据。M6/M7 工程实现已运行，但完整验收还要求真实成熟样本、
连续运行审计、持出期比较和明确的有效性结论。产品身份、流通量、关联所有人及实际可卖出性仍可能未知。
数据覆盖是供应商子集，不宣称覆盖全链；测试通过或报价成功不证明盈利能力。

观察期限后的收尾：第 121 天后不再采集网络数据，仍会在离线保护下把错过的评估窗口
记为 unknown/missed_window 并生成最终状态报告。相关时间边界仅在临时测试数据库中模拟。

## 短窗观察数据层

新研究笔记触发独立数据优化，不改 v2 实验。新采集自动保存来源提供的短窗成交额、
正负涨跌、交易笔数和部分独立交易者计数到 snapshot_windows（feature_version=1）。
原始数据仍通过 snapshot_id/request_id 回溯；旧记录不自动回填。

```bash
python3 -m crypto_observer windows-report --output reports/windows.md
```

cycle 会更新 windows.md，按来源、池地址计算实际观测间隔。来源市场时间未知不能以本地
接收时间代替。滚动窗口不是已收盘 K 线；不能用 h24 差分生成小时成交额，笔数不能当净资金流。
下一步设计见 research/pipeline_optimization_20261025.md。

## 独立小时行情与短期事件实验

优化后的支线使用 Bitget 同源公开行情；无需密钥。正式 market-v2 在首次读取时冻结合格
按来源分类排除股票/RWA的加密现货/永续库存，再按hash固定选择24现货+12永续，保持预算明确；并非全市场实时覆盖。
原 pilot 保留在 data/short_term.sqlite3；正式支线默认 data/short_term_v2.sqlite3。
Base 原实验仍使用原数据库，不改变v2规则。

```bash
python3 -m crypto_observer short-cycle --market-limit 36
python3 -m crypto_observer market-report --output reports/short_term/market.md
python3 -m crypto_observer short-report --output reports/short_term/experiment.md
python3 scripts/manage_short_service.py install --interval 300
python3 scripts/manage_short_service.py status
python3 scripts/manage_short_service.py stop
```

每5分钟服务独立触发 short-cycle，写入 reports/short_term/ 下的 health、cycles、market、
experiment 和 audit。运行电脑仍需开机联网；互斥锁避免同数据库重叠执行，不以定时配置代替实际覆盖。
小时K线按UTC边界、源时钟和接收时间核验，成交额单位USDT不冒充USD。
short-v1 以此前24个连续小时为基线，分别观察量、价、联合信号及事前匹配控组。
先7天开发，合格数据不足不启动持出信号；校准后阈值一次冻结。1h/4h/24h报价期限与10分钟
宽限独立于Base实验；中断和错过窗口保留unknown，不回填后来价格。
现货仅作展示盘口容量报价，不下单；永续OI/数量单位未确认，不用于合约模拟执行。
详见 research/short_term_implementation_20261026.md。


## 运行质量与研究审计升级

新增可选 `--mode critical|maintenance`、`--network-budget-seconds`、`--request-budget`，
以及独立 `short-quotes` 报价命令。默认 full 服务流程及冻结实验配置保持不变；新入口不会自动注册后台任务。
预算限制新逻辑请求的准入，不强制取消在途请求；报价 worker 不做新选样，不改变冻结持仓顺序。

只读诊断：`python3 -m crypto_observer.research_quality --output reports/research_quality.json`。
报价审计：`python3 -m crypto_observer.quote_audit --db data/observer.sqlite3 --output reports/quote_audit_base.json`。
详见 [工程升级说明](research/engineering_upgrade_20261111.md)、[报价审计](docs/QUOTE_AUDIT.md) 和 [TODO](TODO.md)。
