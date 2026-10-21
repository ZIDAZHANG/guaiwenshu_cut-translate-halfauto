# 模块与调用关系

应用根为 app/，保留原程序的 tools/config 相对路径。

| 类别 | app/tools 中的模块 |
| --- | --- |
| 桌面与队列 | one_click_roast.py、roast_inbox.py |
| 流水线、恢复与产物 | roast_pipeline.py、task_state.py、artifact_validation.py、diagnostics.py |
| 字幕切分与对轴 | subtitle_segments.py、subtitle_axis_align.py |
| 原工具适配 | magia_timeline_adapter.py、magia_utf8_runtime.py、voicetransl_adapter.py |
| 翻译与字典 | sakura_client.py、translation_checkpoint.py、voice_dictionary.py、dictionary_editor.py、voice_common.py |
| 工具发现 | tool_discovery.py |
| 剪映和诊断 | jianying_bridge.py、jianying_ui_import.py、jianying_uia_probe.py、capture_app_window.py |
| 可选对齐 | forced_alignment.py，默认不启用，未合格结果不会作为可靠词时间 |
| 维护 | release_sync.py，需显式目标目录及写入参数，运行前先看计划 |

流水线为 INPUT → TIMELINE → ASR → SEGMENTATION → TRANSLATION → FINAL_OUTPUT → NLE_EXPORT。切分主记录保留父语音、原文范围、画面、词时间与身份；翻译接回相同身份，而非按输出条数猜配。

tests/ 单独存放回归测试，由 scripts/run_tests.py 设置导入路径。build/windows.spec 打包桌面入口，scripts/build_windows.py 把运行源码和通用配置附在 dist/ 中。EXE 不嵌入用户视频或模型。
