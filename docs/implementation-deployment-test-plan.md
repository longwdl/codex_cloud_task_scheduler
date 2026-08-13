# Codex Cloud Task Scheduler：实施、部署与验收方案

> 状态：规划基线（环境无关核心已开始实现）
>
> 日期：2026-08-13
> 需求来源：[ChatGPT 对话「Codex远程工作一致性」](https://chatgpt.com/s/t_6a7d2caae96481918a0f6ba442814c78)

## 1. 结论

本项目应实现为一个**无常驻 Web 服务的 systemd 薄调度器**：

```text
GitHub Issues / Labels             人类任务事实与队列
        │
        ▼
systemd timer + flock              周期触发与单实例保护
        │
        ▼
Python dispatcher                  校验、认领、幂等、对账
        │
        ├── SQLite                 仅保存运行状态和恢复锚点
        ├── gh CLI                 GitHub Issue/Branch/PR 适配器
        └── Codex Cloud CLI        提交、查询、获取和应用 Diff
                │
                ▼
Git branch + Draft PR + CI         代码事实与人工验收入口
```

第一版的明确边界：

- Tracker 只实现 GitHub Issues；接口按可增加 Linear/Jira Adapter 设计。
- Executor 只实现 Codex Cloud；为以后增加 Linux 本地 `codex exec` 保留接口。
- 一台 Linux 控制节点；单个仓库最大并发为 1。
- 调度器只创建任务分支和 Draft PR，不自动合并、不部署、不标记业务任务完成。
- Issue、仓库内容、评论和 Cloud 输出均视为不可信输入。
- SQLite 不是任务事实来源，只用于防重、恢复和审计。
- Cloud Environment 默认禁用 Agent 阶段网络；确有需要时只允许任务所需域名和 HTTP 方法。
- Agent 生成代码触发的 CI 属于不可信代码执行，CI 不得接触部署凭据、生产 Secret 或生产自托管 Runner。
- 不开发自制看板、Redis、消息队列、多 Runner、Webhook Gateway 或 Kubernetes。

这与原始对话中的最终需求一致：MacBook 只用于下发任务和验收；长期在线 Linux 负责调度；Codex Cloud 负责执行；GitHub 保存可审查的任务和代码结果。

## 2. 当前事实与假设

### 2.1 已核验事实

- 当前工作目录为空，不是 Git 仓库；不存在源码、测试、CI、容器或部署配置。
- 本机 `codex-cli` 版本为 `0.147.0`。
- 该版本本机实测提供：
  - `codex cloud exec`
  - `codex cloud status`
  - `codex cloud list --json`
  - `codex cloud diff`
  - `codex cloud apply`
- 当前 `codex cloud exec` 没有 `--json`；`status` 也没有 JSON 输出。
- 当前帮助中没有 Cloud Task 的 `wait`、`logs`、`message` 或 `cancel` 命令。
- `codex exec --json` 是 Linux 本地非交互执行协议，不等于 `codex cloud exec`。
- 本机尚未安装 `gh` CLI。

这些是 2026-08-13 的本机快照，不应被当成永久 API 合同。部署前必须固定版本并运行合同测试。

### 2.2 需要在实现前确认的配置

以下信息不阻塞架构和测试开发，但阻塞真实 Cloud Smoke Test：

1. 允许调度的 GitHub 仓库列表。
2. 每个仓库的默认分支和 Codex Cloud Environment ID。
3. 允许添加 `agent:ready` 的 GitHub 用户或团队。
4. 每个仓库默认允许修改和禁止修改的路径。
5. Draft PR 的必要 CI Check 名称。
6. 日志和运行记录保留周期。
7. Codex CLI 与 `gh` CLI 的首个固定版本。

### 2.3 绿地默认值

如果没有新的选择，第一版采用：

| 项目 | 默认值 |
|---|---|
| 运行时 | Python 3.12，运行时尽量只用标准库 |
| 调度方式 | 每 2 分钟执行一次 oneshot sweep |
| 全局并发 | 2 |
| 单仓库并发 | 1 |
| Tracker | GitHub Issues + Labels |
| Executor | Codex Cloud |
| 状态库 | SQLite，WAL 模式 |
| 交付 | 独立分支 + Draft PR |
| 自动合并/部署 | 禁止 |
| Issue 评论输入 | 仅允许维护者的 `/codex-context` 评论 |
| 失败策略 | 不确定时阻塞，禁止猜测性重试 |

## 3. 目标与非目标

### 3.1 MVP 目标

用户创建一张格式完整的 GitHub Issue，并由维护者添加 `exec:cloud` 和 `agent:ready` 后，调度器能够：

1. 确定性选取任务。
2. 验证需求完整性和安全准入。
3. 为运行生成唯一 `run_token`。
4. 从记录的 `base_sha` 创建并推送任务分支。
5. 向指定 Codex Cloud Environment 提交任务。
6. 在后续 sweep 中对账 Cloud 状态。
7. 完成后将 Cloud Diff 应用到任务分支。
8. 检查修改路径、Diff 基础质量和敏感内容风险。
9. 创建提交、推送分支并创建 Draft PR。
10. 将 Issue 移到 `agent:review`，写回运行和 PR 链接。
11. 在重启、超时或任一外部调用中断后安全恢复，且不静默重复提交。

### 3.2 非目标

- 不替代 GitHub Projects、Linear、Jira 或完整 Kanban。
- 不托管聊天 UI；Codex session/task 只是一次运行记录。
- 不在控制节点执行 Issue 中任意提供的测试命令。
- 不直接连接 staging/production，不持有生产数据库或部署凭据。
- 不自动处理数据库迁移、IAM、DNS、Kubernetes、Terraform Apply 等高风险工作。
- 不提供多租户鉴权或公网服务。
- 不承诺 exactly-once；目标是**可检测、可对账、默认不重复的 at-most-once 自动提交**。

## 4. 事实来源与一致性规则

| 数据 | 权威来源 | SQLite 是否复制 |
|---|---|---:|
| 任务目标、范围、验收条件 | GitHub Issue | 保存提交时快照 Hash |
| 队列状态 | GitHub `agent:*` Label | 保存期望状态 |
| 人工决策 | 维护者 Issue 评论 | 保存纳入 Prompt 的评论 ID/Hash |
| 代码基线 | Git `base_sha` | 是 |
| Cloud 执行 | Codex Cloud Task ID | 是 |
| 修改内容 | Git branch/commit | 保存 SHA |
| 审核状态 | GitHub Draft PR/CI | 保存 PR 编号和最近 Check 状态 |
| 调度过程 | SQLite + journald | 是 |

一致性原则：

- Issue 是任务主键；一次 Issue 可以有多次 Run。
- Run 必须有独立 `run_token`、任务分支和 Cloud Task ID。
- 认领时冻结 Issue 正文、允许评论 ID 和内容 Hash；运行过程中这些输入发生变化时，当前结果不得自动交付，必须阻塞并由人决定是否开启新 Run。
- Cloud session/task 不是长期上下文的唯一来源；新 Run 应可从 Issue、Git 和上一 Run 摘要重建。
- GitHub 与 SQLite 无法形成跨系统事务，所有写操作按 Saga 处理并提供对账逻辑。
- 遇到外部结果不唯一或无法证明时，进入 `agent:blocked`，不自动再提交。

## 5. GitHub 队列协议

### 5.1 状态标签

每张参与调度的 Issue 必须且只能存在一个 `agent:*` 状态：

| 标签 | 含义 |
|---|---|
| `agent:ready` | 维护者确认可以自动执行 |
| `agent:dispatching` | 已认领，正在建立运行和提交 Cloud Task |
| `agent:running` | Cloud Task 已确定并执行/等待结果 |
| `agent:review` | Draft PR 已创建，等待人工审核 |
| `agent:needs-input` | 需求、范围或验收条件不完整 |
| `agent:blocked` | 技术失败或状态存在歧义，需要人工处理 |
| `agent:paused` | 不允许开始新运行 |
| `agent:discard` | 当前结果不得应用或交付；当前 CLI 不保证能取消 Cloud Task |

执行后端标签：

```text
exec:cloud
exec:local        # 仅保留，MVP 不实现
```

优先级标签：

```text
priority:p0
priority:p1
priority:p2
priority:p3
```

没有优先级时按 `p2`。选择顺序固定为：优先级升序（p0 最高）→ Issue 创建时间升序 → Issue 编号升序。

### 5.2 自动执行 Issue 模板

以下段落必须存在且非空；`允许修改路径` 必须使用仓库相对路径：

```markdown
## 目标

## 背景

## 范围

## 非目标

## 验收条件
- [ ] ...

## 允许修改路径
- src/...
- tests/...

## 验证命令
~~~bash
...
~~~

## 阻塞条件

## 部署限制
- 不部署 staging 或 production
- 不自动合并 PR
```

验证命令是交给 Cloud Agent 和 CI 的任务要求，**控制节点不得直接执行 Issue 提供的命令**。

### 5.3 安全准入

只有同时满足以下条件才可调度：

- 仓库位于静态 Allowlist。
- Issue 为 Open。
- 包含且仅包含一个状态标签 `agent:ready`。
- 包含 `exec:cloud`，且不包含其他 `exec:*`。
- `agent:ready` 由允许的维护者添加。
- 通过 GitHub Timeline/Audit Event 验证最近一次 `agent:ready` 添加者；仅检查 Issue 当前 Label 不足以证明准入来源。
- Issue 模板通过严格校验。
- 不存在未完成的阻塞依赖。
- 该仓库的活动 Run 数小于 `max_active`。
- Issue 没有其他活动 Run。

Prompt 只纳入：

- Issue 标题和正文；
- 允许维护者发布、且以 `/codex-context` 开头的评论；
- 调度器生成的固定约束；
- Run Token、仓库、分支和 Base SHA。

其他评论只用于人类讨论，不得进入 Prompt。

## 6. 内部状态机与数据模型

### 6.1 Run 状态

```text
discovered
  → claimed
  → branch_prepared
  → dispatching
  → running
  → result_ready
  → applying
  → validating
  → delivering
  → review

任意非终态
  → needs_input | blocked | discarded
```

关键不变量：

- 同一 `repository + issue_number` 最多一个活动 Run。
- 同一仓库活动 Run 不超过配置的 `max_active`。
- 未记录 `run_token` 前不得写外部状态。
- 未记录 `base_sha` 和任务分支前不得提交 Cloud Task。
- Cloud Task ID 不唯一或不可证明时不得再次自动提交。
- Diff 未通过路径和安全校验时不得 commit/push/create PR。
- PR 创建前必须能证明分支提交属于当前 Run。

### 6.2 SQLite 最小表

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  repository TEXT NOT NULL,
  issue_number INTEGER NOT NULL,
  attempt_no INTEGER NOT NULL,
  state TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  base_branch TEXT NOT NULL,
  base_sha TEXT,
  task_branch TEXT,
  cloud_environment_id TEXT NOT NULL,
  cloud_task_id TEXT UNIQUE,
  cloud_task_url TEXT,
  cloud_diff_sha256 TEXT,
  head_sha TEXT,
  pr_number INTEGER,
  retry_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_seen_at TEXT,
  last_error_code TEXT,
  last_error_redacted TEXT,
  UNIQUE(repository, issue_number, attempt_no)
);

