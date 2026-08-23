# Codex SSH CLI Task Scheduler：实施、部署与验收方案

> 状态：已确认的实施基线
>
> 日期：2026-08-14
>
> 目标平台：Linux Control Host + 独立 Linux SSH Runner

## 1. 结论与范围

项目从 Codex Cloud 调度器调整为远程 Linux Codex CLI 调度器。Codex Cloud 不再处于执行
主路径，已有 Cloud 代码在迁移期间可以保留但默认禁用。

```text
GitHub Issue / Label                 唯一人工输入和项目事实
             │
             ▼
Linux Control Host
├── Dispatcher                      单线程生命周期与对账
├── SQLite                          WorkItem、Turn、恢复锚点
├── Publisher                       机械校验并 push 确切 commit
└── SlackReporter                   只读执行详情
             │ restricted SSH
             ▼
Dedicated Linux Runner
└── Codex CLI + per-Issue directory + runner-wide CODEX_HOME
             │ bundle/result pulled by control host
             ▼
Stable branch + one Draft PR + CI   代码审核和人工合并入口
```

第一版明确边界：

- Tracker 只实现 GitHub Issues。
- Executor 是通过 SSH 调用另一台 Linux 主机上的 `codex exec --json`。
- Dispatcher、Publisher、SQLite 和 SlackReporter 位于同一台 Linux Control Host。
- GitHub 是唯一人工输入渠道；Slack 严格只出不进。
- 一个可执行 Issue 在完成前始终绑定同一个分支、目录、Codex session、Slack thread 和 PR。
- 全局只允许一个活动 Codex turn，不按仓库并发。
- Codex 负责修改、测试和本地 commit；Publisher 只校验并 push 已有 commit。
- Codex Runner 没有 GitHub 写凭据。
- 不自动 merge、release、deploy，也不接触生产基础设施或生产凭据。
- 第一阶段 Runner 不做 Docker、UID 或 systemd 沙箱限制；整台 Runner 视为可丢弃边界。
- Docker 隔离是后续加固阶段，不能作为第一阶段已经存在的安全控制。

## 2. 已确认的产品约定

### 2.1 稳定 1:1:1 映射

```text
1 executable Issue
= 1 WorkItem
= 1 stable branch
= 1 runner directory
= 1 Codex CLI session
= 1 Slack thread
= 0/1 Draft PR
```

Issue 中等待人工补充、重新进入 ready、规划、实现、测试、修复同一 PR 的过程都只是新的
Turn，不产生新 WorkItem。只有原 Issue 完成且 PR 已合并后，新 bug 或新需求才创建新 Issue。

稳定分支不包含 attempt number：

```text
codex/issue-<number>-<stable-issue-key-prefix>
```

`stable-issue-key-prefix` 从仓库身份和不可变 Issue 身份确定性计算，不能使用创建时间或本地
随机数。相同 Issue 在任何状态流转后都解析成同一分支。

迁移例外：旧调度器已经持久化且已创建远端 task branch 的 Issue，不重新计算或替换分支。
只有旧 SQLite 中的 repository/Issue/branch/base/head 与远端 ref 精确一致时，才以
`task_branch_source=migrated` 导入；新 Issue 使用 `derived`。两种绑定一经持久化都不可替换。

### 2.2 项目视角和任务视角

- GitHub Issue/PR 是项目视角，也是所有人工输入和审批的入口。
- Slack 是任务执行细节视角。Issue 保存 Slack thread 直达链接。
- 人工 Slack 消息无论内容如何都不能进入 Prompt、改变状态或触发执行。
- 建议一个项目使用一个私有 Slack channel，每个 WorkItem 使用一个 thread，避免 channel
  数量无限增长。

### 2.3 单线程

只有一个 Dispatcher 实例可以运行，只有一个 Turn 可以处于 `starting/running/publishing`。
实现使用：

1. systemd 只启动一个服务实例；
2. 进程启动时获取全局 `flock`；
3. SQLite 唯一约束拒绝第二个活动 Turn；
4. SSH Runner 再使用一个全局执行锁拒绝重入。

SQLite 不承担多调度器租约或并发抢占协议。

## 3. 信任边界

### 3.1 Control Host（高信任）

保存：

- GitHub App 私钥或受限凭据；
- SQLite 和备份；
- 仓库 mirror/quarantine；
- Publisher；
- Slack bot token；
- Runner SSH 私钥和固定 host key。

不得执行：

- Issue 提供的验证命令；
- Runner 返回的脚本或二进制；
- Git hook、submodule helper、clean/smudge filter、textconv 或仓库自定义命令；
- Agent 生成项目的构建和测试。

### 3.2 Runner Host（低信任）

第一阶段直接运行 Codex CLI，不增加 Docker 或 per-task UID 限制。每个 WorkItem 使用不同目录：

```text
/srv/codex-runner/
├── app/              # protected shared CODEX_HOME
├── etc/
├── run/
└── work-items/

/srv/codex-runner/work-items/<repository-key>/issue-<number>/
├── repo/
└── runner-state/
```

`CODEX_HOME=/srv/codex-runner/app` 在 Runner 范围共享，权限为 `0700`；Codex 登录状态只在该目录
原地初始化和刷新，不复制到 WorkItem。WorkItem 与 Codex 上下文的绑定由持久化的精确 session
ID 完成。

`work-items/.registry/` 保存 WorkItem 到上述确定性相对目录的严格映射；`.staging/` 和
`.exports/` 仅保存同文件系统上的短期原子安装/导出文件。source bundle 在 PREPARE 完成前
删除，result bundle 在响应 frame 生成后删除。

风险接受：运行用户能够访问的所有 Runner 数据都可能被删除或外传，磁盘可能被填满，Runner
系统可能需要重建。Runner 因此不得保存：

- GitHub 写凭据；
- Control Host 登录凭据；
- 生产、部署、数据库、云、Kubernetes 或个人凭据；
- 个人文件或挂载的 Control Host 文件系统；
- 能从 Runner 主动登录 Control Host 的 SSH key。

Runner 丢失时，允许损失尚未 Publisher checkpoint 的改动和本地 Codex session 上下文。
GitHub 已发布 commit、Issue/PR、Control Host SQLite 和审计记录不得受影响。

### 3.3 Rootless Docker 隔离

当前 `s3` Fixture 路径已增加：

- 每个活动 WorkItem 一个容器；
- 只挂载该 WorkItem 目录；
- 持久化 `repo/` 和 `runner-state/`；
- CPU、内存、PID、磁盘和执行时限；
- 丢弃 capabilities，禁止 `--privileged`；
- 不挂载 Docker socket、SSH agent、Control Host 或其他 WorkItem；
- 禁止访问内网和云 metadata，按需限制外网；
- 容器内可拥有完成开发所需权限，但这些权限不能扩展到 Runner Host。

Runner 级 `auth.json` 只作为宿主机种子。每个 WorkItem 在自己的 `codex-home` 中持有可写
副本，容器外的不可变 sidecar 绑定 WorkItem 与种子摘要；不得挂载整个 Runner 级
`CODEX_HOME` 或共享可写认证文件。每次 START/RESUME 前必须在实际执行将使用的同一个
WorkItem `CODEX_HOME` 内通过 ChatGPT login-status 门禁；失败即阻断，不能启动 Codex 或盲目
重试。Fixture live 成功不自动准入高价值仓库，剩余 repository-class recovery 和精确目标
read-back 门槛以 `deploy/runner/DOCKER.md` 的矩阵为准。

## 4. GitHub 协议

### 4.1 状态标签

每个参与调度的 Issue 必须恰好有一个状态标签：

| 标签 | 含义 |
|---|---|
| `agent:ready` | 维护者确认可以开始或继续原 session |
| `agent:dispatching` | 正在冻结输入并启动一个 Turn |
| `agent:running` | 原 Codex session 正在执行 |
| `agent:needs_input` | 原 session 暂停，等待 Issue 中补充信息 |
| `agent:review` | Draft PR 等待人工审核，仍可恢复原 session |
| `agent:blocked` | 外部状态歧义或安全校验失败 |
| `agent:paused` | 人工暂停，不执行新 Turn |
| `agent:completed` | PR 已合并且 WorkItem 关闭 |

