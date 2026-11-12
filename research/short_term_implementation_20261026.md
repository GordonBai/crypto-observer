# 短窗研究链路优化实施与验收

纽约日期：2026-10-25。UTC 证据日期：2026-10-26。

## 本次实现

| 模块 | 已实现行为 | 证据 / 局限 |
|---|---|---|
| Base 覆盖与容量 | 全部保留池作分母，单来源覆盖、未观测/停更、时间间隔 p50/p95、轮转容量 | 2721 池的当时轮转估算约 2.97h，不能支撑短时事件；数字会随池数变化 |
| 同源小时数据 | Bitget market-v2 固定宇宙、UTC 非重叠小时 OHLCV、原始报价币成交额、首次接收时点 | 有时钟与闭合宽限校验，但来源没有独立 confirm 标志；修订留存原值和新原始响应 |
| 独立事件账本 | short-v1，先前 24 小时成交额中位数、价格变化、联合信号、小时选样冻结、4h 冷却 | 不从滚动 h24 差分制造小时数据；历史只用于收到之后的基线，不补造历史信号 |
| 匹配对照 | 同小时/来源/报价币/年龄/成交额/历史波动/事前展示深度与价差分层，无放回选控组 | 深度或年龄缺失时保留 unmatched，失败样本不删除；严格分层下控组可能稀少 |
| 短期模拟 | 60s 最低延迟、10min 入场窗口、1h/4h/24h 期限、各10min退出宽限 | 按实际盘口容量消耗买卖侧档位；仍是展示报价，不是成交或真实利润 |
| 成本与路径 | 每侧0/10/30bps费用假设、未知率、收益中位数/尾部/去掉最大赢家、配对比较、采样MFE/MAE | 不声称账户实际费率、盘中真实极值、止损成交或独立样本 |
| 开发与验证 | 开发期7天，阈值仅由开发期合格数据一次校准，之后冻结 | 至少100个合格品种小时且4品种；不足则禁止持出期信号，不事后补选 |
| 中断恢复 | 冻结选择、事件和控组关系恢复；超时入场/退出终态未知；截止后离线收尾 | 原始触发时点和期限不重置，不能把重启后的行情当先前可行动报价 |
| 永续诊断 | 当前OI、资金费率与周期、market/mark/index及basis，分别记录来源时点 | OI及永续盘口数量单位未确认，禁止换算或模拟永续成交；没有历史OI覆盖证明 |
| 持续运行 | 独立5min调度、独立数据库/锁/健康与周期报告 | 本地电脑仍可能休眠或离线；不是全天候主机保证 |

## 正式数据宇宙与 pilot 更正

试采 market-v1 仅按 instrumentType 筛选，复核发现4个观察现货实际 symbolType=stock，部分 launchTime=0 也被错误解释为1970。
旧 data/short_term.sqlite3 和 research/market_data_live_20261026.json 完整保留为 pilot，不提升为正式实验。

正式 market-v2 在全新 data/short_term_v2.sqlite3 中冻结，额外核对 symbolType、Reality/RWA/ISIN，并将非正上市时间保留 unknown。分类依据来源字段，不是独立资产性质或发行主体认证。
冻结库存 962 个（499现货、463永续），按固定hash规则观察24现货、12永续；不是挑选六币赢家，也不宣称覆盖全部市场。
冻结后新上市不自动加入，失败/失踪不从原分母移除。正式库存hash见 market_data_live_v2_20261026.json。

正式数据首轮：1152根小时K线、110个完整原始响应、36份有效双边盘口、12份永续诊断，耗时43.58秒，外键异常0，选中股票类0、1970上市0。
订单簿集中在每轮末尾读取，避免前置数据请求让用于匹配的盘口超过30s有效期。

## 实验口径

开发期预声明探索阈值：相对量3倍、小时价格变化3%、当前小时成交额至少10000 USDT。
这些不是已经优化或证实盈利的阈值。第7天仅从边界之前收到并完成的开发期特征计算90分位，设置量能1.5倍和价格1%下界；阈值与数据ID一次冻结。
持出期不能用后来的数据补校准，数据不足则明确 insufficient。

每小时每策略最多选择一个处理样本，规则为volume / price / combined及其匹配随机对照。每笔假设100 USDT，最多48个待跟踪开放样本。
观察集合、开发数据和后续持出期均有时间/版本隔离，v2 Base原实验不改规则、不混账本。

## 运行与检查

```bash
python3 -m crypto_observer short-cycle --output-dir reports/short_term --market-limit 36
python3 -m crypto_observer market-report --output reports/short_term/market.md
python3 -m crypto_observer short-report --output reports/short_term/experiment.md
python3 scripts/manage_short_service.py status
python3 -m unittest discover -s tests
```

这些新增CLI默认使用 data/short_term_v2.sqlite3；Base 原CLI仍默认 data/observer.sqlite3。
独立服务标签 com.gordonbai.crypto-short-observer，每300秒触发，周期实际耗时会影响起点间隔；保留 com.gordonbai.crypto-observer 原服务。

检查 reports/short_term/health.json、cycles.jsonl、market.md、experiment.md 和 audit.md。
报告中的未知、未到期、错过窗口和未匹配不转换成0收益/0失败率。
实际部署验证和最后测试数量记录于 DEVELOPMENT_LOG.md，以免把本文件中的实现说明当作运行成功证据。

## 仍需真实时间完成的研究验收

- 多轮单品种实际间隔、失踪和采集缺口；本机离线不能靠代码证明连续覆盖。
- 自然发生的新信号及其1h/4h/24h到期报价；没有信号时不降低阈值造样本。
- 7天开发结束后的校准与独立持出结果，含控组、未知和成本敏感性。
- OI/永续数量单位进一步证实，若要研究实际合约执行还需要独立的新版本。

本次工程与现场采集验收不等于短线策略有效性验收，更不等于真实可成交收益。

官方来源与详细字段约束：market_data_sources_20261026.md；原笔记：market_anomaly_lessons_20261025.md。

## 本次整合现场结果

short-v1 实验于 2026-10-26 02:57:42 UTC 开始。完成3个完整周期，其中2个由独立launchd执行，退出均正常。
03:00 UTC闭合小时产生24个合格现货评估、72条规则事件；volume触发2次、预算选择1个APTUSDT，另一个记cohort_frozen。
选择后没有合格同分层控组，保持unmatched。
第二次后台周期按原窗口于03:07:03 UTC取得模拟入场报价，保存1个持仓和首个路径估值，没有重复事件。
当时1188根小时K线、435个完整原始响应；全部SHA一致、外键异常0。详细证据见 short_integration_live_20261026.json。
94项离线测试通过，涵盖基线/可用时点/类型与单位/匹配/冻结/跨日恢复/延迟入场与错过退出等。
首个1h期限在04:07 UTC后到期，尚无期限结果；实现与现场数据链路验收不替代有效性判断。
