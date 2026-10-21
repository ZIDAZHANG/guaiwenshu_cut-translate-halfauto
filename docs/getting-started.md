# 配置与运行

## 环境

Windows；已验证的解释器为 Python 3.10。桌面界面使用 tkinter。requirements.txt 记录整理时本机可用的依赖版本，不代表它们是各上游最新版。FFmpeg 可直接在 PATH 中，也可在界面指定；测试入口可复用 imageio-ffmpeg 的程序。

## 本机工具

需要自己安装或准备 MagiaTimeline、VoiceTransl / Whisper、Sakura 对应的模型与 llama-server。识别引擎与模型格式必须匹配：Whisper ggml 与 Faster Whisper CTranslate2 不能互换。需要字词时间时配置相应 DTW 模型。工具自动发现只是辅助，找不到时手动指定路径。

首次运行 `python scripts/run_gui.py`，选择视频或链接，检查外部工具配置并保存。本地配置位置是 `app/config/roast_pipeline.local.json`；下载功能还需要 yt-dlp 及可用网络。工作目录建议使用绝对路径，避免从不同当前目录启动时输出位置改变。

## 时间边界

- 画面与语音融合：以已对应的画面页组织原文，语音帮助校验。
- 画面台词边界：采用画面页边界，不能据此声称时间来自音频实测。
- 语音字词边界：采用验证后的原字词时间；缺边界时保留连接父句时钟并标记 CHECK，不按字符数猜时间。

语音不中断但画面换页时可用前两种。真实重复和未完句保留；不设单句长度、时长、固定阅读速度或必须停顿的规则。

## 命令行

从仓库根目录运行，例如：

```powershell
python app/tools/roast_pipeline.py local-file --config app/config/roast_pipeline.local.json --video-file "path/to/video.mp4" --timing-mode mixed
```

具体选项可运行 `python app/tools/roast_pipeline.py --help`。配置中的 `{root}` 指 `app/`，同目录 tools/config 的关系保留。

## 产物与恢复

任务目录下，`subs/zh_machine.srt` 是中文 SRT；`deliverables/final.ass`、`review.csv`、`quality_summary.json` 分别为字幕、复核表和质量摘要。`jianying_import/` 为视频、ASS 与复核表导入包。原文与片段身份记录在 subs，完整外部请求和错误证据在日志中。

相同任务可恢复，改变源文、时间、字典、模型或相关程序/配置会使受影响缓存失效。生成没有固定截止；如果模型循环，需要手动停止，已完成条目保留。

剪映 11.4 自动导入尚未验收。可以取消“自动导入剪映”，生成后手动导入 SRT 并依据 review.csv 审片。不要把检查通过等同于中文语义准确。
