
# AzurPilot

<p align="center">
  <a href="https://deepwiki.com/wess09/AzurLaneAutoScript">
    <img src="https://deepwiki.com/badge.svg" alt="DeepWiki" height="22">
  </a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/github/license/wess09/AzurLaneAutoScript?style=flat-square&label=License&color=2ea44f" alt="License">
  <img src="https://img.shields.io/github/stars/wess09/AzurLaneAutoScript?style=flat-square&label=Stars&color=ffcc00" alt="Stars">
  <img src="https://img.shields.io/github/forks/wess09/AzurLaneAutoScript?style=flat-square&label=Forks&color=58a6ff" alt="Forks">
  <img src="https://img.shields.io/github/issues/wess09/AzurLaneAutoScript?style=flat-square&label=Issues&color=f85149" alt="Issues">
</p>

<p align="center">
  <img src="https://img.shields.io/github/last-commit/wess09/AzurLaneAutoScript?style=flat-square&label=Last%20Commit&color=8b949e" alt="Last Commit">
  <img src="https://img.shields.io/github/commit-activity/m/wess09/AzurLaneAutoScript?style=flat-square&label=Commit%20Activity&color=8957e5" alt="Commit Activity">
  <img src="https://img.shields.io/github/repo-size/wess09/AzurLaneAutoScript?style=flat-square&label=Repo%20Size&color=orange" alt="Repo Size">
  <img src="https://img.shields.io/github/languages/top/wess09/AzurLaneAutoScript?style=flat-square&label=Top%20Language&color=3776AB" alt="Top Language">
</p>

<p align="center">
  <img src="https://img.shields.io/github/contributors/wess09/AzurLaneAutoScript?style=flat-square&label=Contributors&color=00b4d8" alt="Contributors">
  <img src="https://img.shields.io/github/issues-pr/wess09/AzurLaneAutoScript?style=flat-square&label=Pull%20Requests&color=ffb703" alt="Pull Requests">
  <img src="https://img.shields.io/github/issues-pr-closed/wess09/AzurLaneAutoScript?style=flat-square&label=PRs%20Closed&color=2ea44f" alt="Closed Pull Requests">
</p>

## 项目简介

AzurPilot 是基于 AzurLaneAutoScript 的碧蓝航线自动化辅助项目，主要用于日常任务、委托、科研、大型作战及相关自动化流程。

本项目为 AzurLaneAutoScript 的二次修改版本，保留原项目的核心能力，并在此基础上整合了多个分支、功能改进和实验性特性。

项目原始来源与相关分支：

- AzurLaneAutoScript 原项目及其生态分支
- 雪风源相关分支
- Alas-with-Dashboard 的部分功能
- guoh064 分支的部分大型作战相关功能
- sui-feng-cb 分支的部分岛屿相关功能
- 其他社区贡献的实用 Pull Request

下载地址：

