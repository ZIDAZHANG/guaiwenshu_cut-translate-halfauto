# 源码整理记录

日期：2026-10-22。来源为 2026-10-12 的第 8 轮本机可用版（分段版本 8）。

## 收录与调整

- app/tools：23 个程序/维护模块；22 个业务与运行模块的 Python AST 与原源码一致。release_sync.py 仅将部署清单中的测试去掉，测试已移至 tests/。
- tests：22 个测试脚本；修改应用根定位，合成媒体配置不再读取私有 local.json，FFmpeg 与测试输出位置改为可移植路径。
- app/config：通用示例、13 条翻译风格要求和字典 seed；没有上传任何实际机器路径配置。
- scripts、build、docs：增加启动/测试/构建入口、目录和使用文档。
- 每个原始源文件与公开文件的 SHA-256、路径对应及调整原因见 source_manifest.json。换行统一为 UTF-8 / LF。

## 排除内容

不收录原视频、真实任务字幕、剪映草稿、模型、第三方 EXE/DLL、原始 HTTP/SSE 日志、个人配置、环境文件、旧构建、备份和机器专用交接材料。测试代码中的 SECRET、example.test 和模拟文件是公开的虚构测试夹具。

## 本轮检查

- `python scripts/run_tests.py`：22 个测试脚本全部成功，包括合成媒体冒烟、阶段缓存、HTTP/SSE、队列和自有子进程停止回归；没有调用用户真实模型或剪映 UI。
- 48 个 Python 文件通过 AST 解析；桌面入口 `--help` 正常。
- `python scripts/build_windows.py`：PyInstaller 构建成功；dist 包含 EXE、23 个运行源码模块、6 个通用配置/seed 文件。构建结果保留本机，不上传 Git。
- 发布前校验源码映射 SHA-256、运行模块 AST 和 Git 清单；排除个人配置、媒体、模型、构建、运行日志及测试产物。

这些是分类整理后的代码/构建检查，不扩大旧版的 GUI、真实模型或剪映验收范围。
