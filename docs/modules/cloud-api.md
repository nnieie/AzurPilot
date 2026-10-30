# 客户端云 API

客户端负责截图识别、本地留存和重试。截图不发送到云端；未知物品只裁出
49×49 RGB PNG，直接上传独立模板服务器。主统计服务器只接收结构化委托、科研
掉落和既有 CL1 月度计数。大世界、战斗等其他掉落继续留在本地。

## 服务与端点

| 服务 | 方法与路径 | 客户端用途 |
| --- | --- | --- |
| `https://templates-api.azurpilot.com` | `POST /api/v3/post/templates/unknown` | 图标匹配与 U-ID 分配 |
| 同上 | `POST /api/v3/post/unknown-candidates` | 可选批量注册，最多 50 项且正文不超过 256 KiB |
| `https://templates.azurpilot.com` | `GET /latest.json` | 获取签名版本指针，支持 ETag |
| 同上 | `GET /releases/{revision}/manifest.json` | 获取签名模板与累计历史映射 |
| 同上 | `GET /releases/{revision}/icons/{sha256}.png` | 获取校验后的图标 |
| `https://api.azurpilot.com` | `POST /api/v3/post/drops` | 批量结构化统计，每批最多 200 条 |
| 同上 | `GET /api/v3/get/commission` | 公开委托统计 |
| 同上 | `GET /api/v3/get/commission/options` | 委托筛选项 |
| 同上 | `GET /api/v3/get/research` | 公开科研统计 |
| 同上 | `GET /api/v3/get/research/options` | 科研筛选项 |
| 同上 | `POST /api/v2/post/cl1` | CL1 月度原始计数 |
| 同上 | `GET /api/v2/get/server` | 游戏服务器状态 |
| 同上 | `GET /api/v2/get/maintenance` | 分区维护状态 |
| 同上 | `GET /api/v2/get/notice/data.json` | 公告 |
| 同上 | `GET /api/v2/get/update/data.json` | 更新策略 |

基础地址与验签公钥集中在 `deploy/api_endpoints.py`。云管理后台独立部署在
`https://admin.azurpilot.com`，公开统计在 `https://stats.azurpilot.com`；客户端不持有
云管理凭证。客户端原有本地 WebUI 的业务 WebSocket `/api/v1/ws` 不受此改动影响。

公开统计调用示例：

```python
from module.base.device_id import get_device_id
from module.cloud.protocol import PublicAPI

api = PublicAPI(get_device_id())
result = api.statistics('research', server='cn', series=9, timezone='Asia/Shanghai')
options = api.statistics('commission', options=True, server='jp')
```

## 上传结构

模板注册只提交 `image_base64`、`client_revision`、`scene`、`client`。响应中的
`template_id` 是当前正式 ID 或 U-ID，`identity_id` 是原始身份，后者用于保留订正能力。
客户端固定从配置的 Worker 域名更新模板，不跳转到响应指定的其他域名。

掉落上传体是 `{"drops": [...]}`。每条包括 `record_id`、`scene`、`server`、
`status`、带时区的 `collected_at`、`template_revision`、`client`、匿名 `device_id`
和 `items`。每项物品是 `{"item": "U-...", "tag": "bonus", "amount": 2}`。
委托额外提供 `comm`、`name`，状态 0 为完成、1 为大成功；科研提供 `series`、
`project`。不上传实例原名、截图、账号或管理凭证。

`accepted`、`duplicate` 回执移除队列记录；`invalid`、`unsupported_scene` 转入
本地 `rejected` 保留错误原因；`retry`、缺失回执、无效 JSON、网络错误和 429
保留记录并退避。重试沿用持久化 `record_id`，避免重复统计。模板服务或 CDN 暂时
不可用时，已能确认 ID 的统计仍可独立提交。

## 本地模板、历史映射和离线恢复

数据位于当前配置目录下 `cloud/{匿名实例哈希}/`：

