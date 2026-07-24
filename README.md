# 公众号自动爬虫设计

一个面向 macOS 的微信公众号公开文章归档工具。输入公众号任意一篇公开文章链接，系统会识别账号、发现公开历史文章、去重抓取正文，并按公众号生成 Markdown 和 Word。

项目只处理公开可访问内容，不绕过登录、验证码、付费墙或微信风控。

## 主要能力

- 单篇公开文章链接扩展为同一公众号历史归档
- 新公众号首次全量、已有公众号每周增量
- 精确 `biz` 账号隔离，避免同名公众号混档
- URL、文章身份、正文 hash、Word 批次四层去重
- SQLite 断点续跑和失败原因记录
- 每个公众号独立 Word，零新增时不生成空文档
- 本地控制台、菜单栏状态工具、每周自动运行
- 验证码、登录、限频和不可访问页面自动暂停并记录

## 系统要求

- macOS 13 或更新版本
- Mac 微信 4.x，且用户已正常登录
- Python 3.9 或更新版本
- Xcode Command Line Tools（用于编译菜单栏工具）

缺少 Command Line Tools 时运行：

```bash
xcode-select --install
```

## 在 Codex 中使用

1. 在 Codex 中打开本仓库。
2. 对 Codex 说：`安装并启动这个项目`。
3. Codex 会读取根目录的 `AGENTS.md` 和项目内 Skill，执行预检、测试及安装。

Codex 可以通过仓库内的 `AGENTS.md` 获得项目操作说明，但出于本机安全边界，仅仅查看 GitHub 页面不会静默执行程序；仍需要用户明确发出一次安装指令。参见 [OpenAI 对 AGENTS.md 的说明](https://openai.com/index/introducing-codex/)。

## 命令行安装

```bash
git clone "https://github.com/delu543/wechat-public-account-archiver.git" "公众号自动爬虫设计"
cd "公众号自动爬虫设计"
./install.sh
```

安装器会：

1. 检查 macOS、Python、Swift 编译器和微信。
2. 创建项目虚拟环境并安装依赖。
3. 运行完整测试。
4. 安装本地后台服务和菜单栏“归档”工具。
5. 打开 `http://127.0.0.1:8765`。

只做环境预检：

```bash
./install.sh --check
```

安装但不自动打开浏览器：

```bash
./install.sh --no-open
```

## 日常使用

安装后，macOS 菜单栏会出现“归档”：

- **打开控制台**：查看订阅、进度、预计剩余时间和失败原因。
- **打开输出目录**：在 Finder 中查看生成的 Word。
- **立即同步**：手动检查某个公众号。
- **暂停/启用**：控制单个账号是否参加每周任务。

默认每周日 20:00 自动运行。控制台网页可以关闭，后台服务不会停止。计划运行时需要：

- Mac 已登录且未锁屏
- 微信已登录
- 网络和用户自己的 VPN 保持正常

如果电脑在计划时间关机或休眠，登录后调度器会补跑；涉及微信前台操作时，锁屏状态会等待而不会操作登录界面。

## 添加公众号

在控制台粘贴该公众号任意一篇公开文章链接：

- 起始日期留空：首次抓取全部可发现历史
- 填写日期：只处理该日期及以后
- 首次完成后：自动转为每周增量

旧文章链接可以作为长期种子，不要求用户每周提供新链接。系统先尝试保存的 URL 和精确 `biz`；仅在公开历史会话失效时，才使用已授权的 Mac 微信可见会话恢复。

## 输出位置

长期数据保存在：

```text
~/Library/Application Support/WeChatPublicAccountArchive/
  data/archive.sqlite
  output/
    weekly/
      YYYY-MM-DD/
        公众号名称_本周增量.docx
        本周公众号增量汇总.docx
  logs/
```

已有账号只把尚未进入成功 Word 批次的新文章写入下一份文档。数据库、正文 hash 和批次成员关系共同保证重跑不会重复输出。

## 本地控制台

地址：`http://127.0.0.1:8765`

控制台提供：

- 公众号文件夹和文章阅读
- 周计划设置
- 当前阶段、进度和 ETA
- 发现、重叠、新增、失败、受限、Word 输出计数
- 持久化失败原因
- 最近 Word 下载
- Finder 输出目录入口
- 可再生成缓存清理

## 更新与卸载

更新代码后重新运行：

```bash
git pull
./install.sh
```

安装器保留数据库、Word 和已有菜单栏 App 身份，避免升级时反复触发 macOS 授权。

停止自动服务但保留数据：

```bash
./uninstall.sh
```

## 开发与测试

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

代码结构：

```text
wechat_crawler/
  crawler/             抓取、解析、发现、存储、导出
  web/                 本地控制台
  macos_status/        菜单栏状态工具
  automation_store.py  自动任务和 Word 批次
  weekly_sync.py       全量/增量调度
  launch_agent.py      macOS 登录自启
tests/
.agents/skills/        项目内 Codex Skill
```

## 隐私与边界

- 不上传本机数据库、文章归档、日志或微信缓存
- 不保存或输出微信签名参数和会话凭据
- 不截图或规避微信的隐私画面保护
- 不自动输入密码或解锁 Mac
- 不破解验证码、登录、付费或访问限制
- 默认只监听本机 `127.0.0.1`

详细能力边界见 [docs/CAPABILITY_MAP.md](docs/CAPABILITY_MAP.md)。

## 许可证

MIT
