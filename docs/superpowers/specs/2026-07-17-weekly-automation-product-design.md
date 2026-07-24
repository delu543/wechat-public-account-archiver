# 微信公众号每周自动归档产品设计

## 1. 产品目标

把现有抓取脚本升级为一个本地、可恢复、低维护的公众号订阅产品。

- 长期数据只保存到 `~/Library/Application Support/WeChatPublicAccountArchive/data/archive.sqlite`。
- 既有账号采用当前数据库作为基线，之后只抓取和输出新增文章。
- 新账号首次全量抓取并输出一次全量 Word，之后自动转为增量。
- 默认每周日 20:00 运行，时间可在网站修改。
- 每周统一输出一个批次目录，公众号之间保持独立。
- 重复运行、中断恢复和历史回溯都不能重复入库或重复写入 Word。

## 2. 保留能力与边界

继续保留现有 CLI、URL 历史发现、日期范围、三阶段正文抓取、Markdown 导出和文章浏览。

只处理公开内容。遇到登录、验证码、付费墙、频率限制、风控或会话失效时暂停当前账号，不绕过限制，不影响其他账号。

账号身份以 exact `biz` 为准。不同账号即使名称或正文相同，也必须保持独立归档。

## 3. 单文件数据模型

SQLite 是唯一长期数据源。WAL 和 SHM 只是在程序运行期间出现的临时旁文件，正常关闭和 checkpoint 后长期保留的仍是 `archive.sqlite`。运行时、日志和 Word 也安装在同一个 Application Support 产品目录，避免后台进程受 macOS“文稿”目录隐私权限影响。

现有表继续保存账号、URL、文章、抓取批次和日志。新增以下表：

### subscriptions

- `account_id`：唯一关联账号。
- `seed_url`：可重复打开的公开文章入口。
- `enabled`：是否参加自动同步。
- `onboarding_mode`：`adopt_existing` 或 `initial_full`。
- `initial_full_completed_at`：首次全量完成时间。
- `baseline_max_article_id`：采用现有数据时的文章基线。
- `lookback_days`：增量重叠回看天数，默认 7。
- `schedule_weekday`、`schedule_hour`、`schedule_minute`：周计划。
- `last_success_at`、`next_run_at`、`state`、`last_error`：运行状态。

### sync_jobs

- 持久化任务 ID、账号、计划时间、解析后的运行模式和日期边界。
- 阶段状态：排队、会话预检、历史发现、快速抓取、失败重试、导出、清理、完成。
- 保存发现、新增、失败、受限、导出计数和可恢复检查点。
- 保存租约，防止同一任务被两个进程重复执行。

### export_batches

- 每周批次或首次全量批次。
- 保存时间范围、状态、Word 路径、文章数量和集合指纹。

### export_batch_articles

- 保存 Word 与文章的成员关系。
- `batch_id + article_id` 唯一。
- 查询增量时排除已经进入成功输出批次的文章。

## 4. 模式判断

1. 数据库已有文章的账号迁移为 `adopt_existing`。
2. 迁移时记录当前最大文章 ID，旧文章不会进入以后增量 Word。
3. 新 URL 解析出从未存在的 `biz` 时建立 `initial_full` 订阅。
4. 首次全量中断时保持 `initial_full`，下次从检查点恢复。
5. 首次全量成功并输出 Word 后改为正常增量。
6. 手动历史回溯只会抓缺失文章，新增文章进入当周增量批次。

增量日期从账号最新发布日期向前回看 7 天。发现器仍从 offset 0 开始，越过边界并遇到连续已知页后停止。日期重叠不会产生重复，因为 URL 和文章保存均是幂等操作。

## 5. 每周任务状态机

```text
scheduled
  -> preflight
  -> discover
  -> crawl_fast
  -> retry_failed
  -> retry_unavailable
  -> export_markdown
  -> export_word
  -> cleanup
  -> completed
```

会话失效进入 `needs_session`；验证码、付费墙和持续风控进入 `blocked`。任务状态保存在 SQLite，网站或进程重启后不会丢失。

## 6. 去重与版本语义

- URL 层：规范 URL 和稳定 URL key。
- 微信身份层：`biz + mid + idx + sn`。
- 文章层：`account_id + title + publish_time`。
- 内容层：`account_id + content_hash`。
- 输出层：成功批次中的 `article_id` 只能首次输出一次。
- 文档层：文章 ID 集合生成 fingerprint，集合相同则跳过重复 Word。

