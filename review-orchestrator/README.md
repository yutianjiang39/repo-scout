# Review Orchestrator

> 能用代码保证的事情，不交给 Prompt；能用程序验证的事情，不交给模型自我声明；只有需要智能判断的事情，才交给模型。

程序负责工作流：冻结、环境、测试、变异、取证、晋级。模型只负责判断。两边不越界：模型控制不了工作流，工作流也不替模型做判断。

## 职责划分

| 职责 | 承担者 | 落在哪里 |
|---|---|---|
| 冻结补丁、拆测试与实现、SHA-256、清单 | 程序 | `ro/artifacts.py` |
| 每关独立干净副本、一次性 worktree、共享依赖 venv（`pip freeze` 防篡改）、导入来源校验 | 程序 | `ro/envs.py` |
| 基线+测试 / 完整补丁的定向测试与套件，**每关独立重跑**，审阅者启动前完成 | 程序 | `ro/engine.py` `preflight` |
| 候选在独立环境不绿 → 不调用模型，直接 BLOCKED | 程序 | `preflight` → `gates.attach` |
| 对补丁新增行做 AST 变异（删语句、条件取反/恒真/恒假、比较符、与或、返回值、常量） | 程序 | `ro/mutation.py` |
| 引用改动模块的其他测试（参考信息，不作门禁） | 程序 | `engine.related_tests` |
| 证据在模型启动前封存进账本（哈希），只给审阅者只读副本 | 程序 | `ro/evidence.py` + `gates.attach` |
| 审阅者自己设计的错误实现，由程序在全新副本**重跑**，并与模型声明比对 | 程序 | `engine.verify_probe` |
| 转录审计：引用了工作区以外的路径（任务目录、其他关副本、实施仓库、凭证）→ 本关作废 | 程序 | `evidence.audit` |
| 模型回执：CLI 报告的实际型号与配置不符 → 作废 | 程序 | `evidence.observed_models` |
| 关卡顺序、必经关、补丁变化后旧 PASS 作废、盲审输入变更需注明理由 | 程序 | `ro/gates.py` |
| PASS 后封存本关材料，后续关卡在磁盘上拿不到 | 程序 | `evidence.seal` |
| Claude 文件工具限制在工作目录内（`--restricted`），不加载用户/项目设置 | 程序 | `ro/reviewers.py` |
| 根因、修复正确性、兼容性、证据解读、回归风险 | **模型** | 报告文本字段 |
| 每个存活变异是等价、无关还是**测试缺口** | **模型** | `mutants[]` |
| 设计有代表性的错误实现 | **模型** | `harness.py probe-*` |
| PASS / REVISE / BLOCKED | **模型** | `verdict` |

**模型判断与程序事实的一致性由门禁核对。** 报告不再填写退出码，所有可测量的事实都由程序记录。报告说"基线因缺陷失败"，程序就核对基线定向测试确实失败；说探针"被识别"，程序就核对重跑结果。每个存活变异都必须有判断；判为 `gap` 的不能给 PASS。至少要有一个审阅者自己设计、程序重跑确认被测试识别的错误实现。

## 架构

```text
orchestrate.py ── ro/orchestrator.py（生命周期）
                    ├─ artifacts  冻结 / 清单            ├─ engine    测试 / 探针重跑
                    ├─ envs       副本 / venv 缓存        ├─ mutation  变异
                    ├─ evidence   封存 / 审计 / 回执       ├─ reviewers CLI 适配器（config/reviewers.json）
                    └─ gates      唯一的状态变更者（gate.py 是它的手动 CLI）
```

目录：

```text
orchestrate.py  gate.py  deploy.py    入口：自动流程 / 手动门禁 / 本地部署
ro/          程序（各模块 + deployment 私有接线）
capsule/     冻结进每个审阅材料包的文件：协议、关卡说明、报告格式、终审说明、harness.py
prompts/     给人和实施会话看的：operator.md（启动语）、implement.md
config/      审阅者注册表、OpenCode provider 示例
templates/   checks.json 与任务输入模板
docs/        操作协议、模型线路、部署
tests/       离线测试（假审阅者）
private/ .runtime/ .cache/   本机私有，不入库
```

每个首审关卡的流程：
`prepare`（全新副本 + 材料包）→ `preflight`（程序跑测试和变异，证据封存）→ 审阅者 CLI（独立判断）→ `post`（审计、回执、探针重跑）→ `report`（门禁对照证据校验）→ PASS 则封存本关并启动下一关。

审阅者收到的提示只有一句：`Read REVIEW/STAGE.md and follow it.`。`STAGE.md` 只有几行，`PROTOCOL.md` 只写需要判断的事项。

## 使用

```text
python orchestrate.py --db <任务目录>/review.sqlite --work-root <仓库>/tmp/<任务> start \
  --repo <实施仓库> --base <基线> --tip <候选> --requirement requirement.md \
  --commands commands.md --checks checks.json --implementer <实施会话ID> \
  --tests <测试文件...> --gates gpt,claude
```

退出码：0 全部 PASS，1 BLOCKED，2 关卡中断，3 REVISE（异议在 `handoff/`）。

```text
orchestrate.py ... recheck <角色> <反证.md>   # 补丁不变、只有证据分歧：回原会话复核，撤回则继续晋级
orchestrate.py ... final <PR正文.md>         # 交叉终审（默认并行），全部通过后进入 ready
orchestrate.py ... status                    # 只看阶段、哈希、裁决
gate.py --db <db> authorize <patch> <body> <目标> "用户授权原话"
gate.py --db <db> complete "交付回执"
```

`checks.json` 示例见 `templates/checks.example.json`，启动语见 `prompts/operator.md`。新增审阅模型只需在 `config/reviewers.json` 加一项，换 CLI 需在 `ro/reviewers.py` 加一个适配器。

任务目录结构：`review.sqlite`（账本：状态、所有封存文件）、`evidence/`（程序证据）、`transcripts/`（CLI 原始输出、`post.json`、探针重跑）、`sealed/`（已通过关卡的材料）、`handoff/`（给实施者的异议）。

## 记录总库

所有任务记录（旧的手工审阅和新的 `review.sqlite` 账本）统一登记在一个 SQLite 总库里。文件留在原位，库里只存事实：任务、文件类别、SHA-256、大小，以及只能追加的事件日志。CLI 会话材料（OpenCode/Claude 转录、提示词、命令捕获）只计数，不读取、不入库。

```text
python -m ro.archive --db <records.sqlite> import <repos目录>   # 可重复执行，已登记的不重复记录
python -m ro.archive --db <records.sqlite> list [仓库]
python -m ro.archive --db <records.sqlite> verify             # 文件被改或丢失时列出
```

文件改动后再 import，会新增一行哈希，旧哈希保留。

## 边界

- 隔离靠三层：独立副本加封存（物理上拿不到）、Claude 的 `--restricted`（文件工具出不了工作目录）、转录审计（事后发现并作废）。Bash 本身不是沙箱，需要系统级隔离时请用独立账号或容器运行 CLI。容器 runner 预留在 `ro/envs.py` 的 `Env` 接口上，本机没有 Docker，还没有实现。
- 变异目前只支持 Python。非 Python 改动会记为 `supported: false`，审阅者自己设计的探针仍是必需的。
- 覆盖率分析还没有接入。

运行 `python -B -m unittest discover -s tests -v` 可离线验证，测试用脚本化的假审阅者覆盖了每一条强制规则。私有部署见 `docs/deploy.md`。
