# 升级实施决策台账（W00 冻结）

计划依据：`docs/architecture-audit/08-complete-upgrade-plan.md` 版本 1.1（2026-10-25）。本文件记录 W00 冻结的基线事实、原样采纳的决策、执行环境与计划假设的差异及其处理方式。变更单编号沿用计划第 0.2 节格式；W00 **没有**提出任何变更单。

## 1. 冻结基线

| 项 | 值 |
| --- | --- |
| 基线提交 | `1a9a0104defbd9881db3f9eded017ad7b1f96edc`（2026-10-28，仅新增审计文档） |
| 业务代码提交 | `93df2625f5bfa71e8dd9bb897beac3d01a730b10`（2026-10-22）；1a9a010 没有改动 app/tests/scripts/build |
| 升级分支 | `upgrade/subtitle-workflow-v2`，从 1a9a010 建立 |
| 发布清单核对 | `docs/source_manifest.json` 51 条 `published_sha256` 与当前文件逐一相同（0 不符、0 缺失）。清单中的 `source_sha256`（fix_stage 侧）只能按清单采信，执行机上没有 fix_stage |
| 旧测试基线 | 22/22 通过（隔离 venv，声明依赖 + PyYAML 6.0.3）；未装 PyYAML 时 12 个失败，见 `baseline.json` |
| 旧准确性结论登记 | 按审计报告 01：`accuracy_accepted=false`、`native_ui_accepted=false`；35 秒 mixed/speech 各 15 条、13 条待复核、3 处术语未遵循；零源字符遗漏只是结构覆盖证明。**本轮未重跑，也无法在执行机上重跑**（素材不在本机） |

## 2. 原样采纳的决策

D01–D20、FR01–FR16、QC01–QC23、W00–W10、T01–T09、B01–B11 全部按计划 1.1 采纳，编号索引见 `traceability.json`。特别重申与执行机环境直接相关的几条：

- **D06/D20**：默认引擎仍是 whisper.cpp GGML large-v3 + VAD、MagiaTimeline、Sakura-GalTransl-7B-v3.7 via llama-server。执行机上存在的 faster-whisper（CTranslate2）、Sakura-13B-LNovel-v0.9b 和 2024-02 版 llama.cpp `server.exe` **不是**这些资源，W00 只登记，不替换、不作为新后端。
- **D12/D18**：资源外置、仓库不捆绑二进制/模型；执行机资源只以逻辑 resource_id + 本机私有路径绑定登记。
- **第 12.2 节**：主运行时目标 CPython 3.13；执行机只有 3.10.6，见第 3 节 B02 处理。

## 3. 执行环境与计划假设的差异

计划的事实基线来自维护者本机（`LOCAL_WORKSPACE`、`LEGACY_ROOT`、`EXTERNAL_TOOLS_ROOT`）。W00 由另一台机器上的测试侧账号执行，差异与处理如下：

| 编号 | 差异 | 处理 | 影响范围 |
| --- | --- | --- | --- |
| ENV-01 | 执行机没有 `fix_stage`、`implementation_20261010`、`candidate_20261011`、`LEGACY_ROOT` | W00 任务 1 的"比较 fix_stage AST"改为核对 `source_manifest.json` 的 51 条发布哈希（全部一致）；fix_stage 侧哈希按清单采信并标 `reused_from` | 不阻塞 W01–W08；W09 需要维护者机器或其提供素材 |
| ENV-02 | S01/S02/S04/S07 素材不在执行机 | 只登记计划中的路径别名与 manifest hash；T07/T08 的真实素材验证标 BLOCKED（B08/B09 相关） | W09 |
| ENV-03 | 执行机没有 whisper-cli/ggml-large-v3/VAD、MagiaTimeline、llama-server、Sakura-GalTransl-7B-v3.7、yt-dlp、ffprobe、Subtitle Edit、Aegisub | 在 `resources-inventory.json` 中以 `present=false` 登记为必需资源缺失；按 B01/B05 处理，不自动下载、不换后端 | W03 真实引擎调用、W07 ffprobe、W09 |
| ENV-04 | 执行机只有 CPython 3.10.6（计划记录维护者为 3.10.11），没有 3.13 | 3.10 venv 仅用于旧测试基线复现；B02 按第 12.2 节在 W01 确定 3.13 版本并单独申请安装授权 | W01 冻结包 |
| ENV-05 | 执行账号对上游仓库只有读权限 | 提交留在本地分支；推送与 PR 需负责人授权（写权限或接受分叉 PR） | 交付方式 |
| ENV-06 | `upgrade_stage` 目录名 | 隔离 checkout 实际位于 `LOCAL_WORKSPACE/repo-roast`，作用等同计划第 4.1 节的 `upgrade_stage/`；开发用 DATA_ROOT 将建在仓库外 `LOCAL_WORKSPACE/roast-data-dev`，不写用户文档目录 | 无 |
| ENV-07 | 执行机 GPU 为 NVIDIA RTX 4080 SUPER 16 GiB（驱动 617.42） | 只登记；维护者历史证据为 Intel Iris Xe/Vulkan。B04 的 profile 实测仍留 W09，不能用执行机结果替代维护者机器 | W09 |

