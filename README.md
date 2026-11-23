# ci-actions

homelab CI（Gitea Actions 与 GitHub Actions）的可复用安全构件。目前提供：

- `spiffe-jwt-svid`：取一个 JWT-SVID（本地 SPIRE agent，或 GitHub 上经 spire-identity-exchange）。
- `spiffe-openbao-login`：用 runner 的 JWT-SVID 换取短期 OpenBao token，并在 job 的
  post 阶段尽力吊销（失败只告警，token 到 TTL 自然失效）。
- `setup-nexus`：为匿名读取配置 Ansible Galaxy、Go Modules 与 OCI repository 地址。
- `setup-artifact-keeper`：让 Go、PyPI、Ansible Galaxy、Maven（Gradle）、npm、Go 工具链、Terraform provider 与 Docker Hub 下载走 Artifact Keeper 的
  匿名 pull-through 缓存。
- `publish-techdocs`：生成仓库的 TechDocs 并发布到共享的 SeaweedFS `techdocs` bucket。
- `security-scan`：semgrep、gitleaks、trivy 报告模式扫描，结果汇总为一条 PR 评论。
- `upgrade-review`：coding agent 评审 Renovate 的依赖升级 PR，shell 跑在一次性 OpenSandbox VM 里，见
  [`upgrade-review/README.md`](upgrade-review/README.md)。

## 引用方式

写 `ddupan/ci-actions/<action>@v1`。仓库在 GitHub（`github.com/ddupan/ci-actions`，公开），
Gitea 的 `DEFAULT_ACTIONS_URL` 指向 github.com，所以同一写法在 Gitea 和 GitHub Actions 上都能解析。
`git.ddupan.top/panxiao81/ci-actions` 已归档，只读，旧的完整 URL 引用还能用但不再更新。

`v1` 随 main 自动移动（`.github/workflows/move-major-tag.yml`），合并后所有引用方立即生效，
不用手动打 tag。不兼容的改动改用新的主版本。

## Runner 前提

`spiffe-openbao-login` 与 `spiffe-jwt-svid` 不下载工具，JWT-SVID 的来源按 runner 自动选择（`lib/svid.js`）：

- 有 SPIFFE Workload API socket（Gitea 的一次性 executor）：用 `spire-agent api fetch jwt`，runner
  image 需带 Node.js 20 与 `spire-agent`。
- GitHub Actions 跑在 homelab ARC runner（`runs-on: homelab`）上：把 runner Pod 的 projected token
  和 job 的 GitHub OIDC token 交给 spire-identity-exchange 换 SVID（homelab-infra
  `platform/sandbox-identity-exchange`）。job 需要 `permissions: id-token: write`；SPIFFE ID 是
  `ci/github/<owner>/<repo>/<workflow>`，由 homelab-infra `platform/spire/github-ci.yaml` 的 entry 决定。

身份注册、OpenBao role 与 policy 仍由基础设施 IaC 管理，Action 不创建或扩大授权。

`spiffe-jwt-svid` 只输出一个 SVID（`steps.<id>.outputs.svid`，日志里已遮蔽），例如推 zot：

```yaml
- id: svid
  uses: ddupan/ci-actions/spiffe-jwt-svid@v1
  with:
    audience: zot
- run: echo "$SVID" | docker login zot-push.lab.ddupan.top --username zot --password-stdin
  env:
    SVID: ${{ steps.svid.outputs.svid }}
```

```yaml
- uses: ddupan/ci-actions/spiffe-openbao-login@v1
  with:
    role: ci-example

- uses: ddupan/ci-actions/setup-nexus@v1
  with:
    ansible: 'true'
    go: 'true'
    oci: 'true'
```

登录 Action 将 `BAO_ADDR`、`BAO_TOKEN` 写入 runner 的 `GITHUB_ENV`，并通过
`GITHUB_STATE` 将 token 交给 post action。它们属于 runner 管理的 job 临时文件；因此只允许
在一次性 Pod/VM runner 使用，不得在共享常驻 runner 或会持久化工作目录的 runner 上使用。
token 不作为 Action output，也不上传 artifact/cache。

`setup-nexus` 只配置匿名读取，不读取 OpenBao secret。正式发布账号建立后，应新增独立的
Nexus login Action，并绑定精确 SPIFFE ID 与最小 OpenBao policy；不得让本 Action 读取
管理员凭据。

## setup-artifact-keeper

按需打开各生态，只写环境变量和本机 Docker 配置，不读取 secret：

```yaml
- uses: ddupan/ci-actions/setup-artifact-keeper@v1
  with:
    go: 'true'      # GOPROXY；GOPRIVATE=git.ddupan.top 让自有模块直连
    go-version-file: go.mod  # 可选；从缓存装 Go 工具链，替代 actions/setup-go
    pypi: 'true'    # PIP_INDEX_URL、UV_DEFAULT_INDEX
    ansible: 'true' # ANSIBLE_GALAXY_SERVER
    docker: 'true'  # job dockerd 的 registry-mirrors，以及 buildx 新建 builder 的 BuildKit mirrors
    gradle: 'true'  # init script 把 Maven Central 与 Gradle Plugin Portal 改指缓存
    gradle-user-home: /tmp/ci/gradle  # 可选；默认 $GRADLE_USER_HOME 或 ~/.gradle
    npm: 'true'     # <npm-home>/.npmrc 的 registry
    npm-home: /tmp/ci/npm  # 可选；默认 $HOME
    terraform: 'true'  # TF_CLI_CONFIG_FILE：registry.terraform.io 的 provider 走 network mirror
```

