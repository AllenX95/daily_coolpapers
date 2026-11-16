# 两阶段分类实施记录

日期：2026-10-10

本轮实现了两阶段分类与方向出清 PRD 的主要产品闭环：研究方向和投资主题共用分类目标视图；Flash 初筛结果与 Pro 精筛结果、人工决定分别留存；有效归类按人工决定、成功精筛、Flash 明确匹配的顺序计算。主题自动入选投影到既有 `paper_investment_themes` 供备忘录等旧入口使用，已有主题关联仅在缺少分类账本行时回填为人工确认。

目录和待分类页面支持按目标、状态及日期查看，人工处理优先于模型任务；已入选项支持单篇或批量移出，恢复时保留最近移出理由、备注和时间。主题手动加入不要求全文评估。候选精筛、已入选项 Pro 复核与主题历史补分类沿用现有任务账本、配置和排重能力。摘要评估按最终入选论文去重触发；没有可用分类目标时沿用原行为。

父代理使用隔离数据库和 mock LLM 完成了浏览器验收：待分类样本 4 个关系、3 篇论文，预览预计 3 次调用并成功创建精筛任务；方向入选、人工待处理、移出与投资主题投影均符合预期。人工确认、研究方向批量移出互不干扰、移出恢复、Pro 排除恢复、Pro 复核任务创建、无全文时手动加入主题及页面菜单布局均通过。投资主题历史补分类预览另以 5 篇隔离样本确认计数为 5 篇、3 篇可执行、2 篇已有成功结果。未对真实论文执行分类，也未启动真实 worker 或 scheduler。

自动化验证：

- `py -m py_compile daily_coolpapers/app.py daily_coolpapers/config.py daily_coolpapers/db.py daily_coolpapers/default_prompts.py daily_coolpapers/form_commands.py daily_coolpapers/job_views.py daily_coolpapers/jobs.py daily_coolpapers/services.py`：通过。
- `py -m unittest tests.test_direction_pipeline.DirectionPipelineTests.test_retry_modes_cannot_classify_new_historical_directions`：通过（Ran 1, OK）。
- `py -m unittest tests.test_classification_refinement tests.test_direction_pipeline tests.test_pending_classifications tests.test_direction_decisions tests.test_direction_backfill tests.test_automatic_abstracts tests.test_investment_themes`：通过（Ran 101 tests in 31.596s, OK）。
- 发布前完整回归 `python -B -m unittest discover -s tests`：通过（Ran 522 tests in 78.651s, OK）。完整回归发现并修复了任务轮询携带 payload 和新增阶段的分组排序问题；并发测试适配新的分类输出格式，备忘录来源删除测试补充分类账本清理。

模型效果的约 40 篇人工核对尚未执行；没有调用付费模型，也没有验证实际模型准确率。使用者应先用小样本核对明确相关、弱关联及同词异义案例，再依据人工意见调整 Prompt 后逐批处理历史候选。