[https://alas.nanoda.work/download.html](https://alas.nanoda.work/download.html)

问题反馈与交流：

[https://addgroup.nanoda.work/#/](https://addgroup.nanoda.work/#/)

## 赞助支持

<p align="center">
  <a href="https://afdian.com/a/miaonaa">
    <img src="doc/afdian.jfif" alt="爱发电" width="400">
  </a>
  <br>
  <b>爱发电支持本项目(用于支付服务器费用等)</b>
</p>


## 重要说明

本项目包含大量自动化逻辑和图像识别相关功能。使用前请确保已经按照本文档完成游戏内设置，否则可能导致识别失败、流程异常或任务无法正常执行。

本项目包含部分实验性功能，可能存在未知问题。建议在使用前备份相关配置，并在发现异常时及时反馈。

## 使用前设置

使用前必须按照以下标准修改游戏内设置。

路径：

主界面，右下角设置，左侧边栏选项。

| 设置名称 | 推荐值 |
| --- | --- |
| 帧数设置 | 60 帧 |
| 大型作战设置，减少 TB 引导 | 开 |
| 大型作战设置，自律时自动提交道具 | 开 |
| 大型作战设置，安全海域默认开启自律 | 关 |
| 剧情自动播放 | 开启 |
| 剧情自动播放速度调整 | 特快 |
| 待机模式设置，启用待机模式 | 关 |
| 其他设置，重复角色获得提示 | 关 |
| 其他设置，快速更换二次确认界面 | 关 |
| 其他设置，展示结算角色 | 关 |

### 大型作战设置

路径：

大型作战，右上角雷达，指令模块，潜艇支援。

| 设置名称 | 推荐值 |
| --- | --- |
| X 消耗时潜艇出击 | 取消勾选 |

### 一键退役设置

路径：

主界面，右下角建造，左侧边栏退役，左侧齿轮图标，一键退役设置。

| 设置名称 | 推荐值 |
| --- | --- |
| 选择优先级 1 | R |
| 选择优先级 2 | SR |
| 选择优先级 3 | N |
| 拥有满星的同名舰船时，保留几艘符合退役条件的同名舰船 | 不保留 |
| 没有满星的同名舰船时，保留几艘符合退役条件的同名舰船 | 满星所需或不保留 |

### 图像识别注意事项

请移除以下可能影响识别的内容：

- 角色设备装备
- 角色皮肤
- 可能遮挡界面元素的自定义显示内容

这些内容可能影响图像识别结果，导致自动化流程出现异常。

## 主要改动

本分支在原项目基础上加入或整合了以下内容：

1. 智能调度
2. 大型作战限制解除相关功能
3. 侵蚀 1 相关功能
4. 部分未合并但实用的 Pull Request
5. 舰娘等级识别
6. 侵蚀 1 相关统计
7. 模拟器管理
8. Python 版本迁移
9. OCR 模型更换
10. GPU 加速推理支持
11. Alas MCP 服务
13. 其他实验性改动与细节优化

## 多平台启动器

启动器项目地址：

[https://github.com/wess09/alas-launcher](https://github.com/wess09/alas-launcher)

该启动器 fork 自：

[https://github.com/swordfeng/alas-launcher](https://github.com/swordfeng/alas-launcher)

启动器项目遵守上游许可证要求，继续使用 GPL-3.0 协议开源。

## MCP 服务

AzurPilot 提供 MCP 服务，可供支持 MCP 的客户端或工具调用。

### 本地连接配置

```json
{
  "mcpServers": {
    "alas": {
      "url": "http://127.0.0.1:22267/mcp/sse"
    }
  }
}
```

### 云服务器或内网连接配置

```json
{
  "mcpServers": {
    "alas": {
      "url": "http://[IP_ADDRESS]/mcp/sse"
    }
  }
}
```

请将 `[IP_ADDRESS]` 替换为实际服务器地址或内网地址。

## MCP 工具列表

当前可用 MCP 工具共 18 个。

### 实例管理

| 工具名称 | 功能 |
| --- | --- |
| mcp_alas_list_instances | 列出所有实例 |
| mcp_alas_get_status | 获取实例状态 |
| mcp_alas_start_instance | 启动实例 |
| mcp_alas_stop_instance | 停止实例 |

### 任务管理

| 工具名称 | 功能 |
| --- | --- |
| mcp_alas_list_tasks | 列出所有任务 |
| mcp_alas_get_task_help | 获取任务帮助 |
| mcp_alas_trigger_task | 触发任务 |
| mcp_alas_get_scheduler_queue | 获取调度队列 |
| mcp_alas_clear_scheduler_queue | 清空调度队列 |

### 监控与信息

| 工具名称 | 功能 |
| --- | --- |
| mcp_alas_get_current_running_task | 获取当前运行任务 |
| mcp_alas_get_resources | 获取资源状态 |
| mcp_alas_get_config | 获取实例配置 |
| mcp_alas_get_recent_logs | 获取最近日志 |
| mcp_alas_get_screenshot | 获取截图 |

### 配置管理

| 工具名称 | 功能 |
| --- | --- |
| mcp_alas_update_config | 更新配置 |

### 维护工具

| 工具名称 | 功能 |
| --- | --- |
| mcp_alas_restart_emulator | 重启模拟器 |
| mcp_alas_restart_adb | 重启 ADB |
| mcp_alas_update_alas | 更新 ALAS |

## OCR 模型

本项目使用基于 PaddleOCR 的定制 OCR 模型，用于适配碧蓝航线界面字体和 Alas 截图场景。

感谢超算互联网提供算力支持。

相关项目：

[https://github.com/PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)

<p>
  <a href="https://arxiv.org/pdf/2507.05595">
    <img src="https://img.shields.io/badge/PaddleOCR_3.0-Technical%20Report-b31b1b.svg?logo=arXiv" alt="PaddleOCR Technical Report">
  </a>
  <img src="https://img.shields.io/badge/hardware-cpu%2C%20gpu%2C%20xpu%2C%20npu-yellow.svg" alt="Hardware">
</p>

### V1.0

| 项目 | 内容 |
| --- | --- |
| 支持语言 | zh-cn, en-us |
| 训练目标 | 针对碧蓝航线字体进行训练 |
| zh-cn 准确率 | 97% |
| en-us 准确率 | 98.6% |
| 已知问题 | zh-cn 存在边缘符号问题，en-us 可能出现负号问题 |
| 训练硬件 | 异构加速卡 BW 64G，NVIDIA Tesla A800 80G |
| 训练时间 | 2 小时 |

### V2.0

| 项目 | 内容 |
| --- | --- |
| 支持语言 | zh-cn, en-us |
| 训练目标 | 针对碧蓝航线字体与 Alas 截图特性进行训练 |
| 处理方式 | 灰度化 |
| zh-cn 表现 | 相对 V1.0 准确率降低 |
| en-us 准确率 | 99.8% |
| 已知问题 | en-us 基本无明显错误 |
| 训练硬件 | NVIDIA Tesla A800 80G |
| 训练时间 | 2 小时 |

### V2.5

| 项目 | 内容 |
| --- | --- |
| 支持语言 | zh-cn |
| 训练目标 | 修复 V2.0 中文模型问题 |
| 准确率 | 98.52% |
| 推理速度 | 约 10 ms |
| 训练硬件 | 异构加速卡 BW 64G，NVIDIA Tesla A800 80G |
| 训练时间 | 5 小时 |

## 功能来源与参考项目

本项目部分功能来自或参考以下项目：

- [Zuosizhu/Alas-with-Dashboard](https://github.com/Zuosizhu/Alas-with-Dashboard)
- [guoh064/AzurLaneAutoScript](https://github.com/guoh064/AzurLaneAutoScript)
- [sui-feng-cb/AzurLaneAutoScript](https://github.com/sui-feng-cb/AzurLaneAutoScript)
- [雪风源](https://gitee.com/wqeaxc/AzurLaneAutoScriptyukikaze21)

感谢以上项目和开发者提供的功能、思路与代码基础。

## 贡献者

由于本项目基于 AzurLaneAutoScript 及其社区分支继续开发，贡献者列表不仅包含本仓库的直接贡献者，也包含上游项目与相关分支中的原始贡献者。

感谢所有为 AzurPilot、原上游 AzurLaneAutoScript 及相关分支做出贡献的开发者。

<a href="https://github.com/wess09/AzurPilot/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=wess09/AzurPilot&max=1000" alt="AzurPilot Contributors">
</a>

感谢所有为启动器项目做出贡献的开发者。

<a href="https://github.com/wess09/alas-launcher/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=wess09/alas-launcher&max=1000" alt="Launcher Contributors">
</a>

## 开发说明

本项目基本完全是 VibeCoding 产物 不足之处请见谅

欢迎通过 Issue 或 Pull Request 反馈问题、提交修复或改进文档。

## 使用过的开发工具与模型

本项目开发过程中使用过多种 AI 模型与开发工具进行辅助。

### AI 模型

| 模型 | 模型 | 模型 |
| --- | --- | --- |
| Gemini 3 Flash | Gemini 3.1 Pro | Claude Opus 4.5 |
| Claude Sonnet 4.5 | MiMo V2.5 Pro | GPT 5.4 |
| GPT 5.3 Codex | Qwen 3 Max | DeepSeek v3.2 |
| Kimi K2.5 | GLM 4.7 | MiMo V2.5 |

### 开发工具

| 工具 | 工具 | 工具 | 工具 |
| --- | --- | --- | --- |
| Claude Code | Codex | Cursor | Antigravity |

## 许可证

本项目遵循原项目及相关上游项目的许可证要求。启动器项目遵循 GPL-3.0 协议开源。

使用、修改或分发本项目时，请同时遵守相关上游项目的许可证要求。
