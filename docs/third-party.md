# 外部工具与许可边界

本项目编排现有工具，模型与第三方二进制不包含在本仓库。使用前分别检查上游下载渠道、许可、硬件要求和兼容格式。

| 工具 | 用途 |
| --- | --- |
| FFmpeg / imageio-ffmpeg | 媒体转换、提音、测试合成素材 |
| yt-dlp | 视频下载 |
| VoiceTransl / Whisper | 日文识别及原工作流兼容 |
| MagiaTimeline | 画面字幕轴与 OCR |
| Sakura / llama.cpp | 本地日译中 |
| 剪映 | 后续编辑与审片 |
| PyInstaller | Windows 打包 |
| pywinauto / pywin32 / Pillow | 可选桌面桥接及诊断 |

可选 Qwen3 强制对齐依赖单独准备，默认关闭，不随 requirements.txt 自动安装。原 VoiceTransl 条件/情景词典因 direct SRT 元数据不完整而跳过并警告；普通 native 字典与用户 JSON 词典保留。

本次上传未替作者指定新的开源许可证，也未把第三方工具许可证改成项目许可证。