## 4. W00 期间确认的客观事实

- **PyYAML 缺口属实**（审计 P1）：`voice_dictionary.py`、`magia_timeline_adapter.py` 无条件 `import yaml`，`requirements.txt` 未列；干净 3.10 环境下 10 个测试因 `No module named 'yaml'` 失败、1 个因 `pywinauto`（legacy 组）失败、1 个因前者连带失败。W01 按计划显式声明 PyYAML 6.0.3。
- **shell=True 命令模板属实**（审计 P1）：`roast_pipeline.run_command` 以 `shell=True` 执行格式化后的字符串；W02 按第 6.3/10.5 节改 argv。
- **词典待确认条目**（B06）：`app/config/dict_gpt.txt` 中 `ラモーヌ`、`ラモさん` 两条标 `[待确认]`；`translation_glossary.json` 的 `アルトレ` 标"根据上下文选择"。W00 如实登记，不替用户决定译名。
- **资源来源**（B01）：执行机上的 faster-whisper 与 Sakura 13B 来自 GalTransl-ASMR 20241110 整合包，均为第三方整合分发；它们对 D06 默认工作流无直接作用，仅作本机资源登记。

## 5. 负责人需要提供或决定的事项（不是变更单）

| 项 | 需要什么 | 对应阻塞 |
| --- | --- | --- |
| 许可证 | 回答 `license-options.md` 第 5 节三个问题 | B10 |
| 默认引擎资源 | 提供或授权获取 whisper.cpp（whisper-cli + ggml-large-v3 + silero VAD）、MagiaTimeline、llama-server + Sakura-GalTransl-7B-v3.7、ffprobe、yt-dlp 的官方来源；或在维护者机器上运行 W03/W09 | B01 |
| Python 3.13 | 授权安装 W01 选定的 3.13 补丁版 | B02 |
| 验收素材 | S01/S02 原片或在维护者机器上执行 W09 | B08 |
| 编辑器 | 授权安装 Subtitle Edit 与 Aegisub 指定版本 | B05 |
| 干净测试机 | 提供 | B09 |

## 5a. W01 记录（2026-11-08）

