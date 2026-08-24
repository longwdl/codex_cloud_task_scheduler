# Codex SSH CLI Dispatcher：当前实施、部署与验收

本文只描述当前受支持的 SSH/容器架构。历史演进、已退役接口和具体 live receipt 保留在
`docs/live-test-evidence.md`，不再作为实现要求。

## 1. 当前范围

系统把维护者审核过的 GitHub Issue 调度为 Runner 上的隔离 Codex CLI WorkItem，并在通过本地
安全检查后发布到唯一任务分支和 Draft PR。

当前明确边界：

- 一个 Issue 对应一个 WorkItem、任务分支、Runner 存储身份和 Slack thread；
- 一个 WorkItem 可以包含多个 Turn 和多个 SessionGeneration；
- primary Agent 固定为 Sol，由 Sol 在 Runner 下发的 policy 中自主选择合适的 direct-child agent；
- 全局只允许一个活动 Turn；
- 普通路径仅接受 `exec:ssh-cli`；
- 只允许受审 Fixture class/profile；普通 higher-value admission 保持 hard false；
- 不自动 merge、release、deploy 或修改生产基础设施；
- 不执行 Issue/comment/repository 中的 shell 命令。

## 2. 组件和责任

### 2.1 GitHub

GitHub 提供 Issue、维护者审批、任务分支、Draft PR、Actions evidence 和最终 merge 事实。Issue
正文、评论、标签、Timeline、PR、Actions 和 API 返回都按不可信输入处理。

可执行候选必须同时满足：

- Issue OPEN；
- 唯一状态标签 `agent:ready`；
- 唯一执行标签 `exec:ssh-cli`；
- Ready 事件来自配置内 maintainer；
- repository class、recovery profile、target-readback profile 和 policy digest 一致；
- TaskSpec、allowed paths、denied paths、required checks 和结构化 AC 合法；
- 没有未解决依赖、活动 Turn 或容量阻塞。

### 2.2 Control Host

Control Host 负责：

- recovery-first planner；
- GitHub/Slack 适配器和最小凭据；
- trusted mirror、source staging、quarantine、Publisher staging；
- SQLite 1–21 migration ledger；
- WorkItem/Turn/generation/Handoff/follow-up/completion/disposition/archive/branch-cleanup 状态；
- online backup、restore drill、full DR、health 和 release receipt；
- 一个全局 Dispatcher lock。

外部写入只能由固定 wrapper + systemd oneshot 发起。Issue 不能控制可执行路径、argv、环境变量、
SSH proxy、Runner 目录、发布 ref 或临时目录。

### 2.3 Runner Host

Runner 固定 SSH 入口只接受 protocol-v2 JSON 请求和独立 bundle stdin。每个 WorkItem 使用：

- 独立 registry 和 8 GiB dense ext4 image；
- 独立 repo、runner-state、generation home 和 auth copy；
- rootless Docker；
- 只读 root、`cap-drop=ALL`、`no-new-privileges`、CPU/memory/PID/tmpfs 限制；
- proxy-only egress；
- 固定只读 policy/tool/schema mount。

容器不挂载 Docker socket、Runner-wide auth seed、其他 WorkItem、Control 凭据或 GitHub write
credential。Runner 不 push、不创建 PR、不写 GitHub/Slack。

### 2.4 Publisher 和 Slack

Publisher 只处理经过 quarantine 和 trusted Git 验证的 exact bundle，只能更新 durable task
branch，并通过 ref read-back 创建或找回一个 Draft PR。禁止 base branch、tag、force push、merge
和任意 ref。

Slack 是出站状态投影，不是控制面。相同 idempotency key/payload 必须找回同一 receipt；歧义只重试
同一消息，不重跑 Codex 或 Publisher。`slack_runtime.issue_channel_id` 只承接 WorkItem root/result；
`slack_runtime.system_channel_id` 只承接 lifecycle-health 告警和恢复，包括数据库、systemd、GitHub
API、备份/恢复可见状态与 Runner capacity。两个 channel 必须不同；旧的单一 `channel_id` 不再接受。

## 3. 状态模型

### 3.1 WorkItem

主要状态：

```text
discovered -> preparing -> ready -> running -> review -> completed
                                  \-> waiting_input
                                  \-> blocked / paused
```

`checkpointing`/`published` 属于 Turn，不是 WorkItem 主状态。完整分层状态机见
[`state-machine.md`](state-machine.md)。

