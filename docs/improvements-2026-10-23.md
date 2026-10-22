# 按需技能检索与兼容性改进

2026-10-23，基于 `eeb91f2793cb256278cb74048cbb71a0796094a6`。改进验证完成后，用户授权 tag 发布，本次发布版本定为 1.1.1。真实用户的技能开关未被修改；实际渠道发布结果以对应 GitHub Actions 与 registry 验证为准。

## 已完成

1. **按用途检索**：名称、宿主标识、描述、分组和路径共同参与大小写无关词法搜索；多词要求全部命中；精确名称优先。已关闭技能仍可找到。描述最多读取/返回 1024 字符，不加载正文。
2. **控制返回规模**：默认最多 20 条，支持 `--limit 5`，`--limit 0` 显式返回全部。JSON report 包含总数、截断标记、匹配字段和分数。减少工具输出量，不等同于已测量模型 token 节省。
3. **跨语言别名**：`--aliases` 接受显式词到同义词数组；附中文示例。不是嵌入语义搜索，也不保证任意自然语言长句的召回。
4. **快速离线索引**：`index --output` 导出带日期的元数据快照；`search --index` 无需调用宿主 CLI。拒绝覆盖现有文件，POSIX 文件创建权限为 0600。索引包含私人路径，示例后缀已加入 gitignore。变更操作不接受索引参数，始终重新发现目标。
5. **故障隔离**：搜索保留健康平台结果，错误按平台收集，整体返回非零。旧 JSON 数组形式保留；诊断走 stderr，`--report --json` 提供统一结构。
6. **启用检查**：`inspect` 对精确目标执行只读预览，列出已知额外配置层及宿主刷新指引。Codex 父插件未在所选配置中启用时返回结构化阻塞，不再无提示地返回成功。预设预检遇到阻塞时不执行前面的禁用动作。
7. **扩大覆盖**：新增 Gemini CLI 和 OpenCode 两个有限范围适配器，使平台枚举从 6 个增加到 8 个。支持 dry-run、备份、保护名单和配置往返；不宣称覆盖全市场或全部宿主运行态。
8. **按需入口**：技能入口允许 Codex 隐式调用，给出“搜索 → 精确选择 → inspect/预览 → 在已有授权下启用 → 刷新 → 宿主验证”的流程。不会因为只请求搜索就修改配置，也不会为解除技能阻塞擅自开启整插件或放宽其他策略。

## 新适配器契约

- Gemini：用户或显式 workspace settings 的 `skills.disabled`；支持 `GEMINI_CLI_HOME`；发现本地 `.gemini/skills` 和 `.agents/skills`。所选配置的 `skills.enabled=false` 或 admin skills gate 阻止 enable。其他系统层、扩展、内建、工作区信任仍由宿主管理。[官方配置](https://geminicli.com/docs/reference/configuration/)
- OpenCode：所选文件的 `permission.skill` 精确 allow/deny 规则；保留 wildcard 顺序和其他权限，ask 报 unknown；所选配置关闭 skill 工具时阻止 enable。支持 `XDG_CONFIG_HOME`、`OPENCODE_CONFIG` 和额外 `OPENCODE_CONFIG_DIR` 技能根；JSONC 需显式选文件。全局 permission 字符串简写拒绝重写，skill 简写保留为 wildcard 默认值。[官方技能](https://opencode.ai/docs/skills/)、[规则优先级](https://opencode.ai/docs/permissions/)

## 设计决定

本次没有可调用的 pending-decision/resolve-decision 工具，因此将选项和结论记录于此：

- 不引入 watcher 或向量服务；采用实时扫描加显式离线快照。优点是简单、可控、可离线；代价是快照不会自动更新。下次安装/升级后必须重新导出。
- 保留旧 `search --json` 数组协议，通过 `--report` 增加结构化结果；默认 top-20 是有意的行为变化，脚本需要全量时应加 `--limit 0`。
- 不自动重启宿主或启动收费模型测试。返回明确刷新指引，live_verified 恒为 false，宿主实际调用仍需独立验证。
- 新适配器明确标为 selected-layer/scoped，避免复刻尚未核实的全部继承/权限规则。
- 不自动启用 Codex 插件、不改写更广泛的工具权限来完成单技能 enable；遇到这些阻塞保持零写入并报告原因。

## 验证

- Windows / Python 3.13.2：126 项 unittest 全部通过（基线 105 项，新增 21 项）。
- 新增用例覆盖描述/中文/别名检索、精确排序、截断、坏平台隔离、不回显解析器敏感内容、旧 JSON 兼容、索引往返/拒绝覆盖/坏 schema、元数据更新、Codex 插件阻塞、预设零写入，以及两个新适配器的临时文件真实读写往返。
- 发现并修复 OpenCode 通配符顺序回归：补默认值不能把原有 `*` 移到队首。
- Python 编译、技能 quick_validate、git diff --check 通过。
- 本机八平台只读索引：282 条记录、0 个发现错误。跨平台记录可能重复，不是独立技能总数。
- 本机只读 audit：0 error、110 warning、3 info。新增平台使扫描范围扩大；未自动清理已有重复或失效规则。
- 本机索引中搜索 spreadsheet：共 17 个匹配，返回 5 个；三个完整 Python CLI 进程耗时 0.532/0.447/0.492 秒。

可复现基准：`python scripts/benchmark_search.py`。下表包含快照读取、结构校验和词法排序，不包含 Python 启动、宿主发现或任何模型请求。每个规模仅 5 次，p95 是该小样本的最近秩统计，不能当成生产 SLA。

| 合成记录 | 中位数 | p95 | 返回条目 | 输出字符 |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 2.70ms | 17.88ms | 5 | 1623 |
| 1000 | 24.62ms | 38.00ms | 5 | 1624 |
| 10000 | 237.33ms | 258.37ms | 5 | 1625 |

## 仍然存在的边界

- 没有真实宿主启停端到端或模型 token A/B；临时配置 roundtrip 不替代它们。没有执行本次 macOS/Linux 实测。
- Cursor、VS Code Copilot、Windsurf、Cline、Roo Code、Kiro 及账号托管池仍没有专用适配器。
- 无完整的多配置层合并、依赖/账号/工具就绪性判断、运行中会话自动刷新。`on` 仍是所选适配器配置，不是保证可调用。
- 快照是词法搜索数据集，不是持久增量索引；新技能需重新导出。没有拼写容错、embedding 检索、跨进程事务锁或自动 token 优化策略。
- skill 入口的隐式调用开关使宿主可以选择它，不保证任何宿主都会选中，也不使该入口零 token。

这些边界已在技能说明与 README 保留；实际安装到用户技能目录及更广范围适配不属于本次 tag 发布。

## 发布门禁跟进

v1.1.0 标签的发布测试在 Windows/macOS 发现测试路径别名比较问题；Linux 四个矩阵通过，发布任务均未执行。修正测试为解析后的路径身份比较，保留原标签，不覆盖已推送提交；发布目标改为 v1.1.1，先验证 main 的完整跨平台 CI 再打新标签。