| 路径 | 内容 |
| --- | --- |
| `catalog/active.json` | 已验证的活动签名清单与 ETag |
| `catalog/icons/` | 按 PNG SHA-256 保存的图标 |
| `catalog/pending-release.json` | 尚未完成历史迁移的恢复日志 |
| `icons/` | 待上传的 49px 图标与识别场景 |
| `bindings.json` | 本地待编号键到模板服务器身份的持久化绑定 |
| `jobs/` | 待确认的结构化统计 |
| `history-dirty.json` | 新本地记录提交后的历史映射刷新请求，覆盖注册与本地入库的并发顺序 |
| `rejected/` | 无法上报的完整记录与服务端原因 |
| `diagnostics/` | 委托配对等识别诊断 |

首次离线识别使用 `U?{RGB像素SHA256}` 作为本地待编号键。它只在客户端使用，
不会提交主服务器；注册取得服务器 U-ID 后，历史和待发送统计同步替换。

本地显示使用正式 ID，统计上报保留已分配的原始身份和现场标签，由主服务器按当前
映射聚合。这样在第一次映射之后新增的记录也保留来源，管理员后来更正映射或撤销
标签覆盖时，仍可正确重算；本地 `U?` 待编号键始终不会发送。

清单必须通过固定 Ed25519 公钥验签；图标同时核对 PNG 文件哈希、规范 RGB 像素
哈希、49×49 尺寸和不透明度。禁止版本回退、跨域资源和 HTTP 跳转。多个图标变体
可以共享一个正式 ID；正式 ID 末尾的 `_2` 会原样保留。

更新先下载并校验全部图标，再保存恢复日志，接着在既有业务存储事务中修正历史，
最后切换活动版本。崩溃重启会重放未完成迁移。同一事件映射为同一物品和标签时合并
数量，始终保留原始 `source_items`；管理员后来修正映射，仍可恢复各原始身份的数量。
空映射标签保留现场标签，非空标签覆盖现场标签。

新模块不包含手写 SQL，也不增加平行业务数据库。历史更新复用
`Cl1Database` 的事务与月度快照接口；客户端其他既有存储仍有历史 SQL 实现，
不能将本次改动理解成全客户端 ORM 迁移。

## 识别与开关

`DropRecord_CloudUpload=True` 且对应委托／科研记录方式为 `upload` 或
`save_and_upload` 才生成云端任务。关闭总开关后暂停待发送任务，保留本地数据。
重新启动实例时会恢复已有模板库和待处理队列。

委托根据完成／大成功页读取名称，与后续奖励、经验、舰船页面配对；使用明确的
名称目录和夜战、日服同名等奖励区间规则。名称不确定、多个奖励页等诊断保留在
本地，不编造委托名称或状态。现有本地资源收益和钻石委托结算继续使用原链路。

科研采集器保留每次领奖的边界，滚动奖励页归于同一个项目。项目代号和期数来自
对应队列卡片或科研列表，不用掉落物推断期数。未知物品保留数量和标签。缺少项目
或期数的云端记录转入 `rejected`，本地统计仍保留。

## 验证

```powershell
uv run --frozen python dev_tools/test_all.py
```

统一测试入口隔离配置目录，并将 Python、SQLite 和子进程临时目录指向本次运行目录，
避免平台默认临时目录的权限影响。结果、完整日志和 JUnit XML 保留在
`log/full-tests-{随机ID}/`。它执行完整 Python 套件、前端单元测试和生产构建；
`--python-only` 只跑 Python，`--last-failed` 只复跑上次失败的 Python 用例。

委托名称目录可用 `uv run --frozen python dev_tools/update_commission_catalog.py` 从固定
上游提交复现。生成器会先核对来源 SHA-256，拒绝静默接受变更。

真实服务闭环使用两个同级服务器仓库、它们的依赖、Go、Node、Wrangler 和本地
PostgreSQL 测试实例。默认 PostgreSQL 为 `127.0.0.1:55439`，用户 `template_test`。

```powershell
uv run --frozen python dev_tools/cloud_api_e2e.py
```

脚本创建随机独立 schema，启动两套服务和本地 Worker，测试两种未知图标、签名模板
更新、委托和科研各 52 条统计（含首次映射后新增的数据）、CL1 提交与重复回执、
历史合并、映射订正和客户端重启；退出时停止测试进程并
通过 SQLAlchemy 清理本次 schema。默认全仓测试跳过这个需要外部服务的测试，以上
命令会单独实际执行。验证结果写入 `log/cloud-api-e2e-result.json`。不做 WebUI 样式测试。