当前全局 `content_hash` 唯一索引需要迁移为账号范围内唯一，避免不同公众号转载相同正文时发生账号覆盖。

## 7. Word 输出

每周目录：

```text
output/weekly/2026-07-20/
  本周公众号增量汇总.docx
  请辩_本周新增8篇.docx
  子说一点_本周新增3篇.docx
  run-report.json
```

- 既有账号只输出从未进入成功批次的文章。
- 新账号首次输出一次完整合集。
- 本周无新增时不生成空账号 Word，只在汇总中记录 0。
- 补录的旧文章按原发布日期排序，并标记本周补录。
- Word 保留标题、账号、作者、发布日期、原文链接和抓取时间。
- Word 成功通过结构检查后才登记成员关系。

## 8. 存储清理

长期保留：账号、规范 URL、正文纯文本、清理后的正文 HTML、元数据、hash、任务摘要、Word 成员关系和最终 Word。

成功输出后可清理：

- `articles.raw_html` 原始整页网页，只在正文已经解析成功时置空。
- 产品运行目录中的临时 HTML、PDF、PNG 和过期渲染目录。
- 已完成任务的详细过程日志按保留期压缩或删除，仅保留摘要。

不自动删除文章正文、Markdown、最终 Word、失败原因或用户输入文件。SQLite 每月或达到增长阈值后执行 checkpoint 和 `VACUUM`，不在每周任务中频繁重写数据库。

所有任务退出路径都执行产品临时文件清理，包括成功、零新增、抓取失败、等待会话和人工中断后的恢复任务。清理范围仅限产品输出目录中的 `.automation-tmp`、未完成的 `*.tmp` 和数据库内已经生成清洗正文的可再生 `raw_html`；不触碰微信客户端缓存或其它应用目录。菜单栏状态轮询只读取任务摘要，不扫描正文或统计全库清理体积。

## 9. 调度与 macOS

安装命令先把独立 Python 运行环境、当前产品代码和配置复制到 Application Support，再由 `launchd` 负责登录后启动本地网站和后台调度器。Python 调度器每分钟检查 SQLite 中的 `next_run_at`，每周只创建一个批次。

- 默认周日 20:00。
- 计划时间睡眠或关机时，唤醒后补跑一次。
- 历史发现串行；正文抓取有限并发。
- 同一账号同一周期只允许一个有效任务。
- 19:30 可做轻量会话预检，失效时发送 macOS 通知。

## 10. 本地网站

首页变为操作型控制台：

- 顶部显示自动同步开关、下次运行和本周批次状态。
- 账号表显示启停、最新文章日期、最近成功、下次运行和新增数。
- 支持添加文章 URL、立即同步、暂停、补抓失败和查看 Word。
- 任务详情显示阶段、进度、ETA、失败分类和会话恢复提示。
- Word 页面按每周批次分组，不把大量文章直接铺在首页。

现有账号文件夹、文章列表和文章正文浏览继续保留。

## 11. API 与 CLI

新增 API：订阅列表、订阅设置、自动化概览、立即运行、批次列表和 Word 下载。

## 11. macOS 菜单栏状态

安装器同时生成本机原生 `WeChatArchiveStatus.app`，并用独立 LaunchAgent 在用户登录后启动。菜单栏显示“归档”；弹出层提供订阅数、待输出数量、下次运行、当前阶段、百分比、预计剩余时间、会话异常和最近 Word。它只访问 `127.0.0.1` 的轻量状态接口，不读取文章正文，也不持有微信签名参数。

后台服务与菜单栏进程独立：后台继续负责调度、数据库和 Word，菜单栏仅负责状态展示与打开本地路径。菜单栏退出或重启不改变任务状态。

新增 CLI：

```text
automation-adopt-existing
automation-run [--account-biz]
automation-status
automation-cleanup [--execute]
automation-install-launch-agent
```

CLI、网站和调度器调用同一个同步服务，避免三套逻辑漂移。

## 12. 验收

- 既有账号迁移后本周候选数为 0。
- 新账号解析为首次全量。
- 同一任务重复运行两次，文章数和 Word 成员数不变。
- 任务中断后恢复，不重新输出已成功文章。
- 一个账号 `needs_session` 不阻塞其他账号。
- 成功 Word 中标题数、链接数和成员表一致。
- 清理后正文仍可搜索、Markdown 仍可导出、Word 仍可打开。
- 原有测试与新增自动化测试全部通过。