`agent:discard` 是受信维护者指令，不交给 AI 再解释。控制面先写 immutable discard request，冻结新
Turn/generation；若已有 Turn 则只通过 STATUS 收敛并拒绝未发布结果。随后精确关闭未合并 PR（如有）、
写入统一 discarded disposition、以 `not_planned` 关闭 Issue，最后才进入独立 archive eligibility。
内部 `abandoned`/`superseded` 只表示是否绑定过 PR，不再映射成额外 GitHub 状态。
`work_items.state` 只是一层主状态，事实还包括 Turns、generation、publication ledger、completion gate、
disposition、archive、absence 和 branch-cleanup receipt。

### 3.2 SessionGeneration 和 Turn

SessionGeneration 绑定 role、policy digest、baseline、Codex session、start/published HEAD、Handoff 和预算。
当前 role 为 `implementation`、`ci_repair`、`audit`。旧 generation 只能 retire/failed，不能重新成为 live。

Turn 冻结 Issue revision、TaskSpec、approved comments、allowed paths、prompt hash、input HEAD、generation 和
follow-up intent。START/RESUME 前必须通过固定 `codex login status`。START 回执不明确时只允许 STATUS，
禁止重发 Prompt。

### 3.3 Follow-up 和完成门

Agent `checkpoint`、CI failure 或 Audit gap 会先写入 schema-19 follow-up intent。Intent 绑定 source Turn、
HEAD、cause、target role/generation 和 evidence digest；下一 Turn 原子消费。

Agent `completed` 只是 candidate。进入 review 之前必须满足：

- exact remote task HEAD；
- 完整 publication ledger；
- 配置内 required Actions 全部绑定同一 HEAD 并成功；
- 结构化 AC 全部由 trusted evidence 支持；
- configured fresh Audit 完成且没有修改 trusted Git。

## 4. 正常调度流程

每次 sweep 按以下顺序执行：

1. 校验配置/DB/WAL/SHM/credential/工具/SSH host key/全局 lock；
2. 读取 SQLite 和外部事实，先生成唯一 recovery plan；
3. 若有 recovery，执行一次并立即 read-back；歧义则 blocked；
4. recovery idle 后读取一个 ready candidate；
5. 在 claim 前写入 schema-20 repository policy identity；终态写入 schema-21 discard/closure receipt；
6. 固定 base SHA，生成 exact self-contained bundle；
7. claim Issue，创建或复用唯一 WorkItem，并 PREPARE Runner；
8. 创建/恢复 generation，冻结 Turn input，START/RESUME Codex；
9. 校验 AgentResult、Git、usage、delegation receipt；
10. checkpoint export -> quarantine -> Publisher -> branch/PR/comment/Slack read-back；
11. 导入 Actions/AC evidence，执行 repair/fresh Audit 或进入 review；
12. merge/discard/retention 只走各自独立 receipt。

一个 sweep 最多推进一个主要动作，不追求吞吐量。跨 sweep 的 durable 状态比进程内 continuation 更重要。

## 5. 恢复优先级

恢复优先级从高到低：

1. 数据库/外部 identity conflict；
2. unknown/executing Turn 的 STATUS；
3. Publisher branch/PR/comment/Slack receipt；
4. completion gate、fresh Audit、follow-up intent；
5. merge completion/discard request、精确 PR/Issue closure 和对应 receipt；
6. WorkItem archive/ARCHIVE_STATUS；
7. terminal branch cleanup；
8. 新 Issue。

以下情况必须 blocked，不能猜测：

- Issue node、branch、PR、base/head、repository policy 不一致；
- Runner STATUS 不可用或 container identity 漂移；
- GitHub/Slack 写入已发送但无法确认 receipt；
- Actions 权限、workflow identity、event、head 或结果歧义；
- registry/image/mount/tombstone 的 storage shape 冲突；
- follow-up source/target Turn/generation 不一致；
- login status、工具版本、磁盘容量或 proxy policy 失败。

## 6. 生命周期和磁盘

- completed retention 默认受审值为 7 天；未配置则不自动 archive。
- terminal branch retention 默认受审值为 30 天，并可设置 rollout cutover，防止配置缩短后扫到历史分支。
- blocked/needs_input/review/active 不因时间自动删除。
- completed 在 exact merged PR 对账后由控制面自动关闭 Issue，reason 为 `completed`。
- discard 由人工设置 `agent:discard`，控制面自动关闭 exact unmerged PR 和 Issue；Issue reason 为
  `not_planned`。Issue 关闭后仍是审计入口，reopen 不会恢复旧 WorkItem。
- Runner 先写永久 archive tombstone，再只删除 exact WorkItem image；registry 保留。
- branch cleanup 必须绑定 repository/Issue/branch/exact head 和 GitHub read-back。
- release/image reclamation 先生成不授权 apply 的 exact plan，记录 commit/digest/path/bytes/reference inventory；
  apply 前重新检查，使用永久 mode-`0600` receipt。
