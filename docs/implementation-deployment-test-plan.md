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
├── etc/
├── run/
├── work-items/
└── auth.json / sessions / other Codex-managed state

/srv/codex-runner/work-items/<repository-key>/issue-<number>/
├── repo/
└── runner-state/
```

`CODEX_HOME=/srv/codex-runner` 在 Runner 范围共享，权限为 `0700`；Codex 登录状态只在该目录
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

### 3.3 后续 Docker 加固

Docker 阶段才增加：

- 每个活动 WorkItem 一个容器；
- 只挂载该 WorkItem 目录；
- 持久化 `repo/` 和 `runner-state/`；
- CPU、内存、PID、磁盘和执行时限；
- 丢弃 capabilities，禁止 `--privileged`；
- 不挂载 Docker socket、SSH agent、Control Host 或其他 WorkItem；
- 禁止访问内网和云 metadata，按需限制外网；
- 容器内可拥有完成开发所需权限，但这些权限不能扩展到 Runner Host。

共享认证状态如何安全提供给容器必须在 Docker 阶段单独设计；不能把整个 Runner 级
`CODEX_HOME` 无条件挂载给所有容器并宣称已经隔离。

Docker 不在当前离线逻辑和第一轮 SSH Fixture 的完成条件内。

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

blocked | paused | waiting_input | review
  → ready
```

`completed` 是终态。自动化不得把 completed WorkItem 重新打开；新工作使用新 Issue。

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
CODEX_HOME=/srv/codex-runner
codex exec --json --dangerously-bypass-approvals-and-sandbox \
  --output-schema <fixed-schema> -
```

捕获 `thread.started.thread_id`，只能在 `codex_session_id IS NULL` 时绑定。

后续 Turn：

```text
cwd=<work-item>/repo
CODEX_HOME=/srv/codex-runner
codex exec resume <recorded-session-id> --json \
  --dangerously-bypass-approvals-and-sandbox --output-schema <fixed-schema> -
```

`--dangerously-bypass-approvals-and-sandbox` 只允许出现在已接受整机损失风险的第一阶段专用
Runner；Docker 阶段继续在容器内使用，但由容器提供外部边界。禁止 `--last`、`--ephemeral`
和自动创建替代 session。JSONL 解析错误、缺少 `thread_id`、返回
不同 session、超时或 SSH 中断都进入对账状态，不能盲目重放 Prompt。

每个 Turn 在启动前先使用相同共享 `CODEX_HOME` 执行固定的 `codex login status`。状态检查和
`codex exec` 都通过固定 CLI 配置覆盖强制 `forced_login_method="chatgpt"` 以及
`cli_auth_credentials_store="file"`，不依赖 WorkItem 或可变环境。当前固定 CLI 版本还必须返回
精确的 `Logged in using ChatGPT`；API key、未登录、状态输出漂移、超时或命令失败都返回
`codex_auth_invalid`，不启动 `codex exec`。检查器不读取或复制 `auth.json`。配置项语义以
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
10. 通过 SSH `start` 或 `resume` 原 session；SSH 回执不明确时只允许 `status` 对账，不重放
    Prompt。
11. 解析、脱敏并保存结构化结果。
12. 若有安全且一致的 checkpoint，拉取 bundle 并调用 Publisher。
13. 创建/更新唯一 Draft PR、Issue 和 Slack thread。
14. 根据结构化结果进入 `needs_input`、`review` 或 `blocked`。

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
覆盖 GitHub claim/state、source snapshot 与 SSH Runner，尚未暴露无人值守入口或接真实外部
服务。

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
完成；未访问 GitHub。

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
回执丢失恢复和竞态拒绝已在本地 bare remote 完成；真实 GitHub 凭据合同、Draft PR 与 Issue
写入仍待 live fixture。

### Phase E：Slack 只读投影

- 只出站 token 和权限；
- thread 创建、复用、permalink 回写；
- 脱敏、长度和重试幂等；
- 证明 Slack 消息不能进入调度路径。

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
/etc/codex-dispatcher/config.toml
/var/lib/codex-dispatcher/state.db
/var/lib/codex-dispatcher/repos/
/var/lib/codex-dispatcher/quarantine/
/var/lib/codex-dispatcher/backups/
/run/codex-dispatcher/dispatcher.lock
```

