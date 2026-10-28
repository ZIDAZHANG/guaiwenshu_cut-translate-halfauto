# OCR、ASR、时间轴与人工检修设计

审计日期：2026-10-25。先说明当前事实，再给设计；未运行识别、自动修复或改写任何现有字幕。证据等级见 [01](01-current-state.md)。

## 1. 三类时间和两类文字必须分开

画面OCR回答“屏幕写了什么、何时出现/消失”；ASR回答“音频说了什么、何时说”；最终中文字幕还包含选择来源、切分、翻译和显示方式。画面可以提前显示完整句、保留未说完的后半句、只出现标题/招牌，也可能与口语不同。**OCR与ASR不一致只能先标记潜在冲突，不能自动把一方替换为另一方。**

当前已有父语音、画面事件和原文范围，这是应保留的基础；尚未形成完善的人工确认文本层。

## 2. 当前八个问题的实情

| 问题 | 从当前代码确认的行为 | 限制 |
| --- | --- | --- |
| 1 如何取OCR帧 | [Magia adapter](../../app/tools/magia_timeline_adapter.py)（本地证据：`fix_stage/tools/magia_timeline_adapter.py`；原审计源码第312行）将视频送外部MagiaTimeline；选择dtd/default/speculative，不是主Python逐帧截图OCR | 主模块看不到每个采样帧/PTS/检测框。`max_resolution=0`为不缩放，当前候选配置区域为整幅。 |
| 2 如何识别文字 | adapter338–344行设extraJobs=ocr、doPaddle=true、doTesseract=false，结果在timeline.ass | 本地二进制精确版本/内部模型revision未完整绑定。研究源码不能代替二进制对应证明。 |
| 3 出现/消失时间 | Magia以画面变化生成ASS区间；主程序解析其开始/结束 | 没有完整逐帧边界证据或VFR/偏移映射。已有个别真实边界PTS核对，不代表全片同帧准确。 |
| 4 重复文字 | [align_page_text](../../app/tools/subtitle_segments.py)（本地证据：`fix_stage/tools/subtitle_segments.py`；原审计源码第66行）与build_segments按文字锚点/事件位置对应，部分连续同文事件可合并；stable_id纳入出现身份 | 不全局删除重复台词。OCR闪烁/背景变化、真正同文再次出现仍可能产生歧义，应检查事件来源。 |
| 5 画面与语音不同 | build_segments保留visual_events/parents/source_ranges，TEXT_CONFLICT、VISUAL_ONLY、未映射等标记 | 当前翻译主源仍主要是ASR原文范围，未有可靠自动“识别纠错主文本”层。 |
| 6 错识别/漏识别/错合并 | 有源范围覆盖、身份校验和冲突标记；VAD词坐标冲突可无VAD重试 | 完整保留的是ASR文字，不代表没漏听。无音频活动覆盖证明；视觉OCR置信度未标准化保留。 |
| 7 最终中文时间 | [build_segments](../../app/tools/subtitle_segments.py)（本地证据：`fix_stage/tools/subtitle_segments.py`；原审计源码第236行）先按mixed/screen/speech产生canonical段；翻译按相同cue身份返回；[publish_segment_output](../../app/tools/subtitle_segments.py)（本地证据：`fix_stage/tools/subtitle_segments.py`；原审计源码第568行）生成ASS/SRT | 缺可靠词时间时保留父句时间包络并CHECK；不按中文字数比例切译文。该包络可能需人工进一步细分。 |
| 8 人工改后保护 | 现有checkpoint和sidecar阻止某些陈旧身份混用 | 没有人工锁/修订分支。原子写保证完整写入，但可覆盖final.ass/review.csv；直接编辑zh_machine.srt而不更新旧sidecar会失败。 |

