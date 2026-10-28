# 人工刷新后的公开缓存累计与验收

常规使用已收敛为 [`main.py cache-update`](CACHE_UPDATE.md)：默认只读核对、按号收据去重、有限收集、已发现 Word 及渲染后登记。下文是底层累计器/严格完整性接口，不必每轮手工拼接这些模块。当前仍没有已验证的零人工完整列表来源。

## 当前能力与边界

本入口用于用户正常浏览公众号主页后，被动读取本机已经写入的公开文章缓存。
不点击微信、不刷新网络、不读取聊天/推送加密库、不获取或重放客户端签名，不解除后台总暂停。
它不是“打开一篇就能证明全部历史”的接口；未见的数据保持未知，不能记作零新增。

读取来源是当前活跃 multitab 的 `weixin_xworker_0.indexeddb`：

- 独立 blob：先识别 Chromium `ff1102` raw Snappy，再解码 V8 完整对象。
- 行内 LevelDB：按 CURRENT/MANIFEST 选择当前表与日志，验证物理 CRC、日志分片和 WriteBatch/SST 结构。
- 同一 key 只取最新 sequence，删除记录抑制旧版本；不能扫描字节片段、混拼字段或复活过期表。
- 精确 `biz`、`gh_` 及永久链接四元组校验通过才保留；传输签名脱敏。
- 表、日志或 manifest 变化、损坏、超时/超量都显式报不完整；不视作零文章。

每轮最多64次文件读取、单文件20MiB、输入/解压各40MiB、20秒软预算及30秒子进程硬超时。
累计会话也有时间和轮数上限。只在有本轮明确授权时使用下方暂停例外；例外不修改后台开关。

## 命令

### 多号共用一次快照

多号入口为 `python -m wechat_crawler.cache_capture_all`，默认读取已启用的精确订阅。
每轮只进行一次公开缓存IO，再按账号分别写入拥有标记的独立stage，不是每个账号重复扫缓存。
没有可用记录的号保持未知，不创建空stage，也不能宣称本周无新增。

```sh
.venv/bin/python -m wechat_crawler.cache_capture_all --plan --until 2026-10-28 --stage-root '<本轮独立目录>'
.venv/bin/python -m wechat_crawler.cache_capture_all --check --until 2026-10-28 --stage-root '<本轮独立目录>'
.venv/bin/python -m wechat_crawler.cache_capture_all --collect --until 2026-10-28 --stage-root '<本轮独立目录>' --seconds 1200 --interval 2 --allow-local-stage-while-paused
```

计划和单次检查不写stage。真实暂存仍需要本人授权，不触碰后台暂停。
在收集器有限运行期间，用户可以正常人工浏览公众号主页，从最新翻到各号已核对的旧节点；程序不操作微信。
收集完成后仍需独立完整清单及正文对账，人工说“已翻到节点”不自动生成完整清单确认。

日期延展必须使用新批次。可给collect追加 `--retain-stage '<旧批次的具体.sqlite绝对路径>'`，
将同账号、同起点且终点不缩小的历史公开证据只读继承到新批次。
旧文件用SQLite只读事务读取，原窗口成员保持分开，不合并伪造重叠；不复制旧导出收据或推进产品节点。
有未解决正文版本冲突、范围不符或来源不是自有stage时停止继承，保留原文件。

先暂存后才知道最新身份时，`CaptureStore.bind_newest`只接受实际观测到的永久链接及
绑定原scope哈希的本人确认收据；不能用缓存数据冒充确认，也不能替换已绑定的锚点。
锚点补录不改历史文章或窗口；同一确认重复提交保持幂等。

### 单号兼容入口

先核对精确订阅与区间，不写 stage：

```sh
.venv/bin/python -m wechat_crawler.cache_capture --account-biz '<精确biz>' --until 2026-10-27 --plan
```

只读一次当前公开缓存：

```sh
.venv/bin/python -m wechat_crawler.cache_capture --account-biz '<精确biz>' --until 2026-10-27 --check
```

经本人授权本次公开缓存暂存后，使用预先列明的独立文件：

```sh
.venv/bin/python -m wechat_crawler.cache_capture --account-biz '<精确biz>' --until 2026-10-27 --newest-url '<已知最新永久链接>' --collect --stage '<任务专属绝对路径.sqlite>' --seconds 600 --interval 2 --allow-local-stage-while-paused
```

未授权的默认行为是在暂停状态停止，不读缓存或创建 stage。不能把此参数用于恢复插件/定时任务。
运行中暂停状态或其更新时间发生任何变化都会停止，事务提交前再次核验，Ctrl+C保留已提交批次。
续跑必须使用同一精确范围、身份和 stage；不同范围不能覆盖原文件。

## 区间及正文验收

