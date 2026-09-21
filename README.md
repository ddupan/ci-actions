# ci-actions

homelab Gitea Actions 的可复用安全构件。目前提供：

- `spiffe-openbao-login`：用 runner 的 JWT-SVID 换取短期 OpenBao token，并在 job 的
  post 阶段主动吊销。
- `setup-nexus`：为匿名读取配置 Ansible Galaxy、Go Modules 与 OCI repository 地址。

## Runner 前提

`spiffe-openbao-login` 不下载工具；runner image 必须包含 Node.js 20、`spire-agent`，并挂载
可访问的 SPIFFE Workload API socket。身份注册、OpenBao role 与 policy 仍由基础设施 IaC
管理，Action 不创建或扩大授权。

```yaml
- uses: panxiao81/ci-actions/spiffe-openbao-login@v1
  with:
    role: ci-example

- uses: panxiao81/ci-actions/setup-nexus@v1
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
