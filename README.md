# guaiwenshu_cut-translate-halfauto

字幕烤肉助手：面向 Windows 的半自动日文视频打轴与翻译工具，沿用 VoiceTransl / Whisper、MagiaTimeline、Sakura 和剪映的本地工作流。

支持画面、语音和融合三种时间边界，处理连续语音跨字幕换屏；保留原文、时间依据与复核报告，提供任务队列、阶段恢复、字典和剪映导入包。

这是 2026-10-12 本机可用版的源码整理，分段版本为 8。听写、专名与译义仍需人工校对，完整 GUI 操作和剪映 11.4 自动导入尚未验收。

## 目录

```text
app/
  tools/       桌面程序、流水线、字幕处理和外部工具适配器
  config/      通用示例配置、翻译要求与字典 seed
tests/         22 个独立测试脚本与合成素材冒烟测试
scripts/       启动、测试、Windows 打包入口
build/         PyInstaller 构建说明文件
docs/          安装使用、代码结构、验证范围、变更与来源说明
```

## 快速开始

使用 Windows 和 Python 3.10（包含 tkinter）：

```powershell
python -m pip install -r requirements.txt
python scripts/run_gui.py
```

在界面中配置自己电脑上的 FFmpeg、MagiaTimeline、VoiceTransl / Whisper、Sakura 模型及运行程序。运行文件和模型需自行按各自许可准备，本仓库不附带它们。初次使用先读 [配置与运行](docs/getting-started.md)。

需要从命令行配置时，将 `app/config/roast_pipeline.example.json` 复制为同目录 `roast_pipeline.local.json`，填入本机路径。私有配置已被 `.gitignore` 排除。

```powershell
# 回归测试（合成素材，不调用真实识别/翻译模型）
python scripts/run_tests.py

# Windows 打包
python -m pip install -r requirements-build.txt
python scripts/build_windows.py
```

打包结果在 `dist/`；保留 EXE、tools 和 config 整个目录。测试结果在 `test-results/`，均不提交。

## 进一步阅读

- [配置、运行和产物](docs/getting-started.md)
- [模块与调用关系](docs/architecture.md)
- [验证范围与已知问题](docs/verification.md)
- [全部功能变化与保留限制](docs/changes-and-constraints.md)
- [源码整理范围与对应清单](docs/publication.md)
- [外部工具与许可边界](docs/third-party.md)

原视频、真实任务字幕、草稿、模型、第三方安装文件、运行日志、本机路径配置和旧构建不在源码仓库内。