stage 只保存已校验的公开记录、批次身份集合、冲突和输出收据；不是产品文章入库。
正文非空且来自结构化 content 字段，只说明缓存有内容；显式付费/截断标记阻止完整正文验收。
各批次通过身份重叠连接、见到最新与旧节点，可以辅助核对，不单独证明中间没有缺文。

完整输出另需可信、精确区间发表清单，逐项对账日期、永久URL、缺文、额外项、正文和内容冲突。
人工清单的确认收据绑定其 SHA256 及 scope；不能把程序观察清单自称本人确认。
“已翻到旧节点”只记录真实浏览边界，不伪造为完整清单确认。
没有完整来源时应报告已发现篇数和未知缺口，不生成声称全部的 Word，也不推进全量节点。

Word准备入口是 `python -m wechat_crawler.cache_capture_word`。人工清单保留确认收据；
有真实完整分页证据的目录由程序验收，不再强制 `--index-confirmation` 或本人补录最新锚点。
机器目录逐项对齐健康缓存正文，不需要人为制造缓存批次间的重叠关系。
按原模板、永久链接及已交付正文去重；不生成空 Word、不覆盖旧文件。
生成后还需成员核验与逐页渲染检查；`word_ready_for_render_review` 不是交付成功。
产品归档、成功节点及插件安装副本不在暂存阶段修改。
配置中标为旧片段的文章，必须取得不同且更完整的结构化正文；原样片段或更短内容阻断完整导出，
即使它有旧批次成员身份也不能被静默当作已完成。更长不单独构成完整性证明，其余来源验收仍需通过。

### 可机验的独立目录

`cache_capture_index.verified_pagination` 接受 `native_history_index` / `official_export`
来源的标准化分页证据。现有缓存正文不能自行生成这份证据，单写来源名称或 `all_publications_for_interval=true` 不生效。

在完整清单上附 `pagination`：`schema=1`、`content_types=all`、`order=newest_first`、
`filters=[]`、含时区的 `observed_at`、`pages`、`stop_reason`。
读取时间必须覆盖本轮终日（北京时间），不得明显位于未来；原始来源必须包含短内容，不能把“仅文章”目录标为全部。
每页含 `payload` 和 `sha256=digest(payload)`。payload 仅有：精确 `biz`、数字 `offset`、
`next_offset`、布尔 `has_more`、`articles`；文章项为永久公开 `url` 与 `publication_day`。
第一页 offset=0，后页连续，next_offset 必须等于 offset 加本页条数；身份不重复、日期从新到旧。
终页必须明确无下一页（`end_of_history`），或已越过包含式起点之前（`crossed_before_since`）；
仅滚到起点当天、达到页数上限、目录漏项/额外项/日期不符都不算完整。

哈希只检查证据一致性，不代表来源真实性或授权。适配器必须从真实独立列表取得页序和终止信息，
不得由正文合集反推分页或编造人工确认。目前真实缓存尚未提供这样的完整目录，离线格式测试通过不代表九号覆盖通过。

## 规则适配与当前授权

旧 Skill 的固定接口、只能读不能保存或两次编辑审批，不是本功能的额外权限边界。
依用户当前授权，正常公开请求、独立暂存和必要源码修正可持续推进；此只读收集命令本身仍不发网络请求。
真实验证码/访问限制、旧数据保护、后台暂停和“完整后导出”的要求保持不变。

## 外部方案参考与实际采用

参考了 [yichen-wechat-mp-batch-exporter 的工作流](https://raw.githubusercontent.com/mcncarl/yichen-skills/main/yichen-wechat-mp-batch-exporter/references/exporter-workflow.md)
中的“先索引对账再正文/导出”设计，并将群发组数、扩展文章URL数和原创数视作不同指标。
未知字段必须保持未知；不能拿主页“原创内容”总数与本轮URL数量直接比较。

该仓库 [许可证](https://github.com/mcncarl/yichen-skills/blob/main/LICENSE) 为个人学习及非商业用途，
并非通常所称的 OSI 开源许可证。本项目未复制、安装、运行或打包其代码，只独立实现通用设计。
其历史来源依赖公众号后台及其他工具，相关 [上游已公告核心接口关闭](https://github.com/wechat-article/wechat-article-exporter/issues/200)，
不能视为已验证的完整列表替代源。未采用第三方URL下载代理、auth-key登录、系统代理或CA证书路线。

二进制格式实现参考 [LevelDB日志](https://github.com/google/leveldb/blob/main/doc/log_format.md)、
[SST格式](https://github.com/google/leveldb/blob/main/doc/table_format.md)、
[Chromium存储压缩说明](https://developer.chrome.com/docs/chromium/indexeddb-storage-improvements) 与 Google dfindexeddb 的格式定义。
均为格式对照与独立实现，无新依赖或第三方代码搬入。离线测试不等于真实完整区间或每周自动化验收。