SQLite 使用 Online Backup API；WAL 模式下禁止仅复制主 DB 文件。GitHub、Slack 和 SSH 凭据
不写 TOML、仓库、Issue、Prompt 或日志。

### 11.3 Runner 目录

```text
/srv/codex-runner/releases/<commit>/src/
/srv/codex-runner/current -> releases/<commit>
/srv/codex-runner/bin/codex-runner-v1
/srv/codex-runner/etc/config.json
/srv/codex-runner/etc/agent-result.schema.json
/srv/codex-runner/work-items/
/srv/codex-runner/run/active.lock
```

Runner 的 SSH host key 固定在 Control Host。禁止 `StrictHostKeyChecking=no`、agent forwarding、
port forwarding 和 X11 forwarding。

当前 `s3` Fixture 因 `ecs-user` 无免密 sudo，上述 release、wrapper 和配置暂由同一用户管理；
这不是进程隔离，也不能阻止不受限 Codex 进程破坏 Runner 本身。正式部署必须将 release、
wrapper 和 `etc` 改为 root-owned，Runner 用户只保留 `run`、`work-items` 与必要认证状态的写权限。

### 11.4 资源和保留

- 单个 WorkItem 默认磁盘预算 20 GiB；大型项目显式提高。
- completed WorkItem 默认保留 7 天后归档。
- 每个 WorkItem 的 `repo/`、`runner-state/` 和 manifest 同生共灭；共享 `CODEX_HOME` 不随单个
  WorkItem 清理。
- 不自动删除 `blocked` 或 `needs_input` WorkItem。
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

### 12.3 Live Fixture 顺序

截至 2026-08-18，步骤 1-3 已通过；步骤 5 的 WorkItem/branch/directory/session 复用已通过
直连协议验收。该次 Issue 仍保持 `agent:paused + exec:cloud`，因此不代表 SSH 调度标签接线已
完成。完整非敏感证据见 `docs/live-test-evidence.md`。

1. SSH 只读连接与 host key 固定。
2. 创建 Fixture WorkItem 目录和独立 repo。
3. 首次 `codex exec --json` 获得 session ID。
4. 在 Issue 添加维护者 `/codex-context` 并再次 ready。
5. 证明使用同一个 Issue、branch、directory、session 和 Slack thread。
6. Codex 创建本地 commit，Control Host 拉取 bundle。
7. Publisher 将精确 SHA 推到任务分支并创建唯一 Draft PR。
8. 重复所有对账命令，证明不新增 session、branch 或 PR。
9. 中断 SSH、Dispatcher 和 Publisher 各一次，验证 fail-closed 恢复。
10. 人工审核并合并后，Issue 进入 completed；后续变化必须新建 Issue。

## 13. 主要风险与回滚

| 风险 | 影响 | 控制 |
|---|---|---|
| 第一阶段 Runner 无隔离 | Runner 数据或系统损坏 | 专用可重建主机、无 GitHub 写/生产凭据、接受 fixture 风险 |
| Codex 凭据泄漏或滥用 | API 费用和账户风险 | 专用低额度凭据、出站限制列入 Docker 阶段、轮换和费用告警 |
| 私有源码外传 | 仓库机密性损失 | 只接入批准仓库、Runner 不接触生产 Secret、后续网络隔离 |
| Publisher 权限仓库级 | 未保护 ref 被修改 | token 不给 Codex、固定参数、精确 SHA、无 force/delete/tag、分支保护 |
| 恶意 bundle/Git 配置 | Control Host 命令执行或凭据泄漏 | quarantine、固定 Git 配置、禁 hook/filter/protocol、保持 Git 补丁更新 |
| session 丢失 | 上下文和未发布工作损失 | 同目录/共享 CODEX_HOME/精确 session ID、每 Turn checkpoint、丢失时 blocked 不替换 |
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