执行后端标签：

```text
exec:ssh-cli
```

旧 `exec:cloud` 只用于历史记录，不触发新任务。

### 4.2 Issue 输入

Issue 模板继续使用：目标、背景、范围、非目标、验收条件、允许修改路径、验证命令、阻塞
条件和部署限制。

`验收条件` 保持 Markdown 兼容。普通文本或 `- [ ] ...` 条目会逐项保存为人工
`unverified` 条件；需要机械判定时只能使用以下严格语法，ID 在同一 Issue 内唯一：

```text
- [AC-1] required-check: fixture
- [AC-2] changed-paths-within-allowed
- [AC-3] task-head-published
```

`required-check` 参数必须同时存在于仓库的 `required_checks` 配置。拼写错误、未知谓词、
重复 ID 或无参数/多参数都在 claim 前拒绝，不能降级成普通文本。

进入 Codex Prompt 的人工输入只有：

- Issue 标题和正文；
- allowlist 维护者发布的 `/codex-context` 评论；
- 当前 PR 中经维护者确认并同步回 Issue 的处理要求；
- Dispatcher 生成的固定安全约束和当前 Git 锚点。

其他 GitHub 评论和全部 Slack 内容不进入 Prompt。Issue 中的命令只是 Runner/Codex 的任务
要求，Control Host 不能执行。

### 4.3 状态变化和输入漂移

每次 Turn 开始前冻结 Issue revision、允许评论 ID、Prompt SHA-256 和输入 HEAD。Turn 运行中
Issue 再次变化时：

- 不创建新 session、目录或分支；
- 当前 Turn 结果不自动发布；
- WorkItem 进入 `needs_input` 或 `blocked`；
- 下一次显式 `agent:ready` 在原 session 中发送新的确定性输入。

## 5. WorkItem、Turn 与状态机

### 5.1 WorkItem 状态

```text
discovered
  → preparing
  → ready
  → running
  → waiting_input
  → review
  → completed

任意非 completed 状态
  → blocked | paused

blocked | paused
  → preparing  # 没有持久 PREPARE ACK
  → ready      # 已有持久 PREPARE ACK

waiting_input | review
  → ready
```

`completed` 是终态。自动化不得把 completed WorkItem 重新打开；新工作使用新 Issue。
`preparing → ready` 的持久状态事件是 Runner 精确 PREPARE ACK 的 provenance。PREPARE 明确
拒绝后，人工重新 `agent:ready` 必须从持久化 `base_sha` 重建 exact source bundle，并先回到
`preparing`；不得把通用 `blocked → ready` 误当成 Runner 已准备完成。读取 exact bundle 失败
发生在 claim 之前，不改变 Issue 或 WorkItem 状态。
如果 PREPARE 成功后的 START 又在创建 Codex session 前被明确拒绝，允许使用新的
implementation generation 重试 START，而不伪造 Handoff。该例外必须在 planner 和
StateStore 事务内分别证明：所有前代都没有 session、checkpoint、AgentResult、usage 或
handoff，唯一 Turn 都以 `runner_request_rejected` 结束，且锚点仍为持久化 base SHA；任一
证据不完整或不一致都要求显式恢复。

### 5.2 Turn 状态

```text
planned → starting → running ───────────────→ checkpointing → published → finished
              │        │                              │             │
              └────────┴→ reconciling ────────────────┘             └→ needs_input
                           │
                           └→ running | needs_input | failed | blocked | interrupted
```

一个 WorkItem 可以有多个 Turn，但同一时刻全局最多一个 Turn 活动。Turn number 只用于审计，
不进入 branch、directory、session 或 PR 身份。`reconciling` 仍属于活动 Turn 并占用全局唯一
槽位；它只能发送 `status`，不得再次发送原 Prompt。

### 5.3 最小持久化字段

现有 `runs` 表保留以兼容已提交 schema。新增表使用 additive migration：

```sql
CREATE TABLE work_items (
  work_item_id TEXT PRIMARY KEY,
  repository TEXT NOT NULL,
  issue_number INTEGER NOT NULL,
  issue_node_id TEXT NOT NULL,
  state TEXT NOT NULL,
  base_branch TEXT NOT NULL,
  task_branch TEXT NOT NULL,
  runner_directory TEXT NOT NULL,
  codex_session_id TEXT UNIQUE,
  slack_channel_id TEXT,
  slack_thread_ts TEXT,
  pr_number INTEGER,
  base_sha TEXT NOT NULL,
  last_published_sha TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(repository, issue_number),
  UNIQUE(repository, task_branch),
  UNIQUE(repository, pr_number)
);

CREATE TABLE turns (
  turn_id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL,
  turn_number INTEGER NOT NULL,
  state TEXT NOT NULL,
  issue_revision TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL,
  input_head_sha TEXT NOT NULL,
  included_comment_ids_json TEXT NOT NULL DEFAULT '[]',
  issue_allowed_paths_json TEXT NOT NULL DEFAULT '[]',
  output_sha256 TEXT,
  output_head_sha TEXT,
  result_status TEXT,
  result_summary TEXT,
  error_code TEXT,
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  UNIQUE(work_item_id, turn_number)
);

CREATE TABLE slack_deliveries (
  deduplication_key TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL,
  turn_id TEXT,
  kind TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  thread_ts TEXT,
  payload_sha256 TEXT NOT NULL,
  state TEXT NOT NULL,
  message_ts TEXT,
  permalink TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(work_item_id) REFERENCES work_items(work_item_id),
  FOREIGN KEY(turn_id) REFERENCES turns(turn_id),
  UNIQUE(channel_id, message_ts)
);
```

迁移只增加表、列和索引，不删除或重新解释既有 Cloud 字段。Cloud 数据保留为历史审计信息。

## 6. SSH Runner 协议

### 6.1 第一阶段操作集合

SSH 远端入口只接受版本化 JSON 请求：

```text
prepare     创建或验证 WorkItem 目录
start       启动首次 codex exec
resume      恢复已绑定 session
status      读取结构化状态
export      输出 Git bundle 和结果 manifest
stop        终止当前 Codex 进程，不删除目录
archive     仅完成后的显式清理入口，第一阶段默认禁用
```

即使第一阶段不做 Docker 限制，也必须从一开始禁止：

- `shell=True`；
- 将 Issue/Prompt 放进 argv；
- 任意远端命令、路径、环境变量、Git URL 或 refspec；
- 关闭 SSH host key 校验；
- SSH agent、端口或 X11 转发；
- Runner 主动连接 Control Host。

Control Host 的 OpenSSH 调用固定忽略用户配置，启用 batch/public-key 和严格 host-key 校验，
固定 `known_hosts`、identity、用户、主机、端口与
`/srv/codex-runner/bin/codex-runner-v1`，并关闭 agent/X11/全部
forward、ProxyJump、local command 和 TTY。生产默认固定 `ProxyCommand=none`。当前 Mac
Fixture 因多级跳板加速可显式配置受保护的 `/opt/homebrew/bin/assh`，此时适配器只生成固定
形状 `assh connect --port=%p %h`，并要求显式、受保护的 `assh_home`，只将该目录作为 `HOME`
传给 SSH 子进程以定位 assh 配置；不继承其他 Dispatcher 环境，也不提供通用 ProxyCommand
字符串入口。Issue、Prompt、路径、Git URL 和任意 Runner 参数都不能进入 SSH argv。

JSON 请求、Prompt 和 source bundle 通过同一个 stdin 的三个独立长度字段传输；response JSON
和可选 result bundle 使用两个独立长度字段。frame 包含协议 magic/version 和各部分 byte
length。PREPARE 必须携带 `repository/issue_number/task_branch/base_sha` 以及 source bundle 的
size/SHA-256；START/RESUME 只能携带 Prompt；其他请求不能携带输入 artifact。解析器拒绝未知
版本、长度不符、尾随字节、操作/内容错配和 hash 不符。输出设字节上限并做脱敏。