CREATE UNIQUE INDEX one_active_run_per_issue
ON runs(repository, issue_number)
WHERE state NOT IN ('review', 'needs_input', 'blocked', 'discarded');

CREATE TABLE run_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_time TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
```

正式实现使用版本化 migration；只做加法迁移。升级前备份 SQLite 文件，并执行 `PRAGMA integrity_check`。

### 6.3 外部写入幂等标记

- Branch：`codex/issue-<number>-<run-token-prefix>`
- Commit trailer：`Codex-Run: <run_id>`
- PR Head Branch：任务分支唯一对应一个 Run
- Issue 评论：`<!-- codex-dispatcher:<run_id>:<event_type> -->`
- Prompt 第一段包含：`Run token: <run_id>`

写评论、创建 PR 或切换标签前先查询对应标记；存在则复用，不重复创建。

## 7. 一次 sweep 的确定性流程

### 7.1 Preflight

每次运行先检查：

1. 配置 schema 和文件权限。
2. SQLite migration 版本和完整性。
3. `git`、`gh`、`codex` 版本与 Pin 一致。
4. GitHub 凭据可读且仓库权限满足最小集合。
5. `codex cloud list --env <id> --json` 返回合同测试支持的 Schema。
6. 本地仓库 Mirror/Clone 没有未识别的脏状态。
7. Base Branch 受保护，服务身份不能 Force Push、删除受保护分支或绕过 PR 审核。
8. PR CI 的实际权限满足第 10.3 节的无 Secret 执行边界。

任一全局 Preflight 失败：本轮不认领新任务，只允许进行不会放大风险的只读对账。

### 7.2 先对账，再调度

处理顺序固定：

1. 对账 `dispatching/running/result_ready/applying/delivering` Run。
2. 修复可证明的 GitHub Label/评论/PR 漂移。
3. 处理已超时或结果歧义的 Run。
4. 计算每个仓库剩余容量。
5. 选择新的 `agent:ready` Issue。
6. 每个仓库本轮最多认领一个新任务。

### 7.3 认领与分支准备

1. 在 SQLite 事务中创建 Run，生成 `run_id` 和 Prompt Hash。
2. 把 GitHub Issue 从 `agent:ready` 切为 `agent:dispatching`。
3. 冻结并记录 Issue/允许评论快照 Hash；后续任何输入变化都要求人工重启任务。
4. 使用显式安全配置执行 `git fetch --prune --recurse-submodules=no`，解析并记录 `origin/<base_branch>` 的 `base_sha`。
5. 从该 SHA 创建本地任务 worktree/branch。
6. 推送空任务分支到 GitHub。
7. 记录分支已准备完成。

先推送任务分支的目的，是让 Cloud 从确定的输入分支运行，而不是在提交时临时解析不断变化的默认分支。

### 7.4 Cloud 提交

使用参数数组调用命令，禁止 `shell=True`。Prompt 优先从受限权限的标准输入或文件描述符传入，不放入命令行参数；若当前 CLI 不支持，Phase 0 必须评估 `/proc`/进程列表暴露并使用专用主机和 `hidepid` 等隔离后才能上线：

```text
codex cloud exec
  --env <environment_id>
  --branch <task_branch>
  <deterministic_prompt>