**上游资料的边界：** [MagiaTimeline官方README](https://github.com/HurryPeng/MagiaTimeline/tree/1.1)说明framewise逐帧/每n帧、speculative跳过不太可能变化的帧。已有研究 upstream_TextExtraction.py（本地证据：`implementation_20261010/upstream_TextExtraction.py:36`，未公开）使用PP-OCRv4_mobile_det/PP-OCRv6_small_rec，从interval attachment取代表帧并提取rec_texts；未把rec_scores写入当前规范记录。该源码是研究材料，不证明本机EXE与其逐字节对应，更不能把上游“多数情况同帧”目标写成本项目准确率。

## 3. 已经存在的可靠性措施

- [audit_segment_records](../../app/tools/subtitle_segments.py)（本地证据：`fix_stage/tools/subtitle_segments.py`；原审计源码第524行）检查ID唯一、正向时间、原文范围合法、非空白字符不能遗漏/重复分配、输出文本符合原范围。
- [load_whisper_word_times](../../app/tools/subtitle_segments.py)（本地证据：`fix_stage/tools/subtitle_segments.py`；原审计源码第459行）保留原始token/DTW证据，不能用非法/缺失词时间插值冒充精确时间。
- [recognize的恢复](../../app/tools/voicetransl_adapter.py)（本地证据：`fix_stage/tools/voicetransl_adapter.py`；原审计源码第150行）保存VAD初次和无VAD重试；非零退出不接收残留SRT。
- [artifact_validation](../../app/tools/artifact_validation.py)（本地证据：`fix_stage/tools/artifact_validation.py`；原审计源码第78行）已有负时间、非正时长、开始倒序、字幕解析遗漏、实际媒体越界等检查；源ASR粗时间可先保留警告，派生时间在翻译前需合法。
- 翻译身份sidecar确认cue_id/index/text/time；批次行数异常拆单条并保留原响应。术语、日文残留、模型主动[待确认]进入复核。
- [quality_summary](../../app/tools/roast_pipeline.py)（本地证据：`fix_stage/tools/roast_pipeline.py`；原审计源码第1292行）汇总源覆盖、词时钟、未映射、术语及语言问题。缺少人工确认仍不可把ready叫“准确成品”。

**当前缺口：**没有统一识别分数和框、可校准的异常时长/同轨重叠检查、OCR连续性证据、完整语音活动覆盖、人工修订版本及安全回流。review.csv的high/low来自CHECK启发式（[611行](../../app/tools/subtitle_segments.py)（本地证据：`fix_stage/tools/subtitle_segments.py`；原审计源码第611行）），不是模型置信度或准确率。

纯无声OCR还有总流程障碍：虽然分段函数支持VISUAL_ONLY，主流程[validate_asr](../../app/tools/roast_pipeline.py)（本地证据：`fix_stage/tools/roast_pipeline.py`；原审计源码第996行）要求ASR非空；不能声称已端到端支持纯无声字幕片。

## 4. 已有真实证据说明了什么

2026-10-12最后审计（本地证据：`implementation_20261010/draft_eval/final_usability_audit_20261012.json:1`，未公开）：35秒mixed/speech各15条，234原字符保留；各13条CHECK、12处源文冲突、3术语问题。零越界/零字符遗漏和七阶段恢复已记录，但准确性/native UI均false。保存产物本次只读核查，未重跑模型。

历史记录另有572.636秒长片的结构检查、无VAD词时钟恢复和Qwen长片失败。它们证明有长片研究资产，不能把短片最终翻译验收扩展成长片全流程准确。例：`近音专名B/普通词A`、人物关系、人称、未完句属于内容问题，整体平移时间轴无法修好。

Qwen强制对齐曾大量返回SDK修正/非法原始预测，结果被拒；默认关闭是合理的证据决策。模型返回单调时间或进程退出0不构成精确边界证明。

## 5. 推荐轻量数据设计

保留现有JSON旁路，在现有cue_id上扩展，不另建字幕编辑GUI或数据库服务器：

| 记录 | 最少应保存的信息 |
| --- | --- |
| Media | 源身份、实际时长/time_base/start_time、派生转换、音轨及绝对时间映射 |
| VisualEvent | ID、出现次序、原帧PTS/关键帧、区域/框、原始OCR文本/分数、模型/参数、出现和消失依据 |
| SpeechParent/Word | 原始ASR句和字词、绝对时间、活动区间/处理覆盖、有效性、原始/修正值分开 |
| SourceRevision | 原ASR/OCR并列、选作翻译的文本、选择理由、确认者、base hash与父版本 |
| Cue/Translation | ID、source_revision、parent/range、角色、层、时间来源、译文、模型/请求/词典版本 |
| Review/Patch | base revision、cue ID或旧新映射、逐字段更改、人工锁、时间、确认范围、变更原因 |
| Export | 选定字幕版本、媒体/字体/样式/渲染器身份、质量状态、输出hash |

模型没有置信度时保存null/unknown，不能填1.0；不同模型分数不直接横比。比较用Unicode/假名规范化时保留原始文本和字符映射，不能把规范化结果悄悄写回原证据。

画面文字和语音字幕至少用role区分；必要时分别呈现或供人工选择。背景招牌和对白同时显示可能合理，不能全局强制单条字幕或一对一映射。

## 6. 质量检查与处置矩阵

硬性完整性校验与可调整的提示规则分开。异常时长/阅读速度仅作警示配置，不新增默认硬上限，不为了减少复核量删字、合并或截断。

| 检查 | 自动检测 | 自动修复范围 | 自动标记/人工判断 |
| --- | --- | --- | --- |
| 识别置信度异常 | 后端有score/logprob时保留分布；未知单列；低分和OCR行内差异定位 | 无确定文本证据不改字 | 低分、专名、数字、否定词优先复核；高分也不能豁免 |
| 持续时间异常 | duration、字符显示速度、相邻间隔、来源时钟；按角色配置提示 | 只修确定格式/单位错误且生成新版本 | 过短/长、跨句包络由人听音看帧；不统一套最短/最长时长 |
| 越界/非有限/倒序 | start<0、end<=start、end>真实媒体、无穷/NaN/倒序 | 明确已知派生offset可重算；保留变换记录 | 未明时基拒绝发布已验收版本；不得简单钳制到片尾 |
| 不合理重叠 | 按role/layer/父来源比较区间，区别合法同显 | 仅完全相同来源的重复发布可在派生层幂等处理 | 不同来源重叠先标记；不删同文真实重复 |
| OCR连续性 | 相邻代表帧文本/框、短闪、断字、逐字出现/消失、同文背景变化 | 只有证据同事件的帧聚类可自动归组，保留原事件 | 边界附近自适应取帧；歧义保留前后帧与候选时间 |
| ASR/OCR潜在冲突 | 同一时窗的文字差异、名字/数/否定/尾句；画面字与口语相似度 | 不自动把OCR覆盖ASR或相反 | 标明“可能不同内容/识别冲突”，需决定字幕角色与翻译来源 |
| 翻译对应 | cue/source revision、原文范围覆盖、响应完整、缺行/串行、术语/日文残留 | 确定源词的显式替换可应用到新版本；重试须留原响应 | 行数相等不证明语义；漏义、人称、语气和幻觉需人工 |
| ASR漏识别 | 处理完成记录、VAD活动/字幕覆盖、静音/音乐/画面台词差异 | 仅对明确失败区间重识别，保留旧候选 | VAD本身可能漏弱音；未覆盖活动时窗由人听音确认 |
| 人工版本保护 | base hash、字段锁、派生版本和导入差异 | 无冲突字段可确定合并；机器结果写新branch | base改变、拆并/重排/多候选映射必须确认；不得覆盖锁定字段 |

自动标记应包含cue ID、规则、来源区间、前后文/帧和原因，便于编辑器定位；不要仅输出整片“低置信度”。质量状态至少区分执行完成、未校验、待人工、人工确认范围；规则未触发不自动成为人工通过。

## 7. 利用现有编辑器进行人工回流

推荐普通SRT用Subtitle Edit、ASS排版用Aegisub，能力和许可详见 [03](03-media-pipeline.md)。新增的是文件协议，不是重做编辑器。

1. 导出一份机器快照和编辑副本，附base hash、cue manifest和review定位表。编辑副本与机器缓存路径不同。
2. 人工在编辑器中打开视频/音频与字幕；复核人名、错误合并、边界、画面/语音差异；另存SRT/ASS。
3. 回流先解析和比较差异。条数/顺序不变且映射唯一时接回cue ID；SRT行号只能辅助，不能当持久身份。
4. 拆分/合并/排序/整体平移产生新的派生cue及父谱系；模糊匹配不自动视为已确认。ASS的Name/Effect可以帮助，但编辑器可能清理它们，旁路仍应保留。
5. 正文/时间/样式分别标记人工锁和确认范围。只校对时间不代表确认翻译；改ASR源文需建立source revision，原始ASR token仍关联旧文本，不能硬挂到新词上。
6. 渲染显式读取已选择的review版本；后续ASR/翻译重跑写新机器版本，产生冲突报告，保持人工版本原字节可恢复。

现有 [retime_supplied_translation](../../app/tools/roast_pipeline.py)（本地证据：`fix_stage/tools/roast_pipeline.py`；原审计源码第712行） 严格核对身份并保留supplied字节，值得保留，但解决的是同身份译文在模式间重新定时，不是自由外部编辑回流。旧 `.segments.json` 与新正文不符时应提示“需要导入修订”，不能让用户手改旁路冒充一致。

## 8. 边界检修与验收样本

优先复用已找到的35秒/25秒和572.636秒素材，追加少量有针对性的金标片段：无声文字、无画面字幕的口语、同文再次出现、逐字显示、标题/对白同显、连续语音跨换页、片尾未完句、VFR/非零起始PTS。不要重新索要已在G盘找到的素材。

金标应分别标注画面出现/消失帧、音频起止、原文、译文和可接受多解。比较时分别报告字符错误、术语/语义错误、边界偏差、漏片段、重复来源和复核耗时；不能以单个合成总分掩盖错误类别。样本少时报告例数，不伪造统计准确率。

自动边界只能在可靠同源时基上优化：粗定位后对边界附近逐帧/PTS复核，保留前后证据。ffsubsync等声音同步工具至多适合明确整体漂移的同一对白轨，不解决OCR视觉出现区间或错误句子对应，不列默认全局修复。

## 9. 失败、恢复与数据保护验收

- 在生成、翻译、导入修订、渲染分别中断；旧人工版与已发布文件哈希保持不变，恢复只重跑受影响机器阶段。
- 改模型、词典、源文、OCR区域或模式后，相关机器缓存失效；原始证据、人工字段和父版本仍能追溯。
- 错ID/陈旧base/缺sidecar/编辑器重编号/拆并/ASS样式损失都要有明确诊断与回滚，而非静默接受。
- 源覆盖与时间合法属于可自动验证；语义正确、画面文字是否该翻译、复杂重叠与专名含义必须人工确认。复核阈值经真实样本决定，不能为了看起来全绿降低规则。

上述均为实施后的验收要求，本轮未创建测试、未写任何人工修订或运行自动修复。

## 10. 补充审查：先独立校对字幕，复杂回流后置

按用户本轮方向，**第7节的完整回流设计不是使用字幕编辑器或交付人工字幕的先决条件**。最小工作流可以直接复用现有SRT/ASS，在编辑器打开原视频与字幕、另存校对稿并交付。仅需把机器稿和人工稿分开，保留视频/字幕身份和所选版本，避免主程序再写同一路径；未来才按需要追加cue级映射、字段锁和来源谱系。

本轮新增的静态证据：

- [roast_pipeline.validate_translation](../../app/tools/roast_pipeline.py)（本地证据：`fix_stage/tools/roast_pipeline.py`；原审计源码第1230行）强制canonical条数、index和起止时间一致。人工拖时、拆并后的合法字幕不等于仍是合格机器翻译输入。
- [normalize_text](../../app/tools/subtitle_axis_align.py)（本地证据：`fix_stage/tools/subtitle_axis_align.py`；原审计源码第88行）去除HTML/ASS标签；[parse_ass](../../app/tools/subtitle_axis_align.py)（本地证据：`fix_stage/tools/subtitle_axis_align.py`；原审计源码第154行）存入规范化cue正文。`raw`中仍有原事件，但旧成品流程不会把人工文件的全部原事件/样式当作渲染源。
- [run_alignment](../../app/tools/roast_pipeline.py)（本地证据：`fix_stage/tools/roast_pipeline.py`；原审计源码第523行）使用旧timeline作为模板。因此不能承诺把Aegisub校对后的ASS放进`--zh-file`会无损保留位置、字体、层和时间。

结论：先把`reviewed.ass/srt`作为独立成品。编辑器或未来独立渲染入口直接读取它，ASS的完整样式表与覆盖标签保留；不要让旧翻译输入、原文匹配和模板重建参与该渲染路径。与原语音/OCR对应关系未知时标明来源关联未重新审计，不妨碍字幕文件本身可编辑/播放，也不能冒充已有cue级来源确认。

还应把两种人工调整分开：

| 操作 | 对字幕/视频版本的影响 | 验收重点 |
| --- | --- | --- |
| 只改文字、起止或样式 | 视频不变，校对字幕成为新版本 | 改字/拖边/整体移动/拆并后另存重开，确认真实帧、声音和样式；不重跑模型 |
| 剪视频、拼接、变速或换音轨 | 形成新媒体时间线，旧字幕不一定继续对应 | 保留NLE工程、剪后视频和剪后字幕；核对字幕是否随剪辑/变速移动，不自动假设跟随 |

字幕编辑器的波形主要定位语音，OCR文字出现/消失仍要看视频帧；场景切换并不总等于文字换页。自动吸附到静音/镜头、固定时长和批量同文合并先预览差异，不默认改写。

本次只查源码和官方文档；尚未运行编辑器、生成波形、安装组件或修改样本。后续最小实机验证和已有样本链接见 [03第9节](03-media-pipeline.md)。
