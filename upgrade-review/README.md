# upgrade-review

Renovate 开的依赖升级 PR 上运行 [pi](https://pi.dev)（coding agent），判断升级在本仓库的用法下会不会坏，
结论以评论贴到 PR。报告模式：只评论，不合并，不让 CI 变红。

| 文件 | 作用 |
|---|---|
| `action.yml` | checkout、Artifact Keeper、Bao 登录，然后运行 `review.py` |
| `review.py` | 取 key、建 sandbox 并上传源码、以最小环境变量启动 pi、事件流转 OTel span、发评论 |
| `prompt.md` | 追加到 pi 系统提示词的评审要求（与仓库无关；仓库特有的约定由 agent 读 AGENTS.md/CLAUDE.md） |
| `extension.ts` | pi 扩展：`bash` 转发到 sandbox 的 execd，`submit_review` 交结构化结论 |
| `package.json` / `package-lock.json` | 锁定 pi 版本（`npm ci`，经 Artifact Keeper） |
| `review.py.lock` | `uv lock --script` 锁定的 Python 依赖 |

## 接入

```yaml
# .gitea/workflows/upgrade-review.yml
name: upgrade-review
on: pull_request
jobs:
  # job id 是 SPIFFE ID 的一部分：bao role ci-upgrade-review 只接受
  # spiffe://ddupan.top/ci/panxiao81/<repo>/upgrade-review。
  upgrade-review:
    if: github.event.pull_request.user.login == 'renovate-bot'
    runs-on: [self-hosted]
    steps:
      - uses: https://git.ddupan.top/panxiao81/ci-actions/upgrade-review@v1
```

再把 Bot 账号加为只读 collaborator（restricted 账号看不到没加入的仓库）：

```bash
tea api -X PUT /repos/panxiao81/<repo>/collaborators/upgrade-agent -d '{"permission":"read"}'
```

CI 若在 PR 上贴了对评审有用的评论（例如 homelab-infra 的 flux 渲染 diff），在贴评论的同一个 workflow 里
`needs:` 那个 job，并设 `extra-context-marker: '<!-- flux-diff -->'`。

## 边界

- 模型只有 `bash`（在 sandbox 里）和 `submit_review`，没有任何在 runner 上执行的工具：runner 上 pi 进程的
  环境里有网关 key 与 sandbox key，本地 read 能读到 `/proc/<pid>/environ`。
- sandbox 是 OpenSandbox 的一次性 Kata VM（2 CPU/3GiB），只有 `git archive HEAD` 的源码；egress 策略
  拒绝 10/8、172.16/12、192.168/16、100.64/10、169.254/16，公网放行。评审结束即删除，job 被杀时按
  timeout（35 分钟）过期。
- 需要的 Bao 读取：`kv/k8s/ai-gateway-keys/<key-id>`、`kv/k8s/opensandbox-api`、`kv/k8s/upgrade-agent`、
  `kv/k8s/otel-collector`（homelab-infra `infrastructure/openbao/terraform/policies/ci-upgrade-review.hcl`）。
  在 key UI 吊销网关 key 即停用评审。
- 评论以 restricted 的 Bot 账号 `upgrade-agent` 发出，token 只有 `write:issue,read:repository`。

## Bootstrap（一次）

评论以 restricted 的 Bot 账号 `upgrade-agent` 发出，只在接入的仓库上有 read 权限；token 只有
`write:issue,read:repository`。Gitea 在 OCI ARM 上（`infrastructure/oci/ansible/roles/gitea`）。

```bash
# 1. 账号。Bot 类型不能设密码（gitea: password can only be set for user accounts），只靠 token。
ssh ubuntu@oci-arm.ddupan.top sudo docker exec gitea-gitea-1 gitea admin user create \
  --username upgrade-agent --fullname "'Upgrade Agent'" --email upgrade-agent@users.noreply.ddupan.top \
  --user-type Bot --restricted

# 2. token 直接进 Bao，不回显。pipefail：生成失败时不能写入空 token。
set -o pipefail
ssh ubuntu@oci-arm.ddupan.top sudo docker exec gitea-gitea-1 gitea admin user generate-access-token \
  --username upgrade-agent --token-name upgrade-review --raw --scopes write:issue,read:repository \
  | tr -d '\n' | bao kv put -mount=kv k8s/upgrade-agent token=-

# 3. 只读 collaborator，每个接入的仓库一次（见上文接入）。
tea api -X PUT /repos/panxiao81/<repo>/collaborators/upgrade-agent -d '{"permission":"read"}'
```

以后放开自动合并时，合并或 approve 再另给权限，不扩大这把评论用的 token。

## 本地运行

```bash
cd upgrade-review && npm ci --registry=https://artifacts.lab.ddupan.top/npm/npm-proxy/
# key 用本机 SPIFFE 身份从 Bao 读出（skill bao-ssh-access）；--source 是 PR head 的检出
UPGRADE_REVIEW_API_KEY=... UPGRADE_REVIEW_SANDBOX_KEY=... UPGRADE_REVIEW_OTLP_TOKEN=... \
  uv run --script review.py --repo panxiao81/homelab-infra --pr <N> --source <checkout> --dry-run
```

## 观测

每次评审一条 trace（service `upgrade-review`），经 `otlp.lab.ddupan.top` 导出：根 span `upgrade_review`
下有 `sandbox setup`、每轮一个 `turn N`（带 token 用量）、每次工具调用一个 `tool <name>`；`traceparent` 经
请求头带给 AI 网关，网关的 `chat <model>` span 在同一条 trace 里。评论末尾印有 trace id。

## 升级 pi

改 `package.json` 的版本后，`npm install --package-lock-only --registry=https://artifacts.lab.ddupan.top/npm/npm-proxy/`
重生成 lock；pi 以 headless 方式运行时 stdin 必须是 `/dev/null`，否则会一直等管道输入。