Control Host 从可信 mirror 将精确 `base_sha` 复制到一次性 bare repository，再生成只公布固定
base ref 的 self-contained bundle；不在 mirror 中创建临时 ref。Runner 只从该 bundle 导入，
拒绝 submodule 和声明 filter/diff/working-tree-encoding driver 的 `.gitattributes`，创建固定任务
分支且不配置 remote。因此 Runner 无需任何 GitHub 凭据。

远端 forced command `/srv/codex-runner/bin/codex-runner-v1` 不接受参数；wrapper 清空继承环境、
禁用 Python user site 和不安全的当前目录导入，只读取权限受保护、字段严格的
`/srv/codex-runner/etc/config.json`。可执行文件和 output schema 必须解析到不可被 group/world
写入的普通文件；配置的 runner-wide `CODEX_HOME` 必须是当前用户拥有且权限不超过 `0700`
的目录。每次 START/RESUME 在运行 Codex 前原子持久化请求，运行期间持有全局非阻塞
`flock`，完成后先持久化 canonical reply 再写 SSH 响应。相同 `turn_id` 的完全相同请求只读回
结果；冲突请求被拒绝；残留 `executing` 记录只返回 `unknown`。wrapper 忽略 SIGHUP 以提高 SSH
断线后落盘结果的概率，但主机/进程崩溃仍按未知结果处理。

### 6.2 Codex CLI 调用

首次 Turn：

```text
cwd=<work-item>/repo
CODEX_HOME=/srv/codex-runner/app
codex exec --json --dangerously-bypass-approvals-and-sandbox \
  --output-schema <fixed-schema> -
```

捕获 `thread.started.thread_id`，只能在 `codex_session_id IS NULL` 时绑定。

后续 Turn：

```text
cwd=<work-item>/repo
CODEX_HOME=/srv/codex-runner/app
codex exec resume <recorded-session-id> --json \
  --dangerously-bypass-approvals-and-sandbox --output-schema <fixed-schema> -
```

`--dangerously-bypass-approvals-and-sandbox` 只允许出现在已接受整机损失风险的第一阶段专用
Runner；Docker 阶段继续在容器内使用，但由容器提供外部边界。禁止 `--last`、`--ephemeral`
和自动创建替代 session。JSONL 解析错误、缺少 `thread_id`、返回
不同 session、超时或 SSH 中断都进入对账状态，不能盲目重放 Prompt。

