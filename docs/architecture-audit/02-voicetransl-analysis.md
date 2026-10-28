# VoiceTransl 身份、职责与整合可行性

审计日期：2026-10-25。证据等级沿用 [01](01-current-state.md)。本报告区分本机安装、固定版本研究源码、当前上游；没有启动或修改 VoiceTransl。

## 1. 先确认是哪一个项目

**可确认是 [shinnpuru/VoiceTransl](https://github.com/shinnpuru/VoiceTransl)，基于 GalTransl 的桌面字幕/翻译项目。** 它不是一个叫 VoiceTransl 的统一识别模型，也不是本项目直接调用的远端翻译服务。

| 对象 | 核对结果 |
| --- | --- |
| 本机安装标识 | `EXTERNAL_TOOLS_ROOT/VoiceTransl-v1.21-win-sakura`；当前配置的模型/原生翻译资源指向此目录。目录版本为 v1.21；不能仅凭目录名证明 EXE 精确源码版本。 |
| 固定研究基线 | 官方tag v1.21指向 `788a25b59e8f8c0d5556ab47c18a4a9d17e3931f`，本次GitHub API已核对。已有部分源码在 `LOCAL_WORKSPACE/.research/VoiceTransl_readonly_20261002/v1.21`；不含完整源码和完整依赖，也没有 .git。 |
| 另一研究快照 | v1.30，`b5f7e5038763aeb3420ac872c0bd191112f92227`，只作差异研究，不能算本机已升级。 |
| 本次官方复核 | GitHub API 确认官方 main 为 `b70799cc2ccf80fbcaa566634bcc6f07fd16cc88`（2026-10-17）；[该版 README](https://github.com/shinnpuru/VoiceTransl/blob/b70799cc2ccf80fbcaa566634bcc6f07fd16cc88/README.md) 已描述 CrispASR/Qwen3-ASR 工作流。 |
| 开源许可 | 固定 v1.21 的 [LICENSE](https://github.com/shinnpuru/VoiceTransl/blob/788a25b59e8f8c0d5556ab47c18a4a9d17e3931f/LICENSE) 为 GPL v3 文本；README 同时写有禁止商业用途，存在声明不一致，见第 7 节。 |
| 本地修改版 | 有本项目自己的适配器、ASCII 路径资源副本和 Magia 私有兼容补丁；没有证据表明完整 VoiceTransl GUI 被自行重编译。VoiceTransl 本机 EXE 精确对应提交及是否含上游外补丁仍未知。 |

身份与历史证据见 2026-10-02 专项复盘（本地证据：`docs/PROJECT_REVIEW_20261002_字幕错位重复与VoiceTransl.md:121`，未公开）；该文描述的旧 JSON 翻译协议等缺陷已被 v8 修改，不能直接沿用为当前结论。

## 2. 当前项目实际上怎样使用它

**[代码] 正常流程绕开 VoiceTransl 主 GUI，也不运行原生 translate.exe。**

1. 主程序先用自配 FFmpeg 提取 WAV。
2. [recognize](../../app/tools/voicetransl_adapter.py)（本地证据：`fix_stage/tools/voicetransl_adapter.py`；原审计源码第75行） 运行 `LEGACY_ROOT/runtime/whisper/whisper-cli.exe`，模型指向 VoiceTransl 安装目录中的 GGML。
3. [translate](../../app/tools/voicetransl_adapter.py)（本地证据：`fix_stage/tools/voicetransl_adapter.py`；原审计源码第273行） 读取自己维护的片段、词典和提示词，启动/复用 `LEGACY_ROOT/runtime/llama/llama-server.exe`，通过本地 OpenAI 兼容 HTTP 接口翻译。
4. 字幕身份、分段、错误处理、恢复和最终输出由本项目负责。

既有专项核对的 D 盘 Whisper/llama-server 与 G 盘 VoiceTransl 对应文件哈希相同；配置事实和组件来源相符。配置里的 `translation.executable=.../translate.exe` 仍被保留/探测，不表示默认 translate 路径实际执行它。VoiceTransl 主要提供 **现成引擎包、模型、词典格式及兼容资源**。

两种接口不可混淆：

- llama-server `/v1/chat/completions` 是当前实际翻译接口。
- v1.21 的 GalTransl/server.py（本地证据：`.research/VoiceTransl_readonly_20261002/v1.21/GalTransl/server.py:2858`，未公开） 实现 `/api/jobs`、`/api/jobs/<id>`、项目/缓存/问题接口；`serve()` 默认 loopback:12333（3263 行）。它提交的是准备好的翻译项目，非直接“视频到成片”接口。本项目未接入，打包 EXE 的可启动入口、安装版本支持程度和稳定性未实测。

原生 translate.parse_arguments（本地证据：`.research/VoiceTransl_readonly_20261002/v1.21/translate.py:46`，未公开） 接收项目目录或 YAML 与翻译模板；不能把它当成现有自有 SRT adapter 的同参数替换物。

## 3. ASR、模型和时钟

| 能力 | 本机当前路径 | VoiceTransl研究源码/上游能力与边界 |
| --- | --- | --- |
| ASR模型 | GGML `ggml-large-v3.bin`，语言 ja；当前运行的是 whisper.cpp CLI | v1.21 MainWorker._process_single_audio（本地证据：`.research/VoiceTransl_readonly_20261002/v1.21/app.py:2641`，未公开） 按模型名前缀分支 whisper 与 faster-whisper。存在分支不等于本机用了后者。 |
| faster-whisper | adapter 92–93 行明确拒绝把 GGML 当 CTranslate2 模型 | 独立后端需自己的模型、CTranslate2/运行环境、词时间/VAD语义测试；不能只改 engine 字符串。 |
| 模型下载/管理 | 主程序检查配置路径存在，通过工具发现选择资源；未见生产自动模型下载/校验注册中心 | v1.21 GUI 1114 行从 whisper、whisper-faster 目录枚举本地模型。完整下载/更新机制未从本地部分源码建立闭环，不承诺自动下载/迁移。 |
| VAD | recognize 116–121 行传 `--vad --vad-model`；缺失启用模型明确失败；未配置时找 EXE 旁 Silero v5.1.2 | 已有 VAD 但不是说话人识别，也不能证明所有语音被识别。 |
| 字词/分段时间 | `-ojf -dtw large.v3`，保存原生 JSON 和 token 对应；v8 验证坐标 | whisper.cpp token/DTW并非人工确认的强制对齐金标。日文 BPE 字节需无损重组，不能按字符数猜词时长。 |
| 时钟恢复 | VAD父句/词时钟冲突时，保存两次完整证据，同音频同模型无 VAD 再识别，只有合格词时间才选择 | 这是本项目 adapter 的功能，不能当作直接抽走上游 ASR 后自然保留的能力。见 [150–224 行](../../app/tools/voicetransl_adapter.py)（本地证据：`fix_stage/tools/voicetransl_adapter.py`；原审计源码第150行）。 |
| 长音频切块 | 当前 `segment_enabled=false`，GUI保存也固定关闭，adapter 未按 segment_minutes 执行分块 | v1.21 GUI有 `_split_audio`、`_merge_segment_translations`（2689/2726行），依赖GUI配置、FFmpeg和翻译项目；没有被本项目接入。 |
| 说话人识别 | 当前adapter没有说话人 diarization 输出/模型/任务阶段 | 当前材料不能确认完整安装包所有可选功能是否支持；不得将原生句子 speaker 字段当作声纹分离。 |
| GPU/CPU | 独立 EXE 承担推理，翻译 server 有 `gpu_layers`，不依赖主Python导入torch；本机目录有ggml-cpu/ggml-vulkan DLL | 历史真实Sakura日志已记录Intel Iris Xe/Vulkan0和29/29层offload，证明曾走GPU路径；本轮未重测驱动/峰值。Whisper的实际GPU执行仍需对应识别日志，不由翻译GPU推定。 |
| 脱离GUI | 已经实现且有真实任务证据 | 原生GUI可留给用户独立操作；无需为“去GUI”先复制整个源码。 |

本次只读 stat：large-v3 为 3,095,033,483 字节（约2.88 GiB），Silero 为885,098字节；Sakura GGUF为6,254,196,608字节（约5.82 GiB）。这些是磁盘文件大小，不是峰值RAM或最低显存。

GPU证据：sakura_server.log第21行（本地证据：`implementation_20261010/jobs/sample_s01_v8_user_glossary_candidate_mixed/logs/voicetransl/sakura_server.log:21`，未公开），127行offload、129行model buffer约5532MiB、149行KV buffer896MiB。日志还报15457MiB free，不能将它称为独立物理显存容量，更不能将两个buffer之和等同整机峰值。

## 4. 翻译、上下文和词典兼容

### 4.1 已经使用的处理链

源SRT和分段身份 → native译前替换 → 相关GPT词条/用户词典 → 用户风格及父句上下文 → Sakura正文请求 → 行数/完整响应验证或拆单句重试 → native译后替换 → 可定位原词替换 → 术语/日文残留/不确定标记 → SRT与身份sidecar。

证据：[translate 295–359行](../../app/tools/voicetransl_adapter.py)（本地证据：`fix_stage/tools/voicetransl_adapter.py`；原审计源码第295行）、[翻译后处理513–598行](../../app/tools/voicetransl_adapter.py)（本地证据：`fix_stage/tools/voicetransl_adapter.py`；原审计源码第513行）、[plain_translation_payload](../../app/tools/sakura_client.py)（本地证据：`fix_stage/tools/sakura_client.py`；原审计源码第365行）。

- 存储：native `dict_pre.txt`、`dict_gpt.txt`、`dict_after.txt`；用户 `translation_glossary.json` 支持 flat/带default等结构；风格为 `translation_style_prompt.txt`。当前 seed 在 fix_stage/config，原用户词表不得合并覆盖。
- [load_native_dictionary](../../app/tools/voice_dictionary.py)（本地证据：`fix_stage/tools/voice_dictionary.py`；原审计源码第44行） 处理制表符、转义、GPT箭头/备注、前缀`^^`和单次`1^`。按长词优先应用普通替换。
- `pre_src/post_src/pre_dst/post_dst/pre_jp/post_jp/pre_zh/post_zh/mono/diag` 等条件/情景语法被跳过并警告；这不是完整 GalTransl 词典兼容。direct SRT 缺少原生句子情境元数据。
- 上游固定版 [GalTransl/Dictionary.py](https://github.com/shinnpuru/VoiceTransl/blob/788a25b59e8f8c0d5556ab47c18a4a9d17e3931f/GalTransl/Dictionary.py#L34) 确有上述条件/情景词典，依赖CSentense，并有条件组合/前后处理语义；不能用普通字符串替换宣称等价。
- [native_glossary_prompt](../../app/tools/voice_dictionary.py)（本地证据：`fix_stage/tools/voice_dictionary.py`；原审计源码第99行） 只注入相关词条；提示词注入不能保证模型遵循。源文本中的错字也可能使词条不命中。
- [ensure_required_terms](../../app/tools/voice_dictionary.py)（本地证据：`fix_stage/tools/voice_dictionary.py`；原审计源码第218行） 当前只原位替换可确认原词；缺失目标交给 review，**没有旧版句尾追加译名行为**。
- `trainer_context` 等上下文分支是有限启发式，不能当作完整世界书或人物关系推理。
- 当前客户端保留完整父句/同批参考；已在待译正文完整出现的父句不重复塞入system，所有审计参考仍保存。画面OCR自动注入翻译参考的退化实验已回退，不是生产默认能力。

### 4.2 重试、批量、缓存和完整性

[translate_batch](../../app/tools/sakura_client.py)（本地证据：`fix_stage/tools/sakura_client.py`；原审计源码第412行） 默认最多三次尝试（初次+2次重试）；多行cue单独请求；普通单行返回数量正确才按顺序对应，记录明确为“非语义验证”。数量不一致保存完整响应，再逐条请求，不猜缺行归属。

[translation_signature](../../app/tools/translation_checkpoint.py)（本地证据：`fix_stage/tools/translation_checkpoint.py`；原审计源码第15行） 包括原文、提示词、词典、模型/适配器及native资源签名；完整条目即时保存，响应断流不接成正式结果。默认生成没有固定时间/输出token截止，模型循环可能需要用户停止；不能为了得到完成状态丢弃台词或静默截断。

[start_server](../../app/tools/sakura_client.py)（本地证据：`fix_stage/tools/sakura_client.py`；原审计源码第158行） 复用已有服务时验证模型名称，自启服务仅loopback并只关闭自己拥有的进程。同名不同权重仍不能由 `/v1/models` 名称证明内容身份，应在未来清单补强。

### 4.3 上游隐藏职责，不应遗漏

固定v1.21源码确认还包括：

| 能力 | 证据 | 本项目使用情况 |
| --- | --- | --- |
| 音频提取、视频硬/软字幕合成、剪切、音频配图 | app.py synth/clip/audiosynth（本地证据：`.research/VoiceTransl_readonly_20261002/v1.21/app.py:2509`，未公开）、3070行提音 | 本项目提音已独立；合成/剪切未走这些GUI函数。 |
| SRT转项目JSON、翻译后SRT/LRC/双语合并 | srt2prompt.py（本地证据：`.research/VoiceTransl_readonly_20261002/v1.21/srt2prompt.py`，未公开）、prompt2srt.py（本地证据：`.research/VoiceTransl_readonly_20261002/v1.21/prompt2srt.py`，未公开）、app 2726行 | 当前主流程不依赖该JSON往返，不必为了兼容原生CLI增加同类转换。 |
| 原生译前/后处理、对话分析与插件钩子 | LLMTranslate.preprocess_trans_list/postprocess_trans_list（本地证据：`.research/VoiceTransl_readonly_20261002/v1.21/GalTransl/Frontend/LLMTranslate.py:244`，未公开） | 自有adapter只复用词典子集；不能声称继承全部插件/情景规则。 |
| 分块、缓存、问题检查、项目恢复 | 同文件318/439/456行及postprocess流程 | 本项目有自己的TaskState/translation_checkpoint；两套缓存不是可互换数据库。 |
| Sakura原生上下文、退化检测、重试和cache | CSakuraTranslate（本地证据：`.research/VoiceTransl_readonly_20261002/v1.21/GalTransl/Backend/SakuraTranslate.py:25`，未公开），120/253/298/382行 | 值得对照，但不能将上游修复/填原文等策略默认为适合本项目。 |
| 下载、人声分离、总结、批量GUI管理 | app `vocal_split`2428、`summarize`2457、`run`2802及README | 未见主流水线调用；不能据README说已整合。 |

未发现上游这些职责是当前烤肉助手不可替代的隐藏依赖。真正必须保留的是已经投入使用的识别/翻译语义、词典子集、时钟恢复和证据机制。

## 5. 按能力比较四种整合方式

下表“保留”指保留**现有 CLI/HTTP 调用**，不是重新依赖 GUI。成本是相对判断，未经实际开发工时测量。

| 能力 | 1 保留现有调用 | 2 直接内部模块 | 3 提取适配源码 | 4 独立库/工具实现 |
| --- | --- | --- | --- | --- |
| ASR+VAD | 低成本，沿用GGML/真实回归；锁定EXE参数与版本 | 调GUI worker会引入Qt、全局cwd、配置和进程注册；收益低 | 抽取模型分支看似小，时钟/路径/进程仍需自维护 | 独立whisper.cpp最接近现状；faster-whisper需新模型和运行时，作为第二adapter比较后再选 |
| 字词时间/时钟恢复 | 保留当前旁路、VAD重试最稳 | 上游返回字段与本地规范记录不同，仍需adapter | 易漏BPE字节、父句坐标、失败证据；维护成本高 | 独立强制对齐可辅助，但当前Qwen长片失败；不能默认升级 |
| 普通native词典 | 当前实现已够用且可迁移 | 完整原生字典依赖句子类型和project config | 可小范围提取但引入GPL来源追踪/兼容测试 | 维持现有自有parser和测试最小；不要“简化”丢失前缀/转义/备注 |
| 条件/情景词典 | 当前跳过，必须明确告知 | 原生完整项目接口可能更合适，需要补句子元数据 | 隐含依赖多，不推荐硬抽 | 独立重做成本中高，需要真实条件词条样本；暂不承诺全兼容 |
| Sakura翻译/上下文 | 当前HTTP+正文协议/留证最容易回滚 | 可评估原生项目CLI/API保完整后处理；需要入口实测 | CSakuraTranslate依赖BaseTranslate、配置、句子、cache/chatbot等，抽取并不轻 | llama.cpp继续独立提供HTTP；新的模型/翻译API只在有质量收益时加adapter |
| 批量/重试/缓存 | 当前有真实恢复证据，优先保留 | 双调度/双缓存需桥接，不能直接import替换 | 复制会产生两套易漂移的状态语义 | 对现有机制补充即可，无需再引入队列框架 |
| 提音/合成/字幕转换 | 提音已用FFmpeg，原生GUI无需参与 | GUI方法依赖控件和cwd，收益很小 | 抽FFmpeg拼串代码还要重写转义/错误管理 | FFmpeg+现有SRT/ASS模块成本最低；新增成片出口即可 |
| 其他下载/分离/总结 | 保留VoiceTransl供用户手动用，不占主流程 | 原生CLI/API入口逐项确认后再接 | 依赖和许可证范围大 | 有明确需求才选独立工具，本轮不扩范围 |

## 6. 开发、维护、性能与扩展取舍

| 维度 | 保留CLI/HTTP | 直接内部模块 | 抽源码 | 独立后端 |
| --- | --- | --- | --- | --- |
| 开发成本 | 最低，补契约/能力探测 | 中高，要装齐上游运行时并适配版本 | 高，需追踪传递依赖与补丁 | 中，whisper.cpp近似；faster/新OCR需重新验收 |
| 维护风险 | 参数漂移可通过版本固定/探针发现 | 私有类/对象/配置改变直接传播 | 成为自维护fork，跟进上游最难 | 库接口更明确，仍须固定版本和模型 |
| 兼容性 | 现有GGML、GGUF、词表/缓存保留最好 | 原生项目完整，但自有cue_id需桥接 | 易漏条件词典、上下文、cache | 明确转换策略；GGML不通用到CTranslate2 |
| 性能 | 不会因保留外部EXE自然变慢；可测批次服务复用 | 推理模型/后端不变时无固有加速 | 删除GUI主要减少壳体，不会自动减权重/VRAM | 可量化/批处理，但需同音频同配置实测，不能引用上游benchmark当本机收益 |
| 许可 | 外部工具/权重各自核对，捆绑有分发义务 | 直接链接/组合需要评估GPL兼容 | GPL代码复制/修改来源和对应源码义务更直接 | 按库及模型独立许可选型，不能只看主库MIT |
| 扩展性 | 现有adapter可增加第二实现 | 受上游内部架构约束 | 变成fork路线 | 高，但不应一次装多个重后端 |

独立候选参考：[whisper.cpp](https://github.com/ggml-org/whisper.cpp)；[faster-whisper官方说明](https://github.com/SYSTRAN/faster-whisper)确认其基于CTranslate2、使用不同模型格式和运行依赖。AMD/Intel机器不能直接照搬NVIDIA CUDA配置。没有本机比较前不指定它优于现有whisper.cpp。

## 7. 许可证与分发边界

1. VoiceTransl 固定v1.21 LICENSE为GPLv3；其README又写非商用，二者表述不一致。需保留具体版本文件并在计划分发/商用前请上游澄清，不把README一条话擅自合成为新许可证。
2. 复制GPL源码和直接引入内部模块前应审查组合方式、修改来源、对应源码及通知要求。以独立进程调用降低代码耦合，不能据此宣称捆绑二进制没有许可义务。
3. Whisper.cpp上游为[MIT](https://github.com/ggml-org/whisper.cpp/blob/master/LICENSE)，这不覆盖VoiceTransl GUI、全部第三方动态库、模型或FFmpeg。
4. [Sakura 3.7官方模型卡](https://huggingface.co/SakuraLLM/Sakura-GalTransl-7B-v3.7)标注 **CC BY-NC-SA 4.0** 并有非商用说明；不能因为llama.cpp开放就按无限制商用模型分发。模型卡与本地权重的精确revision对应仍须保留现有校验清单。
5. 主项目发布镜像没有指定自己的许可证，第三方LICENSE不能代替主项目授权决定。先外置用户自行选择工具/模型可减少捆绑范围，但不能掩盖来源和条款。

这是基于具体文件的工程许可风险清单，未替任何权利人作许可解释或裁定。当前只读使用和本报告不涉及重新分发。

## 8. 推荐

**保留现有 Whisper CLI 与 Sakura HTTP 作为默认适配器，保留原 VoiceTransl 安装供独立人工使用；逐步解除安装目录布局绑定，不提取整个GUI或GalTransl框架。**

仅当用户确实依赖目前跳过的条件词典、原生项目插件或原生cache时，先验证其稳定CLI/本地项目API，再决定是否增加第二翻译路径。对简单普通词典继续复用现有数据/测试即可。独立ASR/OCR后端应在同一金标集证明质量、时钟、恢复及资源收益后加入，不自动替换默认。

实施前仍需：精确安装包来源/哈希与对应源码、第三方组件构建信息、真实条件词典样本、GPU/CPU能力与峰值、原生API入口及许可声明澄清。统一列于 [07](07-open-questions.md)。
