# 开发文档索引

所有开发需求、设计评估、性能基线及验收报告统一放在本目录。根目录中英文 README 保留为安装和使用入口；目录级 AGENTS.md（若存在）保留在其适用位置。

## 当前开发方案

- [全量采集与梯次存储优化](PRD_全量采集与梯次存储优化.md)：元数据和原始摘要长期保留；普通 PDF／Markdown 7／30 天，核心 30／180 天；T0–T4 开发及隔离验收见下方报告。

## 产品需求与设计

- [基础 PRD](PRD.md)
- [投资业务优化](PRD_投资业务优化.md)
- [架构稳定性与性能优化](PRD_架构稳定性与性能优化.md)
- [代码质量与设计评估](代码质量与设计评估.md)

## 性能与验收证据

- [全量采集与梯次缓存实施验收](TIERED_STORAGE_RELEASE_REPORT.md)
- [梯次存储容量与清理基准](TIERED_STORAGE_BENCHMARK.json)

- [架构性能基线](ARCHITECTURE_BASELINE.md)
- [S3 分页性能基线](S3_BASELINE.md)
- [S4 模型调用记录开销](S4_BASELINE.md)
- [架构发布与验收说明](ARCHITECTURE_RELEASE_NOTES.md)
- [S6 隔离恢复演练](S6_RECOVERY_REPORT.md)

历史报告保留原测量环境、源码哈希和命令；移动目录不表示重新测量。历史路径可能反映生成报告时的位置，当前目录以本索引为准。

## 新报告输出

从仓库根运行，基准工具默认将报告写入本目录。正式已有基线不要覆盖；重测使用独立文件名，例如：

```powershell
python -B -m tests.benchmark_architecture --sizes 100 --warmup 1 --samples 1 --report doc/ARCHITECTURE_BASELINE_recheck.md
python -B -m tests.benchmark_s3 --sizes 10000 100000 --warmup 5 --samples 30 --report doc/S3_BASELINE_recheck.md
python -B -m tests.benchmark_llm_calls --report doc/S4_BASELINE_recheck.md
python -B tests/benchmark_tiered_storage.py --sizes 10000 100000 --output doc/TIERED_STORAGE_BENCHMARK_recheck.json
```

上述命令为使用示例，不表示本轮已执行性能重测。临时测试材料与运行数据仍按各自目录管理，不纳入开发文档。