- 禁止 `docker image prune`、glob 删除、未解析变量和模糊 release 清理。

## 7. systemd 和目录基准

Control Host：

```text
/opt/codex-dispatcher/releases/<commit>/
/opt/codex-dispatcher/current -> releases/<commit>
/opt/codex-dispatcher/release-receipts/
/etc/codex-dispatcher/config.toml
/etc/codex-dispatcher/dispatcher.env
/var/lib/codex-dispatcher/state.db
/var/lib/codex-dispatcher/{mirrors,source-temporary,quarantine,publisher-temporary}/
/var/lib/codex-dispatcher/{backups,disaster-recovery-drills}/
/run/codex-dispatcher/dispatcher.lock
```

Runner：

```text
/srv/codex-runner/releases/<commit>/
/srv/codex-runner/current -> releases/<commit>
/srv/codex-runner/etc/config.json
/srv/codex-runner/work-items/
/srv/codex-runner/{reclamation-plans,reclamation-receipts}/
/srv/codex-runner/run/active.lock
/var/lib/codex-runner/home/
```

Control 的 dispatcher、health、backup、restore-drill 均为 oneshot + timer。Runner capacity timer 独立。
release apply 后 timer 保持 stopped，直到人工观察 preflight、一个 write sweep、health、backup/restore 状态。

## 8. 发布和回滚

发布顺序：

1. 本地完整测试、compileall、diff check；
2. 生成源归档和 SHA-256；
3. 只读 release plan，确认无活动 service/Turn、lock 可得、两台主机当前/rollback identity；
4. 停 timer 并等待 service 自然 quiesce；
5. Runner candidate 验证并切换；
6. Control candidate 验证并切换；
7. 写永久 transaction receipt，保留 prior links；
8. 手动 preflight、一次 recovery-first sweep、health；
9. 恢复 timer 并 read-back。

自动 rollback 只在 receipt identity、InvocationID 和 schema boundary 仍精确匹配时允许。运行过新 sweep 后，
数据库或外部状态回滚必须使用备份 + recovery-first 对账，不能只改 symlink。

## 9. 备份、恢复和健康

- daily online backup 不读取网络凭据，验证源库和备份 integrity 后 mode-`0600` 原子公布；
- 保留最老 migration anchor 和最新七份，任一候选损坏则不轮转；
- weekly restore drill 在临时数据库验证 integrity、foreign keys、migration 1–21 后删除临时库；
- full DR 在隔离 root 恢复数据库，对账 release receipt、Control/Runner commit、Runner tombstone/absence、
  GitHub、Slack，并演练空 Control application root；不替换在线环境；
- health 监控 active/blocked Turn、archive/branch backlog、GitHub API metrics/cursor、follow-up intent、
  no-progress exhaustion、systemd 和 Runner capacity，并使用 durable Slack alert/recovery outbox。

## 10. 验收门槛

每次代码改动至少执行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests
git diff --check
```

发布还必须执行：

- Control 和 Runner 实际 service account 下的完整测试；
- wrapper shell syntax 和 `systemd-analyze verify`；
- release plan/apply receipt read-back；
- `ssh-preflight` recovery idle 或唯一预期 recovery；
- 一个 write-enabled sweep，确认 external write/read count；
- lifecycle health 零意外 alert；
- backup/restore 状态和所有 timer active/enabled；
- 两台主机 current release 与 receipt 一致。

Live Fixture 只在新故障边界、协议变化、外部 provider 行为变化或新 repository class 准入时增加。已经有
永久 receipt 的场景不重复制造 Issue/PR/镜像。

## 11. 保留兼容与已删除兼容

保留：

- migration 1–21 文件和 committed schema ledger；
- 历史 AgentResult、protocol-v1 receipt、legacy generation、旧 archive storage shape 和 schema-1 Handoff
  的只读灾备解析；
- 已归档/absence-reconciled WorkItem 的永久证据。

已删除：

- 第二执行器和旧 Cloud adapter；
- 旧 Cloud dry-run/contract；
- 旧 `Run` API 和 branch-preparation workspace；
- repository 配置中的无效 environment ID；
- 新工作创建旧格式记录的入口。

历史表 `runs`/`run_events` 只因 additive migration identity 保留，不再有应用读写 API。不得为了清理名称执行
破坏性 DDL 或改写历史 receipt。

## 12. 当前不做

- 自动 merge、release、deploy；
- Slack 入站控制；
- 多 Dispatcher、多活或并发 Turn；
- 普通 higher-value admission；
- production secrets、内网或 production self-hosted CI；
- 把 Agent summary/tests/paths 作为可信验收事实；
- 无 receipt 的分支、WorkItem、release 或 image 删除。