`go-version-file` 读 `toolchain` 行，没有则读 `go` 行；只写到次版本（`go 1.27`）时取最新补丁版，
同 actions/setup-go。Go 发行版本身就是 `golang.org/toolchain` 模块，所以从同一个 Go 缓存下载。

`npm` 写 `.npmrc` 而不是环境变量，在容器里跑、`HOME` 挂同一目录的 npm 也能用；npm 会把
`package-lock.json` 里 registry.npmjs.org 的地址改写到配置的 registry。

`gradle` 把 init script 写到 `<gradle-user-home>/init.d/`，URL 直接写进脚本，所以在容器里跑、读不到
job 环境变量的 Gradle 也能用，只要容器的 `GRADLE_USER_HOME` 挂的是同一目录。只改写指向
`repo.maven.apache.org`、`repo1.maven.org`、`plugins.gradle.org` 的仓库，其他仓库不动。

`docker` 写 `/etc/docker/daemon.json` 后向 dockerd 发 SIGHUP 热加载，需在 job 里 sudo；只代理
Docker Hub（`docker.io`）。BuildKit 的配置通过 `BUILDX_CONFIG` 下的 `buildkitd.default.toml`
生效，只影响本 step 之后、未显式传 `--buildkitd-config` 的 `docker buildx create`。

## publish-techdocs

仓库根目录需要 `mkdocs.yml`、`docs/index.md`，以及带 `backstage.io/techdocs-ref` 的
`catalog-info.yaml`。OpenBao role `ci-techdocs-publisher` 只接受以 `techdocs` 结尾的 SPIFFE ID：

- Gitea：**job id 必须是 `techdocs`**（`spiffe://ddupan.top/ci/panxiao81/<repo>/techdocs`）。
- GitHub：**workflow 文件必须是 `.github/workflows/techdocs.yml`**，job 跑在 `runs-on: homelab`、带
  `permissions: id-token: write`，并在 homelab-infra `platform/spire/github-ci.yaml` 登记 entry
  （`spiffe://ddupan.top/ci/github/<owner>/<repo>/techdocs`）。runner 镜像已带 Python 3 与 Node。

```yaml
on:
  push:
    branches: [main]
    paths: [docs/**, mkdocs.yml, catalog-info.yaml]
  workflow_dispatch:
jobs:
  techdocs:
    runs-on: [self-hosted, pod]
    steps:
      - uses: actions/checkout@v4
      - uses: ddupan/ci-actions/publish-techdocs@v1
```

登录、读取 S3 凭据、发布与吊销 token 在同一个 step 内完成，token 与 AK/SK 不写入
`GITHUB_ENV`，也不进入后续 step。所有仓库共用一个发布身份，任一仓库都能覆盖其他实体的
文档；有外部仓库接入时应拆分为按仓库的 role 与 S3 身份。

## security-scan

报告模式：发现问题只在 PR 上评论（重跑原地更新同一条），从不让 step 失败；扫描器出错在报告里标 ❌。
PR 上 semgrep 只报相对 merge-base 新增的问题，gitleaks 只扫 PR 的提交；trivy 查依赖漏洞，总是全量。
非 PR 事件（schedule、workflow_dispatch）全量扫描，报告只写 job 日志。

```yaml
on:
  pull_request:
  schedule:
    - cron: '17 3 * * 1'   # 依赖漏洞随时间出现，与 PR 无关
jobs:
  security-scan:
    runs-on: [cloud]   # 不需要 Docker 和内网，也可以用 [self-hosted, pod]
    steps:
      - uses: actions/checkout@v4
      - uses: ddupan/ci-actions/security-scan@v1
        with:
          cloud: 'true'                      # 云端 runner：扫描器从 GitHub、PyPI 下载；内网 runner 不写
          scanners: semgrep,gitleaks,trivy   # 默认全开，按需去掉
          semgrep-config: p/default          # 空格分隔；可写 p/java、p/golang 或仓库内规则文件
```

报告只列规则、位置和版本，不输出代码行：semgrep 的命中行和 gitleaks 的 Match 可能就是密钥本身。
浅克隆也能用，PR 上 action 会自己补取到 merge-base 的历史。

各仓库的差异写在**工具自己的配置文件**里，放在仓库根目录，action 不需要额外输入：

| 需求 | 文件 |
|---|---|
| 排除目录 | `.semgrepignore`；`trivy.yaml` 的 `scan.skip-dirs` |
| 已确认的误报、已轮换的历史密钥 | 代码里 `# nosemgrep: <rule>`；`.gitleaksignore`（报告里的 Fingerprint 列，确认已轮换后原样写入）；`.trivyignore` |
| 自定义密钥规则 | `.gitleaks.toml` |
| 漏洞级别阈值 | `trivy.yaml` 的 `severity` |

⚠ 自定义 `.semgrepignore` 会**替换** semgrep 的默认忽略列表（`tests/`、`node_modules/` 等），不是追加；
需要的条目自己写回，`:include .gitignore` 可以引入 `.gitignore`。

扫描器直接装在 runner 上，不需要 Docker：semgrep 从 PyPI 装进 venv，gitleaks/trivy 从 GitHub release
下载，并用同一 release 的 checksums 文件校验。内网 runner 经 Artifact Keeper 的 PyPI 与 github-releases
缓存下载，`cloud: 'true'` 时直连。版本号固定在 `action.yml`，由 Renovate 更新。
