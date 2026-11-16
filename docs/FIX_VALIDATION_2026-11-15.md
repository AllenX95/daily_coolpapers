# 新方向探索修复验收

验收日期为 2026-11-15（北京时间）。代码验证基于 `fc57e5041492c278dce5aa4e202cf10220507757` 上的当前工作树。对应需求见[修复 PRD](PRD_新方向探索缺陷修复.md)，原始复现见[缺陷报告](DEFECT_REPORT_2026-11-15.md)。

## 定向结果

| 缺陷 | 实测结果 | 定向用例 |
|---|---|---|
| F1 时间窗口 | 北京时间 09-30 20:00 排除，10-07 20:00 入样。起点纳入、终点排除，UTC、带 +08:00 和历史无偏移表示一致。覆盖跨年和重复入库，非法时间计入异常。 | `test_legacy_local_timestamps_match_beijing_window`、`test_timezone_boundaries_missing_and_failed_classification`、`test_first_seen_offsets_across_year_and_repeated_ingestion` |
| F2 容量 | 20 篇 × 500 字符、提取 65,536 与汇总 2,600 的组合在 POST 被拒，未创建任务。容量介于两者之间时，按固化顺序裁减且保留完整摘要。worker 复查冻结计划。模拟实际汇总过大时只发出 1 次提取请求并保留消费记录。换用较大汇总配置后创建关联快照。子任务中断前已占用 1 次预算，恢复后只发送 1 次汇总请求。40 篇正常任务发送 5 次请求，每阶段各重试一次的上限为 10 次。 | `test_synthesis_small_context_blocks_before_queue`、`test_capacity_trim_and_frozen_post_replanning`、`test_worker_rechecks_legacy_frozen_capacity_before_extracting`、`test_actual_synthesis_oversize_checkpoint_and_configured_retry`、`test_five_calls_checkpoint_usage_and_no_business_side_effects`、`test_all_stages_retry_once_total_ten_requests` |
| F3 热度 | 明确双零计入同类目同日比较组，未知可进入随机池。单项缺失、解析失败及全组未知时保留未知。0 → 10 → 10 → 缺失的四次观测更新时间，首次入库时间不变。详情显示未知，排序可用，CSV 未知字段为空。旧计数原样留在 `legacy_heat_json`，当前值和采集时间为空，等待下一次真实采集。 | `test_unknown_heat_is_not_zero_and_observation_updates`、`test_heat_counts_zero_failed_and_recapture`、`test_legacy_heat_and_candidate_upgrade_is_repeatable` |
| F4 方向聚合 | 已匹配加缺失、失败或人工确认加缺失均可入边缘池。否决加缺失、possible 加缺失及全缺失计入待分类。验证完整 unmatched、完整 possible、停用方向与无方向计划。 | `test_known_match_survives_missing_direction`、`test_direction_aggregation_matrix`、`test_20_10_10_with_manual_overrides_and_cross_category_dedup`、`test_seven_full_days_and_deterministic_no_direction_split` |
| F5 候选身份 | 重排保留 Alpha 的 ID 与观察状态。模拟原任务在部分周报提交后、任务状态更新前中断。恢复只发送 1 次失败批次及 1 次汇总，共 5 次请求。恢复期间经表单提交的人工判断保留；同名不同范围获新身份。旧候选链接展示原内容，旧表单返回 409。明确引用后改名保留原 ID 和旧版本。验证非法引用整次回滚和重复保存幂等。 | `test_candidate_identity_survives_reordering`、`test_queue_partial_then_interrupted_recovery_keeps_old_links`、`test_continuation_versions_and_invalid_refs_are_atomic`、`test_human_states_prefill_atomic_add_and_stale_update` |

模型测试在临时库中走生产 HTTP 客户端并通过 `httpx.MockTransport` 计数。上述中断用例在事务提交后的指定断点模拟中断，随后调用启动恢复逻辑并使用同一任务入队入口，未执行进程级强制终止。热度和身份升级夹具重复初始化两次，完整性检查为 `ok`，外键错误为 0，既有 Profile 和旧周报快照保留。

## 浏览器

- 隔离实例内 45 篇合格、40 篇普通选样上限。汇总上下文 4,000 时实际计划为 10 篇、因容量跳过 30 篇。经预览生成、查看任务详情、观察、编辑范围并加入一个关注方向，模拟 HTTP 请求总计 2 次。见[窄屏周报](../output/playwright/report-mobile.png)。
- 汇总上下文 2,600 时预览显示容量原因、空输入固定开销及零篇，生成按钮禁用。390 px 视口的内容宽度为 390 px。见[桌面容量提示](../output/playwright/capacity-blocked-desktop.png)和[窄屏容量提示](../output/playwright/capacity-blocked-mobile.png)。
- 旧候选网址在替换后继续打开原 Alpha，显示观察状态、原证据和处理历史。该浏览器场景使用已保存的替换前后夹具；上述队列用例覆盖中断与恢复过程。见[历史候选](../output/playwright/historical-candidate-card.png)和[窄屏历史候选](../output/playwright/historical-candidate-mobile.png)。
- 两个标签页持有同一版本的表单。第一个标签页保存观察后，第二个标签页提交返回 409，并提示刷新周报。独立详情验证点击计数全缺失时显示“未知”。见[旧表单冲突](../output/playwright/stale-form-conflict.png)和[论文未知热度](../output/playwright/unknown-heat.png)。

## 回归命令

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_exploration
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
git diff --check
```

最终全量回归执行 528 项，527 通过、1 跳过，耗时 98.035 秒。跳过的是 S6 的 S4 兼容回退演练，环境缺少与基线匹配的 `db_before.py`，可通过 `S6_S4_DB_PATH` 指定该夹具。本轮热度与候选模拟旧格式迁移已单独通过。工作树的 `git diff --check` 通过。

### 合并两阶段分类后的复验

将探索修复与 `origin/main` 的两阶段分类（`9e73fe7`）合并后，复验 `test_exploration_respects_refinement_and_manual_removal` 通过。用例覆盖精筛确认、排除、低置信度、人工移出与精筛失败后沿用初筛匹配的选样资格。全量 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v` 执行 540 项，539 通过、1 跳过，耗时 92.736 秒。S6 跳过原因仍是缺少匹配 S4 的回退夹具。

## 部署备份和回退

1. 正式发布前停止应用及调度，确认工作进程退出。使用 SQLite Backup API 为主库和 Profile 库各生成一份一致备份，并备份当前 Flask 密钥和本地配置。对备份执行 `PRAGMA integrity_check`、`PRAGMA foreign_key_check`，记录配套代码版本。
2. 用迁移后的代码对与备份一致的隔离副本执行两次初始化，验证历史周报、关注方向、Prompt 和 Profile。在正式库上运行迁移前保留该备份，启动后复核业务行数、迁移记录、完整性和外键检查。
3. 回退时先停止应用，保留新库用于诊断，再一起恢复与旧代码对应的数据库和密钥备份。旧代码直接打开新库会绕过新候选身份和采集时间契约。恢复后先做隔离启动和完整性检查，再恢复调度。

## 验收边界

本轮验证使用临时库和模拟 Provider。正式库、应用设置与实际模型连接均未改动。真实 Provider 小样本、实际 token 与费用、全量采集持续运行和 E3 四周试用仍属运行验收。首次入库历史格式通过逐行时间解析兼容，预览仍需检查有类目记录的论文时间。全量采集后的大规模性能另行验证。既有无偏移时间按项目旧北京时间契约解析，外部来源须先明确时区。
