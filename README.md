# 微信公众号公开内容本地归档

面向 macOS 的公开文章和短内容归档：精确账号隔离、增量去重、断点保留、每个公众号一份带时间的 Word。

**当前已验证的是被动缓存路线，不是无条件的全自动全历史抓取。** 微信写入的公开缓存可被批量累计；缓存未包含的新列表不能由旧 seed 链接凭空补出。完整发表覆盖、真实来源可用性和插件部署须分别验收。

## 推荐工作流

在实际产品工作区使用已配置的 Python 环境：

```sh
# 默认只读检查：已有暂存、产品节点、已验收 Word 收据
.venv/bin/python main.py cache-update --check

# 已有正文直接整理；明确接受未知完整覆盖
.venv/bin/python main.py cache-update --export --accept-discovered
```

没有新缓存时，先启动有限收集，再正常浏览**受影响账号**的主页，不必逐篇打开：

```sh
.venv/bin/python main.py cache-update --collect --seconds 600 --export --accept-discovered
```

仅本次独立收集明确获准时，可增加 `--allow-local-stage-while-paused`。它不解除后台暂停；暂停状态变化会停止提交。命令不操作微信、不读取聊天、不获取/重放客户端签名、不发网络请求。

Word 先通过标题、正文、永久链接和成员核验，状态为 `awaiting_render_review`。交付者实际渲染检查后登记：

```sh
.venv/bin/python main.py cache-update --confirm-delivery '<返回的批次目录>' --render-reviewed
```

该标记不是渲染器，也不能替代检查。登记后的真实成员用于下一轮去重；不推进未证实的完整归档节点，不吞掉旧缺口。无需要求用户为正常文档 QA 再手动点确认。

完整配置、历史批次迁移、日期/排除账号/中断恢复见 [缓存增量使用说明](docs/CACHE_UPDATE.md)。

## 需要多少人工

- 数据已经在缓存或暂存中：读取、累计、去重、分号 Word、收据核对可自动完成。
- 新目录没有进入缓存：仍可能需要用户正常打开对应主页并向下加载到提示日期。程序先收集，只提示缺数据的号；不能保证每周零人工。
- 已绑定的公众号推送库是另一条条件路线，但专用初始化/HMAC/真实覆盖尚未完整验收，且推送可能缺“发表未群发”。不在缓存不足时自动 setup。见 [条件推送路线](docs/PASSIVE_WEEKLY.md)。
- 全历史需要可验证的分页目录或实际已取得的缓存。当前没有已验收的完整历史替代源；只含长文章的目录不能替代含短内容的全量目标。

原 `reverse`、`history-sync`、`history-cache-sync`、控制台与菜单栏代码保留。旧客户端会话分页/前台恢复属于条件功能，不是当前推荐入口的自动兜底；不要因它们存在就重复点击微信或重试受限请求。

## Codex Skills

仓库内 `.agents/skills/` 提供两个可独立使用的 Skill：

```text
$wechat-public-account-subscriptions 更新全部公众号到今天。
$wechat-public-account-subscriptions 添加公众号：https://mp.weixin.qq.com/s/...；从 YYYY-MM-DD 开始。
$wechat-public-account-history 归档这个公众号的全部历史。
```

新账号需要公开 seed、精确身份以及开始日期/全部历史选择；已登记的号复用现有订阅，不需每周重新给链接。Skills 会核对当前来源及数据证据，不把旧接口或固定点击序列当作强制步骤。遇到真实登录/验证/限制必须停受影响来源。

## 安装与配置

macOS 13+，Python 3.9+；菜单栏构建另需 Xcode Command Line Tools。只运行缓存 CLI 不需要安装后台服务。仅在用户明确要求安装时创建环境：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

配置来自 `config.yaml`；私人迁移路径、排除账号、冻结交付节点请在本地配置，不能提交到公开仓库。已有数据库不能用空白样例覆盖。默认数据目录：

```text
~/Library/Application Support/WeChatPublicAccountArchive/
  data/archive.sqlite
  output/cache-updates/
    stages/
    deliveries/batch-日期-指纹/
      公众号_起点至终点_已发现增量_标识.docx
      plan.json
      delivery-manifest.json
      verification.json     # 实际渲染验收后才生成
```

本发布样例关闭定时任务与自动前台会话准备，不会自动恢复用户原有暂停。源码更新不等于安装副本更新；切换旧插件/每周日21:00定时运行，还要另验安装路径、暂停按钮、实际新数据和通知打开的目录。

如用户另外请求安装本地控制台/菜单栏，可先只做预检：

```sh
./install.sh --check
```

`./install.sh` 会安装依赖并启动本地服务，不能在仅更新源码或查看仓库时运行。控制台仅监听 `127.0.0.1:8765`。新缓存入口当前是 CLI，未自动接管旧“立即同步”按钮。历史输出仍保留原目录，不迁移或删除。

## 工程与验证

```sh
.venv/bin/python -m pytest -q
./install.sh --check
```

- 公开缓存支持 bounded Snappy/V8、LevelDB 日志/SST/内联值以及跨窗口累计，按精确 biz/微信号/永久身份保存。
- 收据逐成员/文件哈希去重，已交付正文变化作为版本缺口，不覆盖旧 Word。
- 未完成批次优先恢复；新发现留给下一批。没有合格内容不生成空文档，未知不记为无更新。
- 真实旧批次只读核验与合成回归分开，源覆盖、排版、安装和自动调度不是同一层验收。

详见 [能力图](docs/CAPABILITY_MAP.md)、[读取格式及覆盖验收](docs/PASSIVE_CACHE_CAPTURE.md)。

## 隐私与发布边界

只处理用户授权的公开内容。登录、验证码、付费墙、限频或访问限制会停止受影响来源及共享依赖；其余独立本地整理可继续。不得换身份、代理或入口绕过限制。

不上传文章库、Word、私有配置、微信缓存或凭据；不自动取密钥、不屏幕保护绕过、不解锁 Mac、不读取历史聊天。数据库、旧输出与任务证据默认保留，清理/部署/初始化均需各自授权。

## 许可证

MIT。缓存格式适配为独立实现，来源和采用边界见格式说明；未复制或打包非商业限制仓库的代码。
