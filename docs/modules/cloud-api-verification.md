# 云 API 接入验收记录

验证日期：2026-10-30。代码位于 AzurLaneAutoScript 的 `dev` 分支。

## 最终结果

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 完整 Python 套件 | 4807 通过、0 失败、13 跳过；6087 子测试通过；802.12 秒 | `log/full-tests-fcba2b3e8c/python.log`、`python.xml`、`results.json` |
| 前端单元测试 | 69 个测试文件、471 项测试全部通过 | `log/api-client-frontend-tests.log` |
| TypeScript 与生产构建 | `npm run build` 通过，包含 `tsc -b` | `log/api-client-frontend-build.log` |
| 真实跨服务闭环 | 1 项集成测试通过；104 条委托／科研记录 | `log/cloud-api-e2e.log`、`log/cloud-api-e2e-result.json` |
| 生产只读检查 | 8 个 GET 端点均为 200；Worker 签名校验通过 | `log/cloud-api-production-readonly.json` |
| 静态检查 | 新增云模块、接口客户端、测试工具与协议测试 Ruff 通过；`git diff --check` 通过 | 本地检查输出 |

Python 套件另有 754 条警告，包含已有 asyncio、Pillow、websockets 弃用提示和
OmegaConf 提示。构建保留已有的大 chunk 体积提示；这些没有导致检查失败。

## 闭环覆盖

启动真实 PostgreSQL 上的两个独立随机 schema，运行当前 Go 主服务器、Python
模板服务器及 Wrangler 本地 Worker；客户端使用实际 HTTP／WebSocket 连接。

1. 两种合成 49px 图标由客户端直接注册，获得两个服务器 U-ID。
2. 相同图标通过批量接口再次注册，返回原 ID；跨场景仍使用同一身份。
3. 客户端从 Worker 下载清单、验证固定测试公钥签名和图标哈希。
4. 委托、科研各上报 51 条，验证 accepted／duplicate 回执及公开统计。
5. 管理员经模板服务 WebSocket 发布映射，将两种身份合并为同一正式 ID。
6. 验证本地历史、主服务器历史和公开统计同步；同一事件合并数量时只记一次出现。
7. 客户端重启后，新增委托、科研各 1 条，继续保留原始模板身份。
8. 管理员将其中一个映射订正为另一正式 ID，并设置标签；验证新增和旧记录都正确拆分。
9. CL1 通过真实主服务器提交两次，验证正常和重复业务回执。

订正后，两类统计分别为 52 个样本、总数量 258：

| 物品与标签 | 数量 | 出现次数 |
| --- | --- | --- |
| `BlueprintClientE2E_2`、空标签 | 102 | 51 |
| `BlueprintCorrected`、`bonus` | 156 | 52 |

测试使用合成图标和本地测试令牌。生产环境只执行 GET 检查，没有上传测试图标或统计。
测试进程退出后，通过 SQLAlchemy 清理本次随机 schema。

## 全仓回归修复

- 修复较远祖先进程消失时，uv／Windows Python 转发器被误判为旧入口的启动问题，
  保留对真正运行中的入口和已登记 worker 的检查。
- 修复 TUI 时钟在默认页面挂载或销毁期间找不到 HeaderBar 的异常。
- 失效的指挥喵合并计算器测试改为验证当前实际购买和溢出购买调用；保留 29 个用例。
- 移除已经删除的 `module.island_handler` 实现所遗留的旧商品名称测试。
- 修正 OCR 有效标志、实际图片颜色识别、归档活动版本、资源时间线存储入口和数字文本
  规范化等测试夹具；测试数据通过当前业务接口写入。
- Git over CDN 测试以独立 bundle 构造旧／新提交的可达对象集合，保持更新验证，
  避免依赖 Windows 会话中不可启动的 MSYS 文件传输 shell。
- 统一测试入口隔离配置目录及 Python／SQLite 临时目录。

## 跳过边界

13 项跳过均有明确条件：Linux／POSIX 权限及 fork 3 项、原生 macOS 2 项、
Windows 符号链接创建权限 3 项、MSYS Bash 命名对象权限 1 项、未提供 Rust 启动器
测试二进制 1 项、无 TPM2 硬件 1 项、未指定旧加密数据测试来源 1 项，以及需要
独立启动云服务的闭环 1 项。最后一项已通过专用启动脚本另行实际执行。

没有连接真实游戏设备，没有进行 WebUI 样式或截图评测。

## 复现命令

```powershell
uv run --frozen python dev_tools/test_all.py
uv run --frozen python dev_tools/cloud_api_e2e.py
```

完整接入协议、域名、端点、开关、缓存和恢复说明见 [cloud-api.md](cloud-api.md)。
