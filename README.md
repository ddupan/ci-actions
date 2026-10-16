# ci-actions

homelab Gitea Actions 的可复用安全构件。目前提供：

- `spiffe-openbao-login`：用 runner 的 JWT-SVID 换取短期 OpenBao token，并在 job 的
  post 阶段尽力吊销（失败只告警，token 到 TTL 自然失效）。
- `setup-nexus`：为匿名读取配置 Ansible Galaxy、Go Modules 与 OCI repository 地址。
- `setup-artifact-keeper`：让 Go、PyPI、Ansible Galaxy、Maven（Gradle）、npm、Go 工具链、Terraform provider 与 Docker Hub 下载走 Artifact Keeper 的
  匿名 pull-through 缓存。
- `publish-techdocs`：生成仓库的 TechDocs 并发布到共享的 SeaweedFS `techdocs` bucket。

## 引用方式

Gitea 的 `DEFAULT_ACTIONS_URL` 指向 github.com，简写 `panxiao81/ci-actions/...` 会被解析到
GitHub 而失败；必须写完整 URL `https://git.ddupan.top/panxiao81/ci-actions/<action>@v1`。

## Runner 前提

`spiffe-openbao-login` 不下载工具；runner image 必须包含 Node.js 20、`spire-agent`，并挂载
可访问的 SPIFFE Workload API socket。身份注册、OpenBao role 与 policy 仍由基础设施 IaC
管理，Action 不创建或扩大授权。

```yaml
- uses: https://git.ddupan.top/panxiao81/ci-actions/spiffe-openbao-login@v1
  with:
    role: ci-example

- uses: https://git.ddupan.top/panxiao81/ci-actions/setup-nexus@v1
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
- uses: https://git.ddupan.top/panxiao81/ci-actions/setup-artifact-keeper@v1
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
`catalog-info.yaml`。workflow 的 **job id 必须是 `techdocs`**：OpenBao role
`ci-techdocs-publisher` 只接受 `spiffe://ddupan.top/ci/panxiao81/*/techdocs`。

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
      - uses: https://git.ddupan.top/panxiao81/ci-actions/publish-techdocs@v1
```

登录、读取 S3 凭据、发布与吊销 token 在同一个 step 内完成，token 与 AK/SK 不写入
`GITHUB_ENV`，也不进入后续 step。所有仓库共用一个发布身份，任一仓库都能覆盖其他实体的
文档；有外部仓库接入时应拆分为按仓库的 role 与 S3 身份。
