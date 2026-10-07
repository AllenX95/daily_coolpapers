# S6 隔离备份与回退演练

日期：2026-10-01

## 结果

仅在临时合成库完成单项演练。当前 S5 初始化重复执行后，主库 schema、迁移标记和样本记录，以及 Profile 库 schema 与记录均未变化。主库和 Profile 库均通过 `sqlite3.Connection.backup()` 备份及恢复；恢复后的两库 `PRAGMA integrity_check` 均为 `ok`，恢复的合成 Fernet 密钥能解密合成 Profile 值。

回退验证将完整 `daily_coolpapers/` 包复制到临时目录，并只把副本中的 `db.py` 替换为 S4 基线匹配文件。S4 兼容代码读回了论文、投资主题、团队跟踪、Profile 和密钥；它把备份中的在途 `provider_started` 请求恢复为 `external_outcome_unknown`，第二次恢复返回 0，attempt 数保持 1。整个过程中只写入合成 attempt 行，没有发送网络请求。

实际命令：

```text
./tmp/a4-test-env/Scripts/python.exe -B -m unittest tests.test_s6_release_drill
```

结果：退出码 0，`Ran 1 test in 0.896s`，`OK`。首次夹具运行因主题关联要求论文已有成功全文评估而失败；加入一条合成成功评估后通过，没有修改生产代码。

## 匹配版本材料和代码哈希

S4 `db.py` 材料 `tmp/s5-review/db_before.py` 的 SHA-256 与 `S4_BASELINE.md` 记录一致。演练用它覆盖隔离副本中的 `db.py`，不是正式 Git 发布包；副本其余文件来自当前工作树。该方式验证的是 S5 到 S4 的兼容回退，不能代表 S0 回退或完整发布物复原。

| 材料 | SHA-256 |
|---|---|
| S4 基线匹配的 `db_before.py` | `5731591ca40822caad0d7fa83fd0345328d24faa181a539958ef23f6de00cae8` |
| 当前 S5 `daily_coolpapers/db.py` | `588dd3febfddf184275a76c41a138a2ed3663bc213c06d50548ed14cf27bb78a` |
| 当前 `call_attempts.py` | `157b526f4b74994c80cba9b1c7c89f0ae6beb23648436fea2ea3f3eb15249942` |
| 当前 `investment_themes_db.py` | `e92cc1884c3c949d0b5eefa854bd0562445c61358432a6c7ccce30d997bed0ed` |
| 当前 `research_entities_db.py` | `227c6f32512bad6ad54ca840857cbac99cb3c759d68221df3ff90e8e7aa8d95b` |
| 当前 `daily_coolpapers/**/*.py` 清单与内容摘要 | `03f29602d60e455676fbea7ffc7a1ad3c3e2bb8e599c13dc9f0646c7461aaebd` |
| 本演练测试 | `6e5a61bbabff5e3dab3cfa495b76c72cbab905882825cc54389cdd484e7c5804` |

测试默认从 `tmp/s5-review/db_before.py` 读取 S4 材料；其他 checkout 可设置 `S6_S4_DB_PATH`。文件缺失时测试会明确 skip，路径存在但哈希不符时会失败。

## 损失范围与限制

演练在备份后向源主库新增一条合成论文；恢复后该论文不存在。若生产环境恢复这组备份，备份时点之后主库和 Profile 库的新增、更新、删除记录都会回到备份时状态；备份后轮换或变更的本地密钥也不会随旧备份保留。演练只观察到一条合成论文的损失，没有估计生产记录数量。缓存、日志等未纳入这组备份的文件不在本次恢复保证内；外部 Provider 已产生的副作用也无法由 SQLite 恢复撤销。

未使用真实数据库、密钥、网络或运行时调度，未执行生产迁移，也未创建 Git 提交。此结果覆盖隔离的 S5→S4 读取与在途 unknown 恢复；完整 S4 发布包、S0 回退及生产恢复流程仍未验证。

后续 GitHub 交付准备仅清理了部分 Python 文件的多余末尾空行（见 PRD 21.4）；上表哈希记录本次演练时点，不随交付格式清理改写，测试语义和演练结果未变。
