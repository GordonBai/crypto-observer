# 2026-11-11 工程升级与验收边界

本次升级保持 Base v2 / market-v2 / short-v1 配置、历史账本与现有 launchd 注册不变。
原服务继续默认 full 模式；新工具以独立输出或显式参数运行。未来阶段切换必须记录时间与代码版本，
单独评价覆盖，不能将旧缺口回填为成功。

## 运行与审计工具

- `cycle --mode critical` / `short-cycle --mode critical`：省去报告及慢维护步骤，保持原信号与报价规则。
- `cycle --mode maintenance` / `short-cycle --mode maintenance`：单独维护/报告路径；不采集新市场数据或选样。
- 默认 `full` 与原服务兼容；非默认模式在各自子目录输出，不覆盖主服务健康记录。
- 步骤耗时与包内源码 SHA256 写入健康记录，区分运行实现变化与研究版本。
- 同一数据库仍采用原互斥锁。拆分模式不能绕过互斥，也不等于已经部署并证明调度改善。

只读诊断命令（不重启服务、不修改数据库）：

```bash
python3 -m crypto_observer.research_quality --output reports/research_quality.json
python3 -m crypto_observer.quote_audit --db data/observer.sqlite3 --output reports/quote_audit_base.json
python3 -m crypto_observer.quote_audit --db data/short_term_v2.sqlite3 --output reports/short_term/quote_audit.json
python3 -m crypto_observer.quote_audit --book normalized_spot_book.json --output reports/book_sensitivity.json
```

`normalized_spot_book.json` 应提供真实留存的、带身份/单位/时间字段的现货盘口。
敏感性计算仅改变名义金额、保留深度比例、费用及同一盘口的时间年龄；不模拟未来盘口或真实成交。
SHA 验证只能证明本地原始字节与已存摘要一致，不能证明供应商数据真实或代币可执行交易。

## 后续实验预注册

`python3 -m crypto_observer.study_plan spec.json --output future-study.json` 创建不可覆盖的独立计划。
拒绝现有冻结版本、过去或无时区的开始时间、重复资产和超过声明容量的集合。
要求 hypothesis、primary_horizon_hours、benchmark、cost_model、matching_policy、unknown_policy、
analysis_plan、decision_rules、start_at、end_at、instruments 和 capacity 等字段。
capacity 包括 budget、cadence_seconds、maximum_age_seconds；应填实测周期，不以配置间隔冒充。
它只冻结计划并保存摘要，不启动实验，也不自动纳入资产或交易。

## 仍需实际观察或独立研究的事项

- 连续 7 天及时覆盖 ≥99%、调度错过 ≤1% 是拟议验收目标，当前没有达标证据。
- 持续在线设备的实际部署、容量和恢复演练需运行验证；现有本机休眠条件未被代码自动解决。
- Base 极端案例的身份、精度、路由、限制和储备变化仍须逐笔解释，本地 AMM 状态模拟未实施。
- 新控组集合、匹配容差、持有人持续跟踪集合需先冻结新版本，再从未来开始采样；不替换旧实验。
- 相关样本的不确定性、时间依赖与统计检验能力需随样本成熟评估，不因有统计代码而视为有效性通过。
- 原实验的真实 30/90 天结果及全样本收尾继续按 PLAN.md 等待，不提前完成。

本地测试命令：`python3 -m unittest discover -s tests -v`。
GitHub CI 运行离线测试，数据库、健康报告、日志及私密配置不上传。