| 编号 | 决定/事实 | 依据 |
| --- | --- | --- |
| RT-01 | 主运行时选定 **CPython 3.13.17**（2026-10-21，最后一个带 Windows x64 安装包的 3.13 版本；3.13.18 起只有源码）。安装包 URL、python.org 公布的 SHA-256、Sigstore/签名地址记录在 `runtime-lock.json`；**尚未下载安装**，需负责人授权（B02） | 计划 12.2 选版规则；python.org 发布页 |
| RT-02 | 依赖分三组锁：核心（PyYAML 6.0.3、yt-dlp 2026.7.4）、legacy（pywinauto、pywin32、Pillow）、dev（imageio-ffmpeg）；`requirements.txt` 仍等于全量安装以兼容旧文档。3.13 带哈希的 transitive lock 因无 3.13 解释器 BLOCKED；3.10 开发 venv 的 freeze 记录在 `requirements-lock-dev-3.10.txt` | 计划 12.2 |
| IF-01 | `app/tools/upgrade/` 新增 contracts / config / storage / resource_catalog / cli；39 种记录类型 + 资源声明文件 3 种，全部导出到 `schemas/`（`scripts/export_schemas.py --check` 可验证一致） | 计划第 5、6 节 |
| IF-02 | 引擎参数（翻译/识别/OCR/媒体/下载）保存在 `settings/engine.json`，与 `settings/app.json`（AppConfig）分开；两者一起进入 ConfigSnapshot | AppConfig 契约字段固定，ConfigSnapshot 单列 `engine_settings` |
| IF-03 | 资源声明文件 `settings/resources.local.json`：`resource_id → {kind, absolute_path, files, expected_content_sha256, …}`。首次 `register` 冻结内容身份；路径变化且内容相同只改绑定；内容变化报 RESOURCE_CHANGED 且 resolve 拒绝 | 计划 4.2、10.3 |
| IF-04 | 能力探针只执行版本/帮助参数（ffmpeg `-version`、whisper `--help`、llama-server `--version`），未探到的能力保持 `unknown`，不适用为 `not_applicable` | 计划 6.2、W01 任务 |
| CO-CAND-001 | CLI 比第 12.1 节清单多了 `config init`、`config migrate`、`resources register`、`resources rebind` 四条命令。它们是第 3.4/4.2/9.6 节已规定行为（首次选数据根、旧配置迁移、资源登记/重新定位）的命令行表达，不新增产品行为；按 0.2 节格式登记为变更单候选，请负责人确认是否纳入 12.1 清单 | 计划 0.2、12.1 |
| EV-01 | 旧 22 测试 + 新 3 个 T01 测试在 3.10 venv 全部通过；3 个 T01 测试在**未装任何第三方包**的系统 3.10 下也通过（v2 核心不依赖 PyYAML/pywin32） | `validation-ledger.json` T01 |

## 5b. W02 记录（2026-11-08）

| 编号 | 决定/事实 | 依据 |
| --- | --- | --- |
| IF-05 | 错误码表新增 `STAGE_FAILED`（阶段实现抛出异常）与 `STAGE_UNAVAILABLE`（本构建没有该阶段实现）。10.4 的集合是"最低集合"，新增两码只用于诚实标注实施进度，不改变已有码含义 | 计划 10.4 |
| IF-06 | 阶段实现通过 `upgrade/stage_registry.py` 注册；W02 的默认注册表为空，真实请求在 INPUT 处以 STAGE_UNAVAILABLE 失败并可 resume。不放占位实现、不模拟成功 | 计划 0.1"范围外不以后续完善包装成已完成" |
| IF-07 | 取消正在运行的请求用 `queue/cancel/<request_id>` 标记文件通知执行器（GUI 只写文件、不碰 PID）；处理后删除标记并在 envelope.last_error 记 PROCESS_CANCELLED | 计划 10.2/10.5"GUI 不直接控制外部任意 PID" |
| IF-08 | task.json 中指向阶段 manifest 的 ArtifactRef 路径以**项目目录**为基准（`runs/<run>/stages/<stage>/<attempt>/manifest.json`），这样同一项目内跨 run 复用已提交阶段不需要 `..` | 计划 4.3 托管路径规则 + 10.3 复用 |
| IF-09 | 子进程优雅退出请求在 Windows 用 CTRL_BREAK；无控制台的引擎收不到时，3 秒宽限后由 Job Object（KILL_ON_JOB_CLOSE）终止整个所有权范围；没有 Job Object 时退回旧 `terminate_process_tree`（Toolhelp 快照），绝不按进程名杀 | 计划 10.5 |
| CO-CAND-002 | CLI 再增 `inbox intake --config --inbox`（旧收件箱接管入口）；`config init` 的 `--config` 改为可选，默认写 `<data-root>/settings/app.json`，使 `queue run --data-root` 等 12.1 命令能按 4.2 布局找到配置 | 计划 10.2 inbox 兼容、12.1 |
| EV-02 | T02 20 用例 PASS；全套 27/27（3.10 venv） | `validation-ledger.json` T02 |

## 6. 不做的事（W00 重申）

不删除 VoiceTransl/剪映相关源码，不为商用换模型，不更新全局 Python，不重写分段算法，不下载模型，不修改 D 盘旧目录、原媒体、旧词典，不在用户文档目录写入任何文件。