```

提交前必须把 Run 标为 `dispatching` 并保存：

- Environment ID
- Task Branch
- Prompt Hash
- 提交开始时间
- 提交前该 Environment 的 Task 列表快照 ID 集合

提交成功后解析 Task ID，立即保存，再写 GitHub 评论和 `agent:running`。

#### Cloud 提交崩溃窗口

`cloud exec` 当前没有 JSON 输出和调用方提供的幂等键，因此“Cloud 已创建 Task，但本地尚未保存 Task ID”无法靠本地事务完全消除。

恢复规则：

1. 查询同 Environment 在提交时间窗后新增的 Task。
2. 只有在 Branch、时间、标题/Prompt Token 等合同字段能唯一对应当前 Run 时才自动补记。
3. 没有候选：允许在配置的短暂宽限期后重查。
4. 多个候选或字段不足：转 `agent:blocked`，禁止自动重提。

这是 fail-closed 的 at-most-once 策略：宁可需要人工绑定 Task ID，也不在夜间静默运行两份任务。

### 7.5 Cloud 状态对账

优先使用 `codex cloud list --env <id> --json` 的结构化输出；`status` 的文本输出只用于诊断，不作为未经合同测试的状态机输入。

分类：

- Running/Pending：更新 `last_seen_at`，结束本次 sweep。
- Success/Ready：记录结果已准备，进入 Apply 阶段。
- Failed：写脱敏错误摘要，进入 `agent:blocked`。
- Unknown/Missing：达到宽限期前保持；超期后 `agent:blocked`。
- Issue 已是 `agent:discard`：不得 Apply，不得建 PR；保存 Task 终态后进入 `discarded`。

当前 CLI 无稳定 Cancel 命令，因此 `agent:discard` 的承诺是“不采纳结果”，不是“立即停止 Cloud 计费或运行”。

### 7.6 Apply、校验与交付

1. 确认 worktree 位于记录的任务分支和预期 Head。
2. 再次计算 Issue/允许评论快照 Hash；与提交快照不一致时阻塞，不 Apply。
3. 在 `0600` 的 Run 私有临时文件中接收 `codex cloud diff <task_id>`，流式计算 SHA-256；不得写入日志，保留期结束后安全删除。
4. 在干净 worktree 中运行 `codex cloud apply <task_id>`。
5. 如果在 Apply 阶段崩溃：
   - worktree 干净：可重试 Apply；
   - worktree Diff Hash 与 Cloud Diff 可证明一致：继续校验；
   - 其他情况：阻塞，不自动清理或覆盖。
6. 执行确定性的本地安全检查：
   - `git diff --check`
   - 修改文件集合为 `允许路径 ∩ 仓库配置 Allowlist`
   - 不命中硬 Denylist
   - 无子模块 URL、Git Hook、凭据文件或二进制异常增量
   - 可选 Secret Scanner 通过
7. 所有 Git 命令显式禁用 Hook 和危险本地配置：使用专用空 Git Config、`core.hooksPath=/dev/null`、禁用递归 Submodule，并拒绝仓库要求的未知 Clean/Smudge Filter、外部 Diff/Textconv 和自定义 Protocol。
8. 不执行 Issue 中提供的任意 Shell 命令；业务验证由 Cloud 和无 Secret PR CI 执行。
9. 创建包含 `Codex-Run` Trailer 的 Commit。
10. Push 任务分支。
11. 按 Head Branch 查询现有 PR；不存在才创建 Draft PR。
12. PR Body 包含 Issue、Run ID、Base SHA、Cloud Task URL、Diff Hash、已运行的调度器检查和 CI 状态；未知或未完成的 CI 必须标为 Pending，不得写成通过。
13. Issue 切为 `agent:review`，写回 PR。

硬 Denylist 默认包含：

```text
.env
.env.*
*.pem
*.key
id_rsa*
.git/**
.github/workflows/**
CODEOWNERS
infra/production/**
terraform/production/**
```

仓库可增加 Deny 条目，不得删除全局敏感条目。确需修改 CI、生产 IaC 或权限配置时，必须脱离无人值守队列人工执行。

## 8. 建议代码结构

```text
codex_cloud_task_scheduler/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── src/codex_dispatcher/
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── domain.py
│   ├── scheduler.py
│   ├── state_store.py
│   ├── migrations/
│   │   └── 001_initial.sql
│   ├── prompt_builder.py
│   ├── command_runner.py
│   ├── redaction.py
│   ├── trackers/
│   │   ├── base.py
│   │   └── github_cli.py
│   ├── executors/
│   │   ├── base.py
│   │   └── codex_cloud_cli.py
│   └── delivery/
│       └── git_pr.py
├── prompts/
│   └── cloud-task.md
├── config/
│   └── dispatcher.example.toml
├── systemd/
│   ├── codex-dispatcher.service
│   └── codex-dispatcher.timer
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   ├── fixtures/
│   └── fake_bin/
└── docs/
    ├── architecture.md
    ├── deployment.md
    ├── runbook.md
    └── test-plan.md
```

适配器接口只暴露确定性语义，例如：

```python
class Tracker:
    def list_ready(self, repository): ...
    def validate_task(self, task): ...
    def claim(self, task, run_id): ...
    def set_state(self, task, state): ...
    def upsert_run_comment(self, task, event, body): ...
    def find_pr_by_branch(self, repository, branch): ...

class Executor:
    def preflight(self, environment_id): ...
    def submit(self, request): ...
    def reconcile(self, external_task_id): ...
    def fetch_diff(self, external_task_id): ...
    def apply(self, external_task_id, worktree): ...
```

Jira 后续通过 REST API 实现 `Tracker`，不应让模型或 MCP 负责认领、状态转换和幂等控制。MCP 可用于 Agent 获取语义上下文，但不是调度事务协议。

## 9. 分阶段实施步骤

### Phase 0：合同验证 Spike

目标：先证明外部 CLI 能支撑自动化，不写业务调度器。

交付：

- 固定 `codex`、`gh`、`git` 版本矩阵。
- 保存脱敏的 `codex cloud list --json` Fixture 和 JSON Schema。
- 验证 `cloud exec` 输出中 Task ID 的可解析方式。
- 验证 Task List 是否包含可用于崩溃恢复的 Branch/时间/标题字段。
- 验证 `cloud diff` 和 `cloud apply` 对同一 Task 的行为。
- 建立一个专用私有 Fixture Repo 和测试 Cloud Environment。

Go/No-Go：

- Task ID 可稳定获得，或提交后可唯一对账：Go。
- 无法唯一识别提交结果：MVP 仍可做，但真实运行必须在该窗口 fail closed，并提供人工绑定命令。
- `diff/apply` 无法稳定脚本化：停止自动 PR，MVP 降级为只提交 Cloud Task 和回写 URL。

### Phase 1：领域模型、配置和 SQLite

交付：

- 配置解析与严格 Schema。
- Run 状态机和转换守卫。
- SQLite migration、事务和对账查询。
- 结构化日志与脱敏。
- `doctor`、`run-once --dry-run`、`status` 命令。

完成标准：所有领域逻辑可在无网络、无 GitHub、无 Codex 的测试中运行。

### Phase 2：GitHub Tracker Dry-run

交付：

- `gh` 命令封装，全部使用参数数组和 JSON 输出。
- Issue 模板解析、安全准入、排序和并发计算。
- Dry-run 只报告“会认领哪张 Issue”，不修改 GitHub。
- Fake `gh` 集成测试。

完成标准：在真实 Allowlist 仓库上连续运行 24 小时 Dry-run，无误选、无外部写入、无凭据泄漏。

### Phase 3：认领、分支和 Cloud Submit

交付：

- GitHub 状态切换和幂等评论。
- Mirror/Worktree/Branch 管理。
- 确定性 Prompt Builder 和 Prompt Hash。
- Cloud 提交、Task ID 保存与崩溃窗口恢复。
- 暂不自动 Apply/PR；结果只回写 Cloud URL。

完成标准：Fixture Repo 连续处理 10 次测试 Run，无重复 Cloud Task；注入每个崩溃点后能恢复或明确阻塞。

### Phase 4：Diff Apply 与 Draft PR

交付：

- Cloud Diff 获取与 Hash。
- 安全 Apply、路径校验、Denylist 和 Secret Scan Hook。
- Commit、Push、Draft PR、CI 状态回写。
- `agent:discard` 行为。
- 无 Secret CI 权限审计和恶意源码泄露测试。

完成标准：Fixture Repo 的只改 README 任务自动形成 Draft PR；越界路径、敏感文件和冲突 Patch 均被阻止。

### Phase 5：systemd 部署与运维

交付：

- 加固后的 service/timer。
- 备份、升级、回滚和灾难恢复 Runbook。
- 结构化 journald 查询示例。
- 24～48 小时 Soak Test。

### Phase 6：可选扩展

按优先级：

1. Jira/Linear Tracker Adapter。
2. Linux 本地 `codex exec --json` Executor。
3. 通知渠道。
4. 多仓库并发优化。

多 Runner、自动合并和自动部署不因完成 MVP 而自动进入范围，必须重新做风险评审。

## 10. Linux 部署步骤

### 10.1 主机规格

Cloud 控制面建议：

```text
1 vCPU
2 GiB RAM
20～30 GiB SSD（仓库较大时按 Clone/Worktree 容量增加）
1 GiB swap
Debian/Ubuntu Server，无 GUI
```

这只适用于 Cloud Executor。若启用本地 `codex exec` 并在 Linux 上构建目标项目，资源需求由目标项目决定，建议从 4 vCPU / 8 GiB RAM 起评估。

### 10.2 目录和身份

使用专用 Unix 用户，不与个人 Shell 或生产服务共用：

```text
/opt/codex-dispatcher/                 只读应用版本
/etc/codex-dispatcher/config.toml      非敏感配置
/var/lib/codex-dispatcher/state.db     SQLite
/var/lib/codex-dispatcher/repos/       Mirror/Worktree
/var/lib/codex-dispatcher/codex-home/  专用 Codex 登录状态
/run/codex-dispatcher/                 锁文件
```

GitHub Token 使用 systemd Credential 或权限为 `0600` 的独立凭据文件，不写入 TOML、仓库、Prompt、Issue 或 EnvironmentFile。

### 10.3 凭据权限

优先使用专用 GitHub App Installation Token；个人 MVP 也可使用 Fine-grained PAT，但必须限制到 Allowlist 仓库。注意：GitHub Token 的 Contents 权限通常是**仓库级**，不能真正限制为“仅任务分支”；分支级边界必须由受保护分支、Ruleset、专用服务身份和禁止 Force Push/Delete 共同实现。最低需要：

- Metadata：Read
- Issues：Read/Write
- Contents：Read/Write（权限技术上覆盖仓库内容；应用只允许写任务分支）
- Pull requests：Read/Write
- Checks/Actions：Read（如果需要读取 CI）

Codex 登录属于 `codex-dispatcher` 用户，并将 `CODEX_HOME` 指向专用 StateDirectory。Cloud Environment 不配置生产数据库、生产 SSH、部署或高权限云凭据。

PR CI 必须满足：

- `GITHUB_TOKEN` 默认 `contents: read`，仅给 Check 所需的最小权限。
- 不向 `pull_request` 工作流提供仓库/环境 Secret；禁止使用会在不可信代码上暴露 Secret 的 `pull_request_target` 设计。
- 不调度到可访问内网、生产网络或主机持久凭据的自托管 Runner；Fixture/初期业务仓库优先使用 GitHub 托管 Runner。
- 构建容器没有 Docker Socket、云实例角色、Kubeconfig、SSH Agent 或持久 Volume。
- CI 结果只是 PR 审核证据；调度器不能把“Job 已启动”或“状态未知”表述为测试通过。

### 10.4 安装和 Preflight

部署时执行而不是在本文档阶段执行：

1. 安装并固定 Python、Git、`gh` 和 `codex` 版本。
2. 以专用用户完成 `codex login`。
3. 配置 GitHub Credential。
4. 部署版本化应用目录和虚拟环境。
5. 写入配置，权限设为 root 可写、服务用户可读。
6. 运行：

```bash
codex-dispatcher doctor --config /etc/codex-dispatcher/config.toml
codex-dispatcher run-once --dry-run --config /etc/codex-dispatcher/config.toml
```

7. `doctor` 必须检查版本 Pin、凭据可用性、Environment JSON Schema、仓库权限、磁盘空间、SQLite 和工作目录权限。

### 10.5 systemd 单元建议

```ini
# /etc/systemd/system/codex-dispatcher.service
[Unit]
Description=Codex Cloud task dispatcher sweep
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=codex-dispatcher
Group=codex-dispatcher
WorkingDirectory=/opt/codex-dispatcher/current
StateDirectory=codex-dispatcher
RuntimeDirectory=codex-dispatcher
UMask=0077
Environment=PYTHONUNBUFFERED=1
Environment=CODEX_HOME=/var/lib/codex-dispatcher/codex-home
LoadCredential=github_token:/etc/credstore/codex-dispatcher-github-token
ExecStart=/usr/bin/flock -n /run/codex-dispatcher/dispatcher.lock /opt/codex-dispatcher/current/.venv/bin/codex-dispatcher run-once --config /etc/codex-dispatcher/config.toml
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
```

```ini
# /etc/systemd/system/codex-dispatcher.timer
[Unit]
Description=Run Codex Cloud task dispatcher periodically

[Timer]
OnBootSec=2min
OnUnitInactiveSec=2min
Persistent=true
RandomizedDelaySec=15s
Unit=codex-dispatcher.service

[Install]
WantedBy=timers.target
```

正式启用前应使用 `systemd-analyze security codex-dispatcher.service` 检查加固项，并验证 `StateDirectory` 在 `ProtectSystem=strict` 下仍可写。

### 10.6 发布策略

```text
/opt/codex-dispatcher/releases/<version>
/opt/codex-dispatcher/current -> releases/<version>
```

发布流程：

1. 停止 Timer，等待当前 oneshot 结束。
2. 使用 SQLite Online Backup API 或 `sqlite3 state.db '.backup ...'` 备份（WAL 模式下禁止只复制主 DB 文件），并执行 Integrity Check。
3. 安装新版本到独立 Release 目录。
4. 执行 migration dry-run、单元/合同测试和 `doctor`。
5. 切换 `current` Symlink。
6. 手工运行一次 Dry-run，再运行一次真实 Sweep。
7. 恢复 Timer，观察至少两个周期。

生产启用、停用或切换版本属于外部状态变更，执行前仍需明确授权。

## 11. 可测试的验收标准

### 11.1 单元与集成测试门槛

- Python 类型检查、Lint、单元测试全部通过。
- 核心状态机、排序、模板解析、幂等和恢复分支覆盖率不低于 95%。
- 全项目语句覆盖率不低于 85%；不得通过排除核心文件或弱化断言达标。
- 所有时间、UUID、命令和外部响应均可注入 Fake，测试不得依赖真实网络。
- 集成测试使用临时 Git Repo、临时 SQLite 和 Fake `gh`/`codex` 可执行文件。

### 11.2 验收用例矩阵

| ID | 场景 | 可观察通过标准 |
|---|---|---|
| AC-001 | 空队列 | Sweep 退出码 0；无 GitHub/Cloud 写调用 |
| AC-002 | 非 Allowlist 仓库 | Issue 不被认领；记录拒绝原因 |
| AC-003 | 缺少必填段落 | Issue 进入 `agent:needs-input`；Cloud 调用数为 0 |
| AC-004 | Ready Label 非允许维护者添加 | 不认领；记录安全拒绝事件 |
| AC-005 | 多个 `agent:*` 状态 | 不认领；进入可诊断异常，不自行猜状态 |
| AC-006 | 优先级排序 | p0 先于 p1/p2/p3；同级按创建时间和编号稳定排序 |
| AC-007 | 单仓库并发 | 已有 Active Run 时，同仓库第二张 Issue 不提交 |
| AC-008 | 跨仓库并发 | 未超过全局上限时，两个仓库可各运行一个任务 |
| AC-009 | Dry-run | 输出候选和原因；GitHub、Git Remote、Cloud 写调用均为 0 |
| AC-010 | 认领崩溃恢复 | SQLite 已有 Run、Issue 仍 Ready 时，下轮补齐状态而不新建 Run |
| AC-011 | 分支幂等 | 同 Run 重试复用相同分支和 Base SHA，不新建第二分支 |
| AC-012 | Cloud 正常提交 | Task ID 在任何 `agent:running` 写入前持久化 |
| AC-013 | Cloud 提交后崩溃且唯一候选 | 下轮唯一关联原 Task，Cloud Submit 调用总数为 1 |
| AC-014 | Cloud 提交后崩溃且候选歧义 | Run 进入 Blocked；Cloud Submit 调用总数保持 1 |
| AC-015 | Cloud Task 运行中 | 只更新 Last Seen；不 Apply、不建 PR |
| AC-016 | Cloud Task 失败 | Issue 进入 Blocked；评论包含脱敏错误码和 Task URL |
| AC-017 | Discard 运行结果 | 不调用 Apply、不 Commit、不 Push、不建 PR |
| AC-018 | Apply 正常 | Applied Diff Hash 与记录的 Cloud Diff Hash 一致 |
| AC-019 | Apply 中途崩溃且 Diff 一致 | 下轮从校验继续，不重复 Apply |
| AC-020 | Apply 后出现未知脏改动 | Run Blocked；不清理、不覆盖、不交付 |
| AC-021 | 越界路径 | 修改路径超 Allowlist 时 Blocked；无 Commit/Push/PR |
| AC-022 | 命中硬 Denylist | `.env`/密钥/Workflow/生产 IaC 修改被拒绝 |
| AC-023 | Diff 格式错误 | `git diff --check` 失败时不交付 |
| AC-024 | PR 创建前崩溃 | 若分支已 Push，下轮按 Head Branch 创建一次 PR |
| AC-025 | PR 创建后崩溃 | 下轮发现既有 PR，不创建重复 PR，补齐 Issue 状态 |
| AC-026 | 评论幂等 | 同 Run/Event Marker 最多一条评论 |
| AC-027 | 日志脱敏 | PAT、Authorization Header、Codex Auth、Prompt 全文不出现在日志 |
| AC-028 | Issue Shell 注入 | 验证命令只进入 Cloud Prompt；控制节点不执行该命令 |
| AC-029 | CLI Schema 漂移 | `doctor` 失败；本轮禁止新 Dispatch |
| AC-030 | CLI 版本漂移 | 与 Pin 不一致时 Preflight 失败，除非显式更新合同 Fixture |
| AC-031 | SQLite 损坏 | 不认领新任务；给出恢复指引；不自动新建空库覆盖 |
| AC-032 | 磁盘空间不足 | 低于阈值时不认领；已有任务只做只读对账 |
| AC-033 | Timer 重叠 | 第二实例因 systemd/flock 不运行；无重复提交 |
| AC-034 | 主机重启 | 下一周期恢复 Running Run；不重复 Cloud Submit |
| AC-035 | 无自动高风险动作 | 全链路不存在 merge、deploy、release、生产 DDL/DML 调用 |
| AC-036 | Issue 快照漂移 | 运行中修改正文或允许评论后，结果不得 Apply；Run 进入 Blocked/Needs Input |
| AC-037 | Ready 准入审计 | 非允许维护者添加或重新添加 Ready Label 时不认领 |
| AC-038 | Git Hook/Filter 注入 | 仓库 Hook、未知 Filter、递归 Submodule 或自定义 Protocol 均不执行 |
| AC-039 | Prompt 进程可见性 | Prompt 不出现在日志；若作为 argv 传递，非服务用户无法从进程列表读取 |
| AC-040 | PR CI Secret 隔离 | 恶意源码尝试读取 Secret/云身份/内网时均不可得且测试失败可见 |
| AC-041 | 受保护分支 | 服务身份对默认分支的 Direct/Force Push/Delete 均被 GitHub 拒绝 |
| AC-042 | WAL 一致备份 | 并发写入期间生成的备份可通过 Integrity Check 并恢复全部已提交 Run |

### 11.3 Live Smoke Test

使用专用 Fixture Repo，不得直接用真实业务仓库做首测：

1. Fixture Repo 有 `README.md` 和一个验证 README 标记的 CI。
2. 创建 Issue：只允许修改 README 中一个确定位置。
3. 添加 `exec:cloud` 和 `agent:ready`。
4. 预期在两个 Timer 周期内出现 Cloud Task URL。
5. Cloud Task 完成后出现唯一任务分支和唯一 Draft PR。
6. PR 仅修改 README 指定位置。
7. CI 通过；Issue 为 `agent:review`。
8. 重跑多个 Sweep，不新增 Cloud Task、Branch、Commit、PR 或重复评论。

然后执行负向 Smoke：

- 要求修改 `.github/workflows/ci.yml`，预期被 Denylist 阻止。
- 在 Cloud Submit 后、保存 Task ID 前注入进程退出，验证唯一恢复或 Blocked。
- 在 PR 创建后、Issue 更新前退出，验证复用 PR。

### 11.4 Soak 与上线门槛

上线真实业务仓库前必须满足：

- 24 小时 Dry-run，无误选任务。
- Fixture Repo 至少 10 个成功 Run、5 个故障注入 Run。
- 48 小时 Timer Soak，无重复 Cloud Task 或 PR。
- 主机重启恢复测试通过。
- 日志抽检无 Secret、Authorization Header 或完整 Prompt。
- 回滚演练通过。
- 人工确认第一批真实任务均为低风险、可回滚、小 Diff。

## 12. 可观测性与运维接口

每条结构化日志至少包含：

```text
timestamp
level
event
run_id
repository
issue_number
state_before
state_after
external_command
duration_ms
result_code
error_code
```

不得记录：

- Token、Cookie、Authorization Header。
- `.env` 内容。
- 完整 Issue/Prompt/Cloud 输出。
- 任意私钥或生产日志。

CLI 运维入口：

```text
codex-dispatcher doctor
codex-dispatcher run-once --dry-run
codex-dispatcher status [--run-id ...]
codex-dispatcher reconcile [--run-id ...]
codex-dispatcher bind-cloud-task --run-id ... --task-id ...
codex-dispatcher retry --run-id ...      # 仅对明确可重试的本地步骤
```

`bind-cloud-task` 是提交崩溃窗口的人工恢复入口；必须校验 Environment、Branch 和时间范围并写审计事件。

## 13. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Codex Cloud CLI 为实验性/Schema 漂移 | 自动化误判 | 固定版本、合同 Fixture、Preflight Fail Closed |
| `cloud exec` 无 JSON/幂等键 | 重复 Cloud Task | 提交前落库、前后 Task 集合、唯一对账、歧义即阻塞 |
| GitHub Label 非事务认领 | 状态短暂漂移 | 单主机 + flock + SQLite Saga + 对账 |
| Issue/评论 Prompt Injection | 越权修改或泄密 | 维护者准入、固定 Prompt、路径 Allow/Deny、无生产凭据 |
| Cloud 测试结果不可结构化获取 | 虚假“测试通过” | 不声称未知结果；PR CI 为自动验收事实 |
| Apply 后控制节点被注入命令 | 主机失陷 | 不执行 Issue 命令；只运行固定 Git/Codex/GH 参数数组 |
| Base Branch 漂移 | PR 冲突 | 先推送固定 Base SHA 的任务分支；冲突可见，不自动 Rebase |
| SQLite 单点损坏 | 丢失运行映射 | 定期备份、Integrity Check、GitHub/Cloud 对账 Runbook |
| 主机或网络离线 | 延迟 | systemd Persistent Timer；恢复后对账；Cloud Run 不依赖控制节点在线 |
| 自动化范围扩大到生产 | 高风险状态变化 | 代码级禁止 merge/deploy/生产路径，需另行人工流程 |

## 14. 回滚与停机

最安全的紧急停止方式是停用 Timer；不会取消已经运行的 Cloud Task，但会停止新任务和结果应用。

回滚原则：

1. 不删除 Cloud Task、任务分支、Commit 或 Draft PR。
2. 保留 SQLite 备份和 journald 审计日志。
3. 将 `agent:dispatching/running` Issue 切到人工 `agent:paused` 前先记录 Run ID。
4. 回滚应用版本时使用旧 Release Symlink；数据库 migration 必须保持向后兼容。
5. 已创建 Draft PR 由人类关闭或保留，不自动删除。
6. 恢复后先运行 `doctor` 和 `reconcile --dry-run`，再恢复 Timer。

## 15. 最终完成定义

只有同时满足以下条件，MVP 才算完成：

- 本文 Phase 0～5 的交付物存在并通过审查。
- AC-001～AC-042 全部自动化或有明确的 Live Test 证据。
- Fixture Repo Smoke、故障注入、重启恢复和 48 小时 Soak 通过。
- 真实业务仓库只启用低风险任务，且首批全部由人工检查 Draft PR。
- 没有自动 Merge、Deploy、生产凭据或高风险路径写入能力。
- 任一外部状态不确定时，系统能明确 Blocked，而不是重复执行或宣称成功。

项目成功标准不是“夜里一直有模型运行”，而是：

> 一张范围完整、可验证的 GitHub Issue，能在不依赖 MacBook 在线状态的情况下，确定性地产生一个可审查、可追踪、可回滚且不自动进入生产的 Draft PR；任何失败都能被定位和安全恢复。
