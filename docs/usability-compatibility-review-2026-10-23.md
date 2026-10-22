# 技能管理可用性、兼容性与按需检索审查

日期：2026-10-23，Asia/Tokyo。基线：`eeb91f2793cb256278cb74048cbb71a0796094a6`，CLI/技能版本 1.0.1，开始时工作区干净。

## 结论

现有实现可作为六个平台的有限范围、按需运行的配置管理工具。尚不能承诺覆盖大部分主流 Agent、保证 token 节省，或让任意技能在当前会话立即可用。跨平台 SKILL.md 格式兼容不等于发现路径、配置层、运行依赖和刷新生命周期兼容。

此次仅审查，新增本报告；没有改动实现、真实宿主配置或安装的技能，没有执行发布。XMemo 初期架构记录仅作背景，旧发布记录未作为当前事实。没有采用新的架构决策；以下改进属于建议。

## 验证结果

- Windows / Python 3.13.2，`python -m unittest discover -s tests -q`：105 项通过，10.587 秒。测试输出中的 registry 验证文案来自测试，不是本次真实发布或线上验收。
- 实际六平台只读 status/audit：0 error、87 warning、3 info。33 条不存在的 Codex 路径规则、13 组重复标识、28 项 OpenClaw 未知状态，其余为元数据及范围提示。不能自动把重复项视为垃圾删除。
- 临时合成技能描述含 `invoice`，名称/路径等不含该词：发现 1 项，搜索逻辑命中 0 项。
- 临时损坏的 Claude JSON 配置传给 `--platform all ... search pdf --json`：退出码 1，stdout 为 0 字节，其他平台结果一并丢失。
- 未在真实宿主中修改设置并验证启停；没有当前提示词/token A/B 实测，没有本次 macOS/Linux 或大规模压测。

| 本机适配器 | 发现条目 | 配置层状态 | 单次 status 耗时 |
| --- | ---: | --- | ---: |
| Codex | 101 | on 59 / off 42 | 0.666s |
| Claude Code | 28 | on 28 | 0.070s |
| Copilot CLI | 25 | on 25 | 3.411s |
| OpenClaw | 28 | unknown 28；本机无 CLI | 0.224s |
| Hermes | 0 | 本机无 CLI；不是远端实例结论 | <0.001s |
| Antigravity App | 34 | on 8 / off 26 | 0.457s |

以上所有发现项的 prompt 可见性都为 unverified。数量是各适配器清单，跨平台可能重复，不能相加作为独立技能总数。

三个独立 CLI 搜索进程的小样本耗时：Codex 0.967/0.857/0.859s；Claude 0.505/0.401/0.398s；Copilot 1.900/1.800/1.859s。计入 Python 启动及宿主清单调用，并非冷缓存或 p95。当前规模可用，但不能据此承诺大量技能时瞬时检索。

## 兼容性覆盖

| Agent / surface | 仓库能力 | 关键边界 |
| --- | --- | --- |
| Codex | 用户/项目/缓存发现，skills.config 启停 | 缓存不代表有效插件注册表；未覆盖所有 admin/动态额外根；配置文件修改需要按宿主要求刷新/重启 |
| Claude Code | 用户/祖先项目技能；四种可见性状态 | 插件/内建/云同步技能不完整；不合并全部配置层；自定义配置根未完整处理 |
| Copilot CLI | 原生清单或文件回退；disabledSkills | 仓库禁用与用户禁用取并集，用户层 enable 不保证可用；并非 VS Code Copilot 适配器 |
| OpenClaw | 原生清单/回退；原生 config set | 本机只能只读回退；依赖、allowlist、快照决定最终可见性 |
| Hermes | 默认 home 技能；全局/平台禁用列表 | 外部目录/profile/运行资格未完整覆盖；essential skill 有原生保护 |
| Antigravity App/CLI | 两种全局根，目录停放 | 非原生 disable API；不管理 workspace/plugin 副本；App 与 CLI 需显式选择 |
| Gemini CLI | 未实现 | 官方已有独立的技能启停及 reload 命令，不能用 Antigravity 代替 |
| OpenCode | 未实现 | 官方已有 skill permission allow/deny/ask 以及 per-agent 控制 |
| Cursor、Windsurf、Cline、Roo Code、Kiro、VS Code Copilot | 无专用适配器 | 本结论来自本仓库平台枚举，不表示这些宿主不支持技能；各自最新控制契约仍需另行核验 |
| Claude Desktop/Cowork、其他账号托管池 | 未实现 | 本地目录适配器不覆盖账号级管理 |

没有市场占有率或完整主流宿主分母，因此不报告“支持率”。六个适配器不构成“大部分主流 Agent 已兼容”的证明。

## 按严重性排序的发现

### P1：按自然语言找技能的链路缺失

`skillctl.py:88` 的 Skill 模型不存 description；`:2026` 的 search 仅对 name/identity/group/path 做不区分大小写的子串判断。没有描述索引、多词排序、中英文别名或拼写容错；入口称 fuzzy 容易令人误解。用户说“识别发票”，技能叫 `document-tools` 时可能完全找不到。

建议先实现 name/description/tags 的词法检索、别名和相关性排序、top-k 限制；语义检索是否必要应由召回评测决定。保留所有已关闭技能的外部索引，使隐藏于宿主的技能仍能被管理入口查到。

### P1：配置写入成功与技能实际可用之间没有验收闭环

各适配器返回 changed 和 new_session_recommended，没有统一的宿主重载、重载后的有效状态查询及调用验证。Claude 的其他配置层、Copilot 的仓库禁用、OpenClaw 的运行资格均可能继续阻止使用。

