# 独立交易所数据源实测与单位约束

纽约日期：2026-10-25；采集证据 UTC 日期：2026-10-26。

## 数据源决策

OKX 公共 instruments 接口本环境返回 HTTP 403；Bybit instruments 同样返回 403。
Binance.US 与 Kraken 公共接口可达，但本模块没有将它们的数据拼接到 Bitget 实验中。
实测 Bitget 同时提供无需账户或密钥的现货、USDT 永续公开行情，选为独立市场观察的单一 venue。
以上仅是公开读取的技术可用性，不表示用户可在该交易所开户或交易。

官方资料：
- [Bitget Market Data](https://www.bitget.com/docs/catalog/market/market-data)：v3 instruments、history-candles、orderbook 及价格/数量字段。
- [Bitget Classic Futures Market](https://www.bitget.com/docs/catalog/classic-contract-market/classic-contract-market)：v2 open-interest、current-fund-rate、symbol-price。

当前文档与实际响应交叉检查后，仅使用明确的原始字段。小时数据第 6/7 项分别是基础币量与报价币成交额；USDT 报价成交额不冒充 USD。订单簿以实际价格层计算价差；现货数量按基础币处理。永续订单簿数量与 OI size 的单位仍明确标为未验证，不据此换算美元、张数或用于模拟合约成交。

## 冻结品种与覆盖

首次成功同时读取 SPOT 与 USDT-FUTURES 两份 v3 instruments 清单后冻结：

- 入选库存要求 category 匹配、status=online、quoteCoin=USDT、instrumentType=crypto 且 symbolType=crypto；排除 isReality=yes、isRwa=YES 与带 ISIN 的品种；永续额外要求 type=perpetual。
- 保存完整符合上述条件的 962 个品种库存和原始规格，不按六币案例、涨幅或后续表现筛选。
- 根据 sha256(venue:kind:instrument) 排序，独立选择 24 个现货和 12 个永续为受预算约束的观察集合。
- 新上市资产不自动加入 market-v2，失败、缺失或下架样本不从原库存删除。库存的 online 只代表冻结时点状态；launchTime 为 0、空或非法时保留 unknown，不能解释为 1970 年上市。
- 默认 limit=6 是轮转抓取；生产短线循环应使用 limit=36 并审计每个品种的实际间隔。1,072 个库存不代表持续覆盖全部市场。

## 时间、闭合与可用性

只接纳 UTC 小时边界对齐、OHLC 合理、数值有限且非负的 7 字段小时数据。
收盘必须早于交易所 requestTime 和本地接收时间两者的较早者至少 5 秒；两个时钟差超过 120 秒时不接纳该响应。
Bitget 响应没有独立 confirm 标志，因此 closure_policy 明确记为按时间边界闭合，不能声称交易所保证终版。

每根闭合小时 K 线只保留首次接收的原始版本。后续数值改变计入 revisions，响应仍归档，不重写旧信号可见的数据。
received_at 是实际本地接收时间，不能写成历史 close_at；closed_bars(asof) 同时检查接收时间和收盘时间。
历史数据可供首次观察后的基线使用，不能用它制造过去可行动的事件。
缺失的小时不插值、不填零，连续 24 小时检查由事件模块进一步执行。

订单簿必须双边非空、价格有序、数量为正且不锁定/交叉。来源时间超过 30 秒或领先本地 5 秒以上时记 quote_unknown。
报价仅代表此时展示的公开深度，未证明真实订单成交、排队优先级或随后价格路径。

## 永续诊断

保存当前 OI 原始 size、资金费率/周期/下一结算时刻、市场价/标记价/指数价和各自来源时间。
OI 单位未证实，保留 provider_size_unit_unverified，不与其他单位比较、不转美元。
资金费率与价格请求不在同一原子时点，分别留 request_ids/source_times；不宣称历史 OI 覆盖。
basis_fraction 为同来源 market/index 的差异，只是诊断，不代表跨市场可成交套利价差。
模拟执行仅支持 spot，swap 的 execution_supported=False。

## 实测证据

正式独立数据库：data/short_term_v2.sqlite3。run_id=1；market_universes.version=market-v2。
库存 SHA256：975a90edb2550af92cb85c7c5a038acac5692caf63714d22d4b2f1765c30fa6f。

首次 limit=36 实测耗时 43.58 秒：

- 962 个库存、36 个观察品种；
- 1,152 根小时 K 线，每品种 32 根；
- 110 个完整原始 HTTP 请求响应；
- 36 份有效双边订单簿、12 份永续诊断；
- 外键异常 0，首次采集顶层错误说明为空。

逐品种结果见 market_data_live_v2_20261026.json；按源追溯 requests.url/body/sha256。
这仅证明采集、单位保留、时点约束和归档链路可运行，不能证明事件信号有效。

## 版本纠正与历史保留

首次 pilot 数据库 data/short_term.sqlite3（market-v1，库存 1,072）仅按 instrumentType=crypto 过滤。复核发现部分 tokenized-stock 的该字段也为 crypto，但 symbolType=stock、isReality=yes；另有 launchTime=0 被解释成 1970。
因此没有修改旧冻结库存或删除旧原始请求，而是使用显式新版本 market-v2 和全新独立数据库。原 pilot 与 market_data_live_20261026.json 保留为开发诊断，不当作正式短线实验样本。
market-v2 拒绝直接打开旧 cohort 作继续冻结，提示使用新数据库。正式样本要求双字段 crypto 并排除 Reality/RWA/ISIN；0 上市时间保持 unknown。
正式实测确认 selected-stock=0、launch-1970=0。11 项模块测试验证上述实际分类问题、未知上市时间、版本拒绝、K 线/订单簿与 raw 证据约束。

订单簿统一放在每轮采集末尾读取，避免早期订单簿在处理其他 K 线和永续指标期间超过事件匹配的新鲜度窗口。请求数量不增加。