每个 Turn 在启动前先使用该次实际执行的同一个 `CODEX_HOME` 执行固定的
`codex login status`：Docker 模式必须是精确 WorkItem home，不能是 Runner seed；direct 模式
必须是该执行路径的固定 home。状态检查和 `codex exec` 都通过固定 CLI 配置覆盖强制
`forced_login_method="chatgpt"` 以及
`cli_auth_credentials_store="file"`，不依赖 WorkItem 或可变环境。当前固定 CLI 版本还必须返回
精确的 `Logged in using ChatGPT`；API key、未登录、状态输出漂移、超时或命令失败都返回
`codex_auth_invalid`，不启动 `codex exec`，Turn/WorkItem 进入 blocked，且禁止盲目重试。检查器
不读取或复制 `auth.json`，也不声称 `login status` 会发起模型请求；实际 START/RESUME 才是
最终 provider-side 校验。Dispatcher 不实现 OAuth refresh，也不通过人为修改凭证来制造过期；
操作员修复方式是对目标 WorkItem 执行受控的重新登录后再显式重试。认证与配置项语义以
[OpenAI Codex authentication](https://developers.openai.com/codex/auth) 和
[OpenAI Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
为准。

### 6.3 Turn 结果 Schema

最后结果至少包含：

```json
{
  "status": "completed | needs_input | blocked",
  "summary": "bounded text",
  "needs_input": ["bounded question"],
  "tests": [{"name": "...", "status": "passed | failed | not_run"}],
  "changed_paths": ["repo/relative/path"],
  "next_step": "bounded text"
}
```

该结果是 Agent 声明，不是 Control Host 已验证事实。Publisher 的机械校验和 GitHub CI 是独立
证据。

`needs_input` 仅在 `status=needs_input` 时非空；`completed` 和 `blocked` 时必须为空。固定 Prompt
和 Schema description 同时声明该关系，本地解析器仍独立强制校验。Schema 合法但违反该领域
关系的输出不自动改写，Turn 以 `agent_result_invalid` 阻塞。Runner/JSONL 失败只记录固定、
有界的机器错误码，不保存或转发原始 stdout/stderr。

## 7. Publisher 协议

Publisher 与 Dispatcher 位于同一 Control Host。推荐不同 Unix 用户和本地 Unix socket；
第一版也可以是严格隔离的模块，但接口不得扩展为任意 Git wrapper。

调用方只提供：

```json
{
  "work_item_id": "stable id",
  "expected_head_sha": "full lowercase object id"
}
```

Publisher 从可信 SQLite/config 中解析 repository、branch、base、quarantine 路径和 remote。
固定流程：

1. 将 Runner bundle 放入新建的 quarantine 目录。
2. `git bundle verify` 和 `git fsck`。
3. 验证 expected head 存在且从记录的 base/last published SHA 可达。
4. 验证仅包含允许的 commit 和路径，未命中硬 Denylist 和凭据检测。
5. 禁用 hooks、submodule、自定义 protocol、filter、textconv、proxy 和任务提供的 Git config。
6. 首次发布验证远端任务分支不存在；后续发布验证它仍等于记录的 `last_published_sha`。
7. 使用完整 SHA 推送到唯一记录的任务 ref；禁止 force、delete、tag 和其他 ref。
8. 读回远端 ref，只有完全相等才记录 `last_published_sha`。

Publisher 不运行测试、不修改文件、不 stage、不 commit、不做语义代码审核，也不 merge。

GitHub 凭据优先使用只安装到 allowlist 仓库的 GitHub App。App 私钥只在 Control Host，按操作
生成短期 token：Dispatcher 使用 Issue/PR write + Contents read；Publisher 才使用 Contents
write。Runner 不接收任何 GitHub write token。

## 8. SlackReporter

SlackReporter 只调用出站消息 API：

- 第一次创建 thread root，保存 channel ID、thread timestamp 和 permalink；
- 后续 Turn 向同一 thread 追加状态；
- Issue 中写入 thread permalink；
- 超时重试不能创建第二个 thread；
- 发送前统一脱敏和长度限制；
- 不发送 Prompt、推理、完整 stdout/stderr、完整 diff 或凭据。

不实现 Slack Events API、Socket Mode、slash command、interaction endpoint 或消息读取权限。
Slack 中人工消息对系统行为没有任何影响。

发送前只持久化 deduplication key、payload SHA-256 和目标 thread，不持久化消息正文。root 成功
回执与 WorkItem thread binding 在同一 SQLite 事务完成；Turn 消息按 Turn/kind 使用独立固定 key。
提供方端口必须保证相同 key 和 payload 的重试返回原消息回执。Slack 当前公开
`chat.postMessage` 参数表没有给出可直接依赖的幂等键合同，因此真实 HTTP adapter 在 live
fixture 证明前保持禁用，也不通过增加 message-history 权限来找回消息。

## 9. 确定性调度流程

每轮处理顺序固定：

1. 获取全局进程锁。
2. 检查 SQLite、磁盘、配置、工具版本和唯一活动 Turn。
3. 对账上次 `starting/running/checkpointing` Turn；外部状态不明确则停止。
4. 对账 Publisher push、Draft PR、Issue label 和 Slack 输出。
5. 如果没有活动 Turn，按优先级、创建时间和 Issue number 选择一个 `agent:ready` Issue。
6. 对从未持久化的新 Issue，先从可信 mirror 取得当前 base 并生成 source bundle；失败时不得
   claim。既有 Issue 只使用持久化的 base 锚点。
7. 通过维护者审批和当前标签的重新读取 claim 至 `agent:dispatching`；一轮最多 claim 一个。
8. 新 Issue 创建 WorkItem 并 PREPARE；既有 Issue 恢复原 WorkItem、branch、directory 和
   session。
9. 先读取允许评论，再重新读取 Issue；只有 Issue identity、状态和 revision 稳定时才冻结
   comment IDs、Prompt hash、input HEAD 和下一个 turn number。漂移时不启动 Turn，下一轮重试。
10. 创建或找回唯一 Slack root 并将 permalink 写入固定 Issue 状态评论；回执不明确时尚未
    启动 Codex。
11. 通过 SSH `start` 或 `resume` 原 session；SSH 回执不明确时只允许 `status` 对账，不重放
    Prompt。
12. 解析、脱敏并保存结构化结果。
13. 若有安全且一致的 checkpoint，拉取 bundle 并调用 Publisher。
14. 对 protocol v2 的 `completed` 候选导入 exact-HEAD Actions、结构化 AC 和 publication
    ledger；pending 时保持运行并只轮询证据，失败/不明确时 blocked。
15. Implementation gate 通过且启用 final audit 时，创建 fresh Audit generation/Handoff；Audit
    可作范围内修复，但完成后必须再次通过同一 gate。
16. 创建/更新唯一 Draft PR、Issue 和 Slack Turn report，并根据可信终态进入
    `needs_input`、`review` 或 `blocked`。
17. 后续人工 merge 后，只在 PR identity 与 persisted exact head 完全一致时先落本地
    `completed` tombstone，再更新固定 Issue comment 和 `agent:completed`；不自动 merge/close/delete。

同一个 Issue 即使多次从 `needs_input/review` 回到 `ready`，步骤 6 也只能解析到原 WorkItem。

## 10. 实施阶段

### Phase A：文档与离线核心

不需要 Linux、网络、GitHub/Slack/Codex 凭据：

- WorkItem/Turn 领域对象、状态机和确定性 ID；
- 稳定 branch 和 runner directory 计算；
- additive SQLite migration 和约束；
- Runner/Publisher 请求与结果 Schema；
- Codex JSONL session 事件解析；
- Publisher 纯校验计划，不执行 push；
- Slack 出站消息模型和去重键；
- 全部 Fake 和故障路径单测。

状态：离线实现完成，包括恢复优先的单次 Control Host sweep；该 sweep 使用注入端口和 fake
覆盖 GitHub claim/state、source snapshot 与 SSH Runner。双重显式启用的 `ssh-run-once` 入口和
严格、无 secret 的 runtime 配置已经实现，并已对 Fixture Issue `#2` 完成一次受控 live
happy-path；具体外部写入证据归入 Phase D。

### Phase B：本地 Git bundle/quarantine

仍不访问 GitHub：

- 临时 bare repo 和独立 task repo；
- base bundle、result bundle、verify/fsck；
- commit ancestry、fast-forward、path policy；
- hooks/config/protocol 防护；
- 崩溃恢复和重复 import 幂等。

离线实现要求 Runner 输出 self-contained bundle。Verifier 将其写入一次性 quarantine bare
repository，使用固定 Git 配置执行 `bundle verify`、`fsck`、anchor ancestry、commit/path/mode、
文本、大小和凭据检查；不执行 checkout、hook、filter、textconv、submodule 或仓库代码。

状态：source/result bundle、Runner workspace 和 Control Host quarantine 的本地 Git fixture 已
完成；固定 GitHub URL/base ref 的可信 mirror refresher 已通过 mocked command boundary 验证，
并已在 Fixture Issue `#2` 和 `#4` 的受控 sweep 中执行 live GitHub fetch。Issue `#4` 首次 fetch
遇到一次写入前的暂时性失败；状态未变化，随后同一受限只读 ref 查询和安全重试成功。针对该
观测，refresher 现在只对固定 `base_fetch` 增加一次自动重试，两次尝试共享原 120 秒总预算；其他
Git 阶段、持久失败和预算耗尽仍 fail closed，且不会 claim Issue 或创建 SQLite/Runner 状态。

### Phase C：Linux SSH Runner Fixture

- 两台 Linux：Control Host 和专用 Runner；
- 固定 OpenSSH、Git、Codex CLI 版本；
- forced-command Runner wrapper；
- 首次 `codex exec --json` 和 session ID 捕获；
- Runner 重启后从相同目录、runner-wide CODEX_HOME 和精确 session ID resume；
- 第一阶段不增加 Docker 或 per-task OS 隔离。

状态：forced-command 服务、严格配置、OpenSSH adapter、Codex argv/JSONL parser 和 fake Codex
集成已经离线实现；真实 SSH、ChatGPT 登录、首次 session 与同一 session resume 已在 `s3`
Fixture 通过。该结果尚不代表 GitHub 调度、Publisher 或 Slack 端到端接线完成。

### Phase D：Publisher + GitHub Fixture

- GitHub App/PAT 权限合同；
- Publisher 仅推任务分支；
- Draft PR 和 Issue 状态；
- 主分支保护可用性核验；
- 故障注入：push 成功但 SQLite 未更新、PR 成功但评论未更新。

状态：Publisher 的 bundle import、精确 SHA、精确 `--force-with-lease`、远端 read-back、push
回执丢失恢复和竞态拒绝已在本地 bare remote 完成，并已接入 recovery-first sweep。Turn 会在
启动前持久化 Issue 路径策略；push 已记账但 Turn 未终态化的崩溃窗口也可在不重新 export/push
的情况下恢复。唯一 Draft PR 已按稳定 task branch 查找、创建、读回并绑定 SQLite；Issue 固定
状态评论成功后才允许写终态 label。PR 创建回执或评论回执丢失时，下一轮不会重启 Runner、
重复 push 或创建第二个 PR。人工合并后，recovery planner 还会验证 exact PR number、repository、
base/task branch 和 `headRefOid=last_published_sha`，先提交不可逆的本地 completed tombstone，再
幂等更新固定 Issue comment 和 `agent:completed`；它不执行 merge、close Issue 或删 branch。

2026-08-18 的受控 live happy-path 已验证真实 GitHub 凭据合同和上述正常写入路径：Fixture
Issue `#2` 绑定到一个 WorkItem、确定性分支和 Runner 目录；一个 Codex Turn 产生 checkpoint
`071ec769c63b8ab594865611cdc8af46ddd07b7f`，Publisher 只更新该任务分支，创建唯一 Draft PR
`#3`，固定评论和 `agent:review` 状态完成，Fixture Actions run `32155421239` 成功，`main` 仍为
原 SHA。随后第二次 write-enabled sweep 返回 idle；SQLite 的 WorkItem/Turn 行、Issue、唯一 PR、
任务分支、`main` 及 Actions run 均保持不变，之后只读 preflight 仍返回 idle。该正常路径本身未
注入 push/PR/comment 回执丢失；这些恢复性质不能从正常路径和空闲重扫外推。
截至 2026-08-19，另有一个不接入正式 CLI 的 source-tree Fixture 故障入口。它硬编码
上述私有 Fixture 及 README-only 合同，要求第三个仓库名级开关、精确 Issue/恢复阶段和自动
SQLite 在线备份，只能在真实远端操作成功后丢弃一次 Publisher、Draft PR 或固定 Issue comment
回执。正式 `ssh-run-once` 的组装和参数未改变。Fixture Issue `#4` 已按顺序完成三次真实写入后
丢弃回执：Publisher 重试读回并复用唯一任务分支，Draft PR `#5` 按分支找回且没有创建第二个
PR，固定评论重试后才将 Issue 从 `agent:running` 更新为 `agent:review`。SQLite 最终只有一个
WorkItem、一个 Turn 和同一个 Codex session；`main` 未移动，Actions run `32160041932` 在精确
checkpoint SHA 上成功。

Draft PR 回执阶段还暴露了 recovery planner 的顺序缺陷：本地 WorkItem 已为终态而远端 Issue
仍为 running 时，旧逻辑会先误判 orphan。测试在第三次写入前停止；修复和 255 项回归测试通过
后，live preflight 才继续并得到 `sync_tracker_state`。因此 AC-019、AC-048、AC-049 已有受控 live
证据。AC-047 也已由 Fixture Issue `#8` 覆盖：`publication-recorded` 在 exact SHA 落库后、Turn
仍为 `checkpointing` 时中断；后续 `recorded-publication-recovery` 使用 fail-before-delegate 的
Runner/Publisher guard，成功完成原 Turn、创建唯一 Draft PR `#9`，Actions run `32163520437`
成功且 `main` 未移动。这是确定性进程内异常注入，不等同于操作系统 kill。

Fixture Issue `#10` 随后完成真实 Dispatcher 子进程 `SIGKILL`：父进程只在精确 post-claim
private-pipe handshake 后终止其创建的同 argv 子进程。结果为 exit `-9`、没有本地 WorkItem/Turn、
Runner 未调用，preflight 精确选择 `recover_orphan_claim`。Git HTTPS 恢复后，普通
`ssh-run-once` 为同一 Issue 创建唯一 WorkItem `wi_56cfb4bd6efc095beabb0852`、Turn、session、
branch 和 Draft PR `#11`；checkpoint `a17ae709a111cd84d7a08050afa975351190fa73` 的 Actions run
`32168464039` 成功，重复 sweep idle，`main` 未移动。

Fixture Issue `#12` 还完成 START 回执歧义：第一阶段只在收到身份一致的成功 START reply 后
丢弃回执，留下唯一 `reconciling` Turn、本地 session/checkpoint/PR 均为空；第二阶段在 delegate
前拒绝 PREPARE/START/RESUME，并自证 Runner 操作严格为 `STATUS, EXPORT`。同一 WorkItem
`wi_594a1305a087ff78a0ab32f8` 和 Turn 完成，绑定 session、Draft PR `#13` 和 checkpoint
`41e67598b506dcbfeac00e5871a812e6e9874078`；Actions run `32169064603` 成功，重复 sweep idle。
随后维护者审核、将 PR 标记 ready 并显式 merge；普通 recovery-first sweep 验证 PR number、
repository、base/task branch 及 `headRefOid` 全部与已持久化绑定一致，先将唯一 WorkItem 落为
`completed`，再更新固定评论，最后写入 `agent:completed`。评论更新时间
`2026-08-19T01:47:34Z` 早于 completed label 事件 `01:47:37Z`；`main` 为 merge commit
`7fe0a9a5d51f4438423744ffb199563a0bcd4d9a`，任务分支仍保留在精确 checkpoint，Issue 保持 open，
重复 write-enabled sweep 与之后的 preflight 均 idle。这证明协议回执恢复和 AC-055 正常 live
路径，但不等同于物理 SSH 链路/daemon 故障，也未注入 completion comment/label 回执丢失。
Slack provider root/result 回执丢失后来已由 Fixture Issue `#18` live 注入；completion
comment/label 回执丢失随后也在同一 Issue 上完成 live 注入。经显式授权的操作者以精确 head SHA
merge 绑定 PR `#19` 后，第一阶段先落本地 completed tombstone 再丢弃固定评论成功回执；第二阶段
幂等复用评论、回读 completed label 后才丢弃其回执。最终 preflight 和两次普通 sweep 均 idle。

Fixture Issue `#14` 随后完成真实 OpenSSH 客户端进程中断。故障入口只持有本次 primary SSH
子进程的不可变 argv/PID capability；在本地 WorkItem/Turn 已落库后，第二条无故障钩子的只读
STATUS 连接第 2 次观察到 Runner durable executing 签名
`unknown + turn_outcome_unresolved`。只有再次验证 primary `PID=PGID=SID=80465` 且仍存活后，
入口才对该进程组发送 `SIGKILL`。SQLite 保留同一 `reconciling` Turn，随后受限恢复拒绝
PREPARE/START/RESUME 并严格执行 `STATUS, EXPORT`。同一 WorkItem
`wi_b7edba3be957aa3d4a851c56`、Turn `turn_7e8f8db322764d579c51595e4b2725ab`、branch、Runner
directory 与 session 完成发布；唯一 Draft PR `#15` 的 checkpoint
`fb2fb166a74984298a56811f3e3e52c4676df82c` 对应 Actions run `32213983342` 成功，`main` 保持
`7fe0a9a5d51f4438423744ffb199563a0bcd4d9a`。最终 SQLite/Runner/GitHub/Actions 独立读回一致，
两次普通 write-enabled sweep 和最终 preflight 均 idle。该测试没有修改 sshd、防火墙、路由或
其他连接，也没有按名称查找或批量终止进程。随后经显式人工审核、标记 ready 并 merge PR
`#15`，只读 preflight 精确选择 `complete_merged_work_item`；普通 sweep 先落本地 completed
tombstone，再于 `06:32:21Z` 更新固定评论，最后于 `06:32:24Z` 写入 `agent:completed`。Issue
保持 open、任务分支保留，`main` 前进到 merge commit
`790c3e0b361f727863e3e6d86ee6e2dce16b4faf`，重复 sweep 与最终 preflight 均 idle。

同日 Fixture Issue `#6` 完成了真实 GitHub 生命周期的两次 Turn：首次因故意缺少精确值进入
`agent:needs_input`，没有 commit、task ref 或 PR；维护者添加唯一 `/codex-context` 并重新批准
`agent:ready` 后，第二次 sweep 使用原 WorkItem、branch、Runner directory 和 Codex session
resume，创建唯一 Draft PR `#7`。SQLite 最终只有一个 WorkItem、两个有序 Turn，Turn 2 只包含
该维护者 comment ID；Actions run `32162425453` 在精确 checkpoint 上成功，`main` 未移动。
期间发现 Fixture 初始化遗留的 `agent:needs-input` 与 canonical `agent:needs_input` 不一致；多状态
Issue 未被执行，错误 label 已删除并补建 `agent:completed`。这为 AC-002、AC-004 及步骤 5-6 的
非 Slack 身份部分提供了受控 live 证据。
当前另有只读 `ssh-preflight`：先校验固定 Git/gh/OpenSSH 版本，在原 SQLite 的临时迁移快照上
执行 recovery-first 规划，再通过 GitHub 只读接口选择至多一个 `exec:ssh-cli` Issue。它不创建
或迁移原数据库、不连接 Runner、不 fetch/push、不 claim、不写评论/label，也不创建 PR。输出
固定声明 `authorizes_apply=false`；它是不持有进程锁的瞬时快照，写入口仍须在锁内重新校验。

### Phase E：Slack 只读投影

- 只出站 token 和权限；
- thread 创建、复用、permalink 回写；
- 脱敏、长度和重试幂等；
- 证明 Slack 消息不能进入调度路径。

状态：消息/回执模型、payload-hash outbox、root 原子绑定、GitHub permalink 投影、Turn 终态
消息和两类回执丢失恢复已经离线实现并接入可注入 sweep。真实 Slack HTTP publisher 已用标准库
实现并接入可选 runtime：固定 `chat.postMessage`/`chat.getPermalink`、稳定 `client_msg_id`、禁止
redirect/markup/mention/unfurl/broadcast、限制响应大小和超时，token 只来自环境。正常入口仍要求
精确 live-proof 配置值及独立 Slack 写开关；live fixture 必须先证明相同 key/payload 的提供方
重试不会创建第二条消息。若该合同无法证明，则保持 fail-closed，不增加 message-history/search
权限绕过。2026-08-20 的受控 live fixture 已对 Workspace `T0BQ60N9WH4`、频道
`C0BR2D0MS8Y` 完成该证明：两次精确请求返回同一 receipt，维护者确认只存在一条可见消息。
随后正常 Fixture Issue `#16` 完成一个 WorkItem/Turn/session、一个 Slack root/result thread、一个
Draft PR 和精确 SHA Actions success；重复 preflight/sweep 均为 idle，补齐正常路径端到端验收。
Fixture Issue `#18` 随后 live 执行真实回执丢失入口：root fault 只从新 candidate 丢弃已验证
receipt，留下未启动 WorkItem 和 prepared root；terminal fault 只从该 root recovery 状态继续
同一 WorkItem，在一个完成 Turn/发布 SHA/绑定 PR 后丢弃 result receipt。普通 sweep 以相同
timestamp/permalink 恢复，最终仅有一个 WorkItem/Turn/session/branch/Draft PR/Actions run 和两个
delivered outbox record；Runner STATUS 为 finished，重复 preflight/sweep idle。

### Phase F：Docker 加固

- 每任务容器和持久目录；
- 资源、网络、capability、mount 和 secret 限制；
- 不挂载 Docker socket，不使用 privileged；
- 重做攻击面和恢复验收后才用于高价值仓库。

## 11. Linux 部署基准

### 11.1 主机规格

| 角色 | Fixture 最低 | 推荐长期使用 |
|---|---:|---:|
| Control Host | 2 vCPU / 4 GiB / 50 GiB SSD | 4 vCPU / 8 GiB / 100 GiB |
| Runner Host | 4 vCPU / 8 GiB / 100 GiB SSD | 8 vCPU / 16 GiB / 500 GiB NVMe |

无须 GPU。全局单 Turn 使 CPU/RAM 不随开放 Issue 数量增长；磁盘按 repo、依赖缓存和 session
数量增长。Runner 可用空间低于 15% 时不得启动新 Turn。

### 11.2 Control Host 目录

```text
/opt/codex-dispatcher/releases/<version>/
/opt/codex-dispatcher/current
/opt/codex-dispatcher/current/scripts/codex-dispatcher-v1
/opt/codex-dispatcher/current/scripts/codex-dispatcher-backup-v1
/etc/codex-dispatcher/config.toml
/etc/codex-dispatcher/dispatcher.env
/etc/systemd/system/codex-dispatcher.service
/etc/systemd/system/codex-dispatcher.timer
/etc/systemd/system/codex-dispatcher-backup.service
/etc/systemd/system/codex-dispatcher-backup.timer
/var/lib/codex-dispatcher/state.db
/var/lib/codex-dispatcher/repos/
/var/lib/codex-dispatcher/quarantine/
/var/lib/codex-dispatcher/backups/
/run/codex-dispatcher/dispatcher.lock
```

SQLite 使用 Online Backup API；WAL 模式下禁止仅复制主 DB 文件。GitHub、Slack 和 SSH 凭据
不写 TOML、仓库、Issue、Prompt 或日志。

Control Host 使用固定无参数 wrapper 启动 `ssh-run-once --apply`。systemd service 为
`Type=oneshot`；timer 在上一次 sweep 进入 inactive 后再等待 120 秒，不制造定时器
积压或有意并发。`dispatcher.env` 必须 root 拥有、权限 `0600`，token 不得出现在
unit、ExecStart argv、TOML 或 release 中。service 使用稳定低权限用户、`UMask=0077`、
空 capability set 和只读系统目录，仅对 `/var/lib/codex-dispatcher` 与
`/run/codex-dispatcher` 保留写权。正式启用前必须在目标 Linux 上执行
`systemd-analyze verify` 和只读 `ssh-preflight`。

独立 backup oneshot 不读取 EnvironmentFile，禁用网络，通过 SQLite Online Backup API 在
Dispatcher 运行时也可创建一致快照。它先检查源库，再检查完整备份，最后以
`0600` 原子公布到受保护的 `backups/`；同名文件不覆盖。daily timer 允许主机
停机后补跑。公布成功后先验证全部规范备份，再固定保留最老迁移锚点和最新七份，
只删除其余已验证副本；任一副本异常时不删除。独立 weekly restore-drill 在临时库验证
最新备份的 integrity、外键和 migration ledger，随后删除临时库，绝不覆盖在线库。

### 11.3 Runner 目录

```text
/srv/codex-runner/releases/<commit>/src/
/srv/codex-runner/current -> releases/<commit>
/srv/codex-runner/tools/codex/<version>/
/srv/codex-runner/bin/codex-runner-v1
/srv/codex-runner/etc/config.json
/srv/codex-runner/etc/agent-result.schema.json
/srv/codex-runner/etc/agent-result-audit.schema.json
/srv/codex-runner/app/                # protected shared CODEX_HOME
/srv/codex-runner/work-items/
/srv/codex-runner/run/active.lock
/var/lib/codex-runner/home/
/etc/ssh/authorized_keys/codex-runner
/etc/ssh/sshd_config.d/60-codex-runner.conf
```

Runner 的 SSH host key 固定在 Control Host。禁止 `StrictHostKeyChecking=no`、agent forwarding、
port forwarding 和 X11 forwarding。

Fixture Runner 已迁移到锁定、无 sudo、无附加组的 `codex-runner` 协议账户。release、versioned
Codex tools、wrapper、Schema、配置和 SSH 授权由 root 管理。Codex 不再直接在 host 上执行：
每个 WorkItem 使用 rootless Docker 容器、独立 ext4 image、repository/session home/auth copy、
固定资源限制和 proxy-only egress；Docker socket、Runner-wide auth seed 和其他 WorkItem 均不挂载。
该边界已完成 Fixture live 验收，但仍不等同于高价值仓库准入；代码矩阵只放行显式 Fixture
class/profile，higher-value 行保持 hard false。剩余 attack/recovery gate 未独立验收并通过新
release 修改矩阵前，高价值仓库继续 fail closed，单独改 TOML 不能放行。

### 11.4 资源和保留

- 当前受审配置为每个 WorkItem 8 GiB ext4 image，并额外保留 16 GiB host reserve；大型项目须以
  独立、受审的配置和容量验收显式提高，不能在 Issue 中请求扩容。
- `completed_retention_seconds` 显式配置后，completed WorkItem 默认保留 7 天再归档；缺省不启用
  自动清理。
- maintainer 的 `agent:discard` timeline event 会形成不可逆 disposition：无 PR 为 `abandoned`，
  精确且未 merge 的 PR 为 `superseded`；记录 disposition 与终结 generation 必须是同一事务。
- 每个 WorkItem 的 `repo/`、`runner-state/`、generation home 和独立 auth copy 同生共灭；Runner
  registry、永久 archive tombstone、共享 auth seed/policy/tools 及 Control SQLite 不随单个
  WorkItem 清理。
- 不自动删除 `blocked` 或 `needs_input` WorkItem；只有上述审计过的 disposition 才能使其可归档。
- 混合 legacy directory/image 由 Runner storage classifier fail-closed；普通 ARCHIVE 对全缺失状态
  仍保持 blocked。显式 `ssh-reconcile-absence --apply` 必须在 Control/Runner 双重全局锁下，通过
  protocol-v2 `PROVE_ABSENCE` 核验 registry、workspace、archive、staging、image 和 mount 全部
  缺失，并将 request-bound Runner receipt SHA 写入 schema-13 ledger；不接受本地 JSON 断言，
  也不伪造 Runner archive receipt。
- 用 `runner-capacity --json` 分别观察 Turn admission 与新 image provision admission/shortfall。
- Control Host 每日 SQLite online backup，并保留 GitHub/Slack 映射。

## 12. 可测试验收标准

### 12.1 离线门槛

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests
git diff --check
```

测试不得依赖网络、真实 GitHub/Slack/OpenAI 凭据或 Linux-only 功能。外部命令使用临时 fake
可执行文件，Git 测试只使用临时本地仓库。

### 12.2 验收矩阵

| ID | 场景 | 可观察通过标准 |
|---|---|---|
| AC-001 | 首次发现 Issue | 只创建一个 WorkItem，branch/directory 稳定 |
| AC-002 | 同 Issue 再次 ready | 复用原 WorkItem、branch、directory，不创建第二个 |
| AC-003 | 首次 Codex Turn | 只接受一个 `thread.started`，绑定 session ID |
| AC-004 | 后续补充 | 使用原 session ID 和 `resume`，turn number +1 |
| AC-005 | session ID 冲突 | WorkItem blocked，不自动新建 session |
| AC-006 | 已 completed Issue 再次 ready | 拒绝执行，要求新 Issue |
| AC-007 | 全局单线程 | 第二个活动 Turn 被 SQLite 和进程锁拒绝 |
| AC-008 | Prompt 确定性 | 相同输入得到相同 bytes 和 SHA-256 |
| AC-009 | Issue 运行中漂移 | 当前结果不发布；仍保留原 session/branch |
| AC-010 | Slack 人工消息 | 系统没有入站处理接口，Codex 调用数为 0 |
| AC-011 | Slack thread 幂等 | 重试复用同一个 thread/permalink |
| AC-012 | Runner 无 GitHub 写权限 | Codex 环境中无 GitHub write token/SSH key |
| AC-013 | Publisher 参数注入 | 任意 URL/ref/path/option 字段在解析阶段拒绝 |
| AC-014 | Publisher 主分支写入 | 即使请求伪造也只能解析到记录的任务 branch |
| AC-015 | 非 fast-forward | 不 push，WorkItem blocked |
| AC-016 | 修改路径越界 | 不 push、不建 PR，记录脱敏原因 |
| AC-017 | Git bundle 损坏 | verify/fsck 失败；不污染主 mirror |
| AC-018 | 恶意 Git hook/config | hook 不执行，proxy/filter/protocol 不生效 |
| AC-019 | push 后崩溃 | 读回远端 ref 后幂等补记，不重复 commit |
| AC-020 | PR 创建后崩溃 | 按 head branch 找回原 Draft PR，不创建第二个 |
| AC-021 | SSH 中断 | Turn 进入 interrupted/reconcile，不盲目重放 Prompt |
| AC-022 | Runner 重启 | 同目录、共享 CODEX_HOME、精确 session ID 可以 resume |
| AC-030 | Mac Fixture assh 代理 | `assh` 路径与 home 必须成对显式配置，只生成固定 `assh connect --port=%p %h`；默认无代理且不能注入任意命令 |
| AC-031 | ChatGPT 登录门槛 | 每个 Turn 前固定状态检查；API key、未登录或输出漂移均不启动 `codex exec` |
| AC-023 | Runner 磁盘丢失 | 已发布 commit/PR/Issue/SQLite 不受影响 |
| AC-024 | 输出包含凭据 | 日志、GitHub、Slack 仅出现脱敏值 |
| AC-025 | Docker 未部署 | 诊断明确报告 first-phase risk accepted，不虚报隔离 |
| AC-026 | PREPARE source bundle | 只接受 size/SHA 匹配且仅公布精确 base SHA 的自包含 bundle |
| AC-027 | 相同 Turn 重试 | 返回同一持久化结果，Codex 调用数和 commit 数不增加 |
| AC-028 | Turn 请求冲突 | 相同 turn ID 的不同 Prompt/session/anchor 被拒绝，不执行 Codex |
| AC-029 | Runner 输入残留 executing | STATUS 返回 unknown；Dispatcher 保持 reconcile 且不重放 Prompt |
| AC-032 | 新 Issue source 失败 | claim 调用数为 0，SQLite 和 Runner 均无新 WorkItem |
| AC-033 | claim 竞争失败 | 不创建 WorkItem、不调用 Runner，下一轮可重新选择 |
| AC-034 | Issue/评论快照漂移 | 不创建 Turn、不发送 Prompt，保留 PREPARE 后的可恢复 WorkItem |
| AC-035 | PREPARE 回执丢失 | 使用持久化 base SHA 重建 exact bundle 并幂等重试 PREPARE |
| AC-036 | START 回执丢失 | 下一轮只发送 STATUS；START/RESUME 调用数不增加 |
| AC-037 | 终态 label 回写丢失 | 新 claim 前将现有 dispatching/running Issue 恢复为 SQLite 终态 |
| AC-038 | checkpoint 等待 Publisher | Turn 保持 checkpointing，重复 sweep 不再启动 Codex |
| AC-039 | mirror 远端/引用注入 | URL 只能从配置 slug 派生，只 fetch 配置 base 到固定内部 ref |
| AC-040 | mirror 凭据隔离 | token 不进入 argv、持久 Git config、异常或命令输出；只注入受控 Git 子进程环境 |
| AC-041 | PREPARE 恢复时主干已前进 | 不 fetch 当前 base，exact bundle 仍使用持久化旧 base SHA |
| AC-042 | SSH live 入口误触 | 缺少 `--apply` 或 `CODEX_DISPATCHER_ENABLE_SSH_WRITES=1` 时不加载配置、不写 SQLite、不访问外部服务 |
| AC-043 | SSH runtime 配置含 secret | 严格字段解析直接拒绝 token/key 内容字段；凭据只能来自显式环境注入 |
| AC-044 | Control Host 工具版本漂移 | Git/gh/OpenSSH 精确版本检查失败时不进入 sweep，不 claim Issue |
| AC-045 | runtime TOML/SQLite 路径替换 | 配置父目录及单链接普通文件、DB 目录及已有 DB/WAL/SHM 必须同用户拥有、非 symlink 且不可被 group/world 写 |
| AC-046 | Issue 在 checkpoint 后扩大允许路径 | Publisher 只使用 Turn 启动前持久化的路径策略，拒绝调用方覆盖或重新解析新正文 |
| AC-047 | push 记账后、Turn 终态前崩溃 | 依据已落库的 exact SHA 完成 Turn，不重新 EXPORT、不重复 push、不重启 Codex |
| AC-048 | Draft PR 创建回执丢失 | 按持久化 task branch 找回并绑定同一 PR，不重启 Codex、不重复 push、不创建第二个 PR |
| AC-049 | Issue 状态评论回执丢失 | 保持 Issue 在 dispatching/running；按固定 marker 幂等补写后才更新终态 label |
| AC-050 | Slack root 回执丢失 | Codex 尚未 START；以同一 key/payload 找回同一 root 并原子绑定，不创建第二个 thread |
| AC-051 | Slack 终态回执丢失 | commit/PR 保持不变；只重试同一 Turn report，不重启 Codex、不重复 push/PR，成功后才写终态 label |
| AC-052 | SSH live 只读预检 | 固定工具版本通过后，在临时 SQLite 快照上先报告恢复动作，否则只选择一个 `exec:ssh-cli` 候选；原 DB、Runner、Git refs、Issue 和 PR 均不改变，歧义状态非零退出 |
| AC-053 | Fixture 故障入口越界或误触 | 正式 CLI 不暴露该入口；缺少任一开关、仓库/README 策略/Issue/恢复阶段不精确时在目标写入前拒绝；每次接受前生成并校验私有 SQLite 在线备份 |
| AC-054 | claim 后 Dispatcher 被 SIGKILL | 只终止精确握手子进程；无本地 WorkItem/Turn、Runner 未调用；preflight 为 `recover_orphan_claim`，普通路径恢复同一 Issue 且只创建一套 1:1:1 身份 |
| AC-055 | 人工合并后的 completed 投影 | 仅 exact bound PR 在 persisted head SHA 合并后先落本地 completed，再写固定 comment 和 label；丢回执只重试投影，不调用 Runner/Publisher，不创建 PR；任何身份/head/merge 状态冲突均 blocked |
| AC-056 | durable START 后真实 SSH 客户端中断 | 仅在本地 WorkItem/Turn 已持久化且第二条只读 STATUS 证明 Runner durable executing/finished 后，复核 primary SSH 的 exact argv/PID/PGID/SID 并只终止该进程组；同一 Turn 留在 reconciling，恢复只走 STATUS/EXPORT，不重发 Prompt，并最终只产生一套身份和一个 PR |
| AC-057 | Linux Control Host systemd 服务化 | 固定无参数 wrapper 只执行一次 write-enabled recovery-first sweep；oneshot/timer 不重叠，token 只由 root-only EnvironmentFile 注入，unit 无 fixture 入口且只写受保护的 state/runtime 目录 |
| AC-058 | Control Host SQLite 定时备份 | 无 token/无网络 oneshot 使用 Online Backup API，源库和备份均 integrity=ok 后原子发布 `0600` 文件；碰撞不覆盖，失败清理暂存，不自动删除旧备份 |
| AC-059 | generation 安全轮换 | 只在无活动 Turn 且 WorkItem/旧代 published anchor 一致时，同一事务 retire 旧代、plan 新代并写入唯一 Handoff |
| AC-060 | Handoff 可信边界 | verified publication facts 与 `untrusted_advisory` 分离；Agent summary/tests/paths/next step 不得成为 CI 或验收事实 |
| AC-061 | replacement Bootstrap | 新代首个 Full START 必须绑定同一 durable Handoff，并先校验 AGENTS/HEAD/commits/code/tests/Issue；同代 Delta 不重放 Handoff |
| AC-062 | Handoff 崩溃恢复 | rotation 后、START 前中断时恢复同一 planned generation/Handoff；START 回执歧义仍只用 STATUS，不生成第二次 START |
| AC-063 | Actions/CI exact-HEAD 导入 | rotation 前后两次读取 task ref 保持精确 HEAD；只接受唯一同仓库、同分支、同 HEAD、`pull_request` 事件和配置 workflow 名的 run；权限失败、ref 漂移、重复同名或畸形响应均拒绝 rotation |
| AC-064 | 结构化 AC 判定 | 只判定配置内 required check、完整 publication ledger 的路径集合和 durable published HEAD；普通文本及证据不完整保持 `unverified`，AgentResult 永不升级为验收证据；Handoff v1 保持可读 |
| AC-065 | PREPARE/START 明确拒绝后人工重试 | 没有 `preparing→ready` ACK provenance 的 blocked/paused WorkItem 在 claim 前读取持久 base 的 exact bundle，原子回到 preparing，幂等 PREPARE 成功后才允许 START；exact source 失败不 claim，已有 ACK 的 Turn-blocked WorkItem 不重复 PREPARE；START 在 session 创建前明确拒绝时，只在 planner 与 StateStore 双重证明无 session/output/checkpoint/handoff 且所有前代均为 exact rejection 后，才允许新 generation 无 Handoff 重试 |
| AC-066 | Sol 自主 agent 路由可信证据 | 调用方只提交任务，Runner 固定 primary Sol 并由其选择 direct-child profile；每个新完成 v2 Turn 以隔离 `state_5.sqlite` 的 edge/token 增量生成 metadata-only receipt，逐项匹配固定 Codex 版本、role/model/reasoning policy 并与 Turn 原子落库；未知 role、策略漂移、删边/计数回退、间接委派或 schema 异常均 fail closed，旧无 receipt 回执仍可恢复读取 |
| AC-067 | exact-HEAD 完成门 | v2 `completed` 只成为 `published` 候选；配置内全部 Actions checks、结构化 AC 和完整 publication ledger 绑定同一远端 HEAD 后才原子进入 finished/review；pending 不重跑 Codex/Publisher，失败或身份/权限歧义 blocked |
| AC-068 | fresh Final Audit | Implementation gate 通过后使用新 `audit` generation、独立 session、`completion_candidate` Handoff 和明确 Audit prompt；Audit 可提交范围内修复但必须再次通过完成门，崩溃恢复不重复 rotation，generation 预算不足时 blocked |
| AC-069 | context failure 安全换代 | 只识别有限的 context/compaction 错误；Runner 必须证明 worktree clean 且 HEAD 等于 Turn input，才原子记录 receipt、interrupt Turn 并通过 `context_failure` Handoff 新建 generation，旧 session 不 resume；dirty、moved HEAD 或状态不明均 blocked |
| AC-070 | completed WorkItem 生命周期和磁盘回收 | 仅显式保留期届满且本地/Issue completed、exact bound PR 在 persisted SHA 合并的单项进入 schema-12 ledger；Runner 在全局锁内证明 clean exact HEAD、全部 Turn finished、v2 容器 inactive 后先写 metadata-bound 永久 tombstone，再原子 staging 并只回收该 WorkItem；SSH 丢回执先 `ARCHIVE_STATUS`，不得盲重放；blocked/needs_input/review/active、registry/tombstone、共享 Runner 状态和完整 Control/GitHub/Slack 证据均保留 |

### 12.3 Live Fixture 顺序

截至 2026-08-19，步骤 1-5、7-9、11 已通过。Fixture Issue `#6` 已证明步骤 6 的
WorkItem/branch/directory/session 复用和维护者 context 过滤；Slack 仍禁用，因此尚未证明同一
Slack thread。步骤 9 已通过第二次 write-enabled sweep 和独立读回验证。步骤 10 已完成
Publisher、Draft PR、Issue comment、Dispatcher `SIGKILL`、START 回执歧义恢复及精确
OpenSSH 客户端进程 `SIGKILL`；没有修改 SSH daemon 或网络设施。步骤 11 已由维护者显式 merge
Fixture PR `#13` 后的正常完成态投影和重复 idle sweep 验证；completion 写回丢失仍只有离线
故障覆盖。完整非敏感证据见 `docs/live-test-evidence.md`。

1. SSH 只读连接与 host key 固定。
2. 创建 Fixture WorkItem 目录和独立 repo。
3. 首次 `codex exec --json` 获得 session ID。
4. 运行 `ssh-preflight` 并保存非敏感 JSON；必须明确得到一个预期 Fixture 候选或已有恢复动作。
5. 在 Issue 添加维护者 `/codex-context` 并再次 ready。
6. 证明使用同一个 Issue、branch、directory、session 和 Slack thread。
7. Codex 创建本地 commit，Control Host 拉取 bundle。
8. Publisher 将精确 SHA 推到任务分支并创建唯一 Draft PR。
9. 重复所有对账命令，证明不新增 session、branch 或 PR。
10. 中断 SSH、Dispatcher 和 Publisher 各一次，验证 fail-closed 恢复。
11. 人工审核并合并后，Issue 进入 completed；后续变化必须新建 Issue。

## 13. 主要风险与回滚

| 风险 | 影响 | 控制 |
|---|---|---|
| 第一阶段 Runner 无隔离 | Runner 数据或系统损坏 | 专用可重建主机、无 GitHub 写/生产凭据、接受 fixture 风险 |
| Codex 凭据泄漏或滥用 | API 费用和账户风险 | 专用低额度凭据、出站限制列入 Docker 阶段、轮换和费用告警 |
| 私有源码外传 | 仓库机密性损失 | 只接入批准仓库、Runner 不接触生产 Secret、后续网络隔离 |
| Publisher 权限仓库级 | 未保护 ref 被修改 | token 不给 Codex、固定参数、精确 SHA、无 force/delete/tag、分支保护 |
| 恶意 bundle/Git 配置 | Control Host 命令执行或凭据泄漏 | quarantine、固定 Git 配置、禁 hook/filter/protocol、保持 Git 补丁更新 |
| session 丢失或上下文膨胀 | 上下文和未发布工作损失 | 每 Turn 发布 checkpoint；仅在安全点按预算换代；原子 Handoff + 新代 Bootstrap；活动 Turn 歧义仍 blocked、不盲目替换 |
| 共享 CODEX_HOME 被破坏 | 所有本地 session 和 ChatGPT 登录状态丢失 | 目录 0700、单 Turn、Runner 无高价值凭据、主机可重建；Docker 阶段重做认证隔离 |
| SQLite 损坏 | 映射和恢复锚点损失 | online backup、integrity check、GitHub/Runner 对账 |
| Slack 故障 | 详情不可见 | GitHub/SQLite 仍为事实来源，恢复后幂等补发 |

紧急停止：停止 Dispatcher systemd service/timer，撤销 GitHub App installation token，保留
SQLite、quarantine、Runner 目录、分支和 Draft PR。停止不会 merge、delete branch 或删除
Runner 数据。恢复前只做只读对账。

## 14. 当前不做

- Codex Cloud task 创建、续接或状态同步；
- Slack 入站控制；
- 多 Dispatcher、多活或并发 Turn；
- 自动 merge、release、deploy；
- Runner 第一阶段的 Docker/VM/per-task UID 隔离；
- 生产凭据、内网或生产自托管 CI；
- 让 Dispatcher 成为语义代码审核器。