Codex 还有具体的诊断缺口：`:525` 将未启用插件的技能判 off，而 `:677` 的 enable 仅移除技能管理规则，不开启插件，也不报告 plugin-disabled 阻塞原因。于是命令可正常结束而技能仍 off。不应自动开启整插件；应返回结构化原因和需要的宿主操作。

建议区分 installed / configured_enabled / eligible / catalog_visible / invocable / reload_required / blocked_reason，未知保持 unknown。对确切宿主版本分别验证；不把统一布尔值作为最终效果。

### P1：自动按需开启的入口尚未形成

`agents/openai.yaml` 设置 `allow_implicit_invocation: false`，Codex 用户需显式调用管理器，或者其他指令主动路由到 CLI。当前没有“任务意图 → 查隐藏技能 → 确定目标 → 启用 → 宿主刷新 → 使用”的端到端流程。

此设置仅限制隐式调用，不证明管理器元数据不占上下文。低 token 模式应保留一个短小且可发现的检索入口；候选目录留在模型上下文之外。不能通过隐藏所有发现入口来期待 Agent 自行知道应启用什么。

### P1：不能保证 token 节省

没有 token 采样、提示词比对或任务完成率评测。Agent Skills 本身已采用渐进披露：通常首先加载目录元数据，调用时才读正文。因此关闭未使用技能主要可能节省目录元数据，不等于省去整篇 SKILL.md。

搜索工具返回、管理说明、启停往返也消耗 token；当前 search 无 top-k/输出预算。已进入会话历史的正文也不会因为改配置自动消失。需要比较同模型、同任务、同上下文设置下的输入 token、缓存 token、延迟与成功率，才能证明净收益。

### P2：全平台搜索没有故障隔离

`:2026` 直接串行遍历 adapter.discover，未像 status/audit 捕获单平台失败。临时损坏配置已复现其他平台结果全部丢失。原生清单调用有 20 秒超时，Copilot 超时不会自动进入现有非零返回码回退分支。建议部分结果与 errors 分开返回，同时保留非零退出状态。

### P2：每次重扫，没有持久增量索引与性能预算

`:155` 及各适配器每次扫描/解析；跨平台串行调用，成本可叠加。无独立索引、mtime/hash 失效策略、分页或延迟指标。无需引入 watcher：可采用按需增量缓存，并在变更操作前重新确认目标、文件和作用域。

### P2：宿主范围、版本和依赖覆盖不足

当前 CLI 没有全面的宿主 capability/version 探测。Claude/Copilot 读取单配置层，Codex 从缓存推测插件状态；静态 SKILL.md 的存在也不能证明依赖程序、账号权限、工具连接和 OS 条件满足。文档已经披露部分限制，产品仍缺少统一可用性诊断。

### P2：预设和并发写入的边界

配置备份、原子替换、精确选择器、保护名单和预检是已有优点。但预设运行时仍可部分成功，跨进程没有事务锁/写前版本比较，另一个宿主进程同时编辑时仍存在覆盖风险。示例 lean 预设是特定名称的演示，不能当成通用的“仅保留必要技能”策略。

## 建议的实施顺序与验收标准

1. 先修复检索故障隔离和 enable 阻塞原因；补描述/别名检索及 top-k。用实际意图测试集验证已关闭技能可召回。
2. 保留轻量管理入口，实现可解释的查找和按需开启流程。明确哪些宿主可当轮刷新、哪些需要新会话；无依赖或权限时给出实际阻塞。
3. 优先评估 Gemini CLI、OpenCode 原生适配，再分别核验 Cursor/VS Code 等目标产品。每个适配器明确版本、OS、用户/项目/插件范围及刷新能力。
4. 对已有六适配器建立隔离真实宿主 roundtrip：发现 → 关闭 → 验证目录不可见/不可调用 → 开启 → 验证恢复。不能用 mock 测试代替。
5. 最后做同任务 token A/B 和 100/1000/10000 技能检索基准。报告召回率、p50/p95、净 token、缓存成本、任务成功率；再决定是否需要更复杂索引或语义层。

## 本次核实的一手资料

- [Agent Skills progressive disclosure](https://agentskills.io/home)：目录、激活、执行三个阶段。
- [OpenAI Build skills](https://learn.chatgpt.com/docs/build-skills)：路径规则、配置修改后重启、隐式调用策略；当前目录预算为上下文窗口的 2%，未知窗口时为 8000 字符，可通过 skills.max_context_tokens 调整。关闭一部分技能可能使剩余描述更完整，而不一定线性减少已达预算的目录大小。
- [Claude Code skills](https://code.claude.com/docs/en/skills)：四态 skillOverrides、插件不受其控制、skill-doctor 统计。
- [Copilot CLI configuration](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference)：disabledSkills 和用户/仓库并集语义。
- [Gemini CLI skills](https://geminicli.com/docs/cli/skills/)：enable/disable、user/workspace、reload。
- [OpenCode skills](https://opencode.ai/docs/skills/)：allow/deny/ask；deny 隐藏并拒绝访问；per-agent 设置。
- [OpenClaw skills](https://docs.openclaw.ai/tools/skills)：发现、就绪、可见性区分和快照刷新；原生已有目录搜索能力，可优先研究复用。
- [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)：默认目录及外部目录。
- [Antigravity skills](https://antigravity.google/docs/skills)：按 surface 的技能机制；skills_disabled 为本管理器约定。

Cursor 文档入口本次读取失败，因此未将其最新宿主契约列为已核实。上述网络文档核对不构成本机安装版本的实测认证。
