# NAS 与 MCPHub 接入

本说明是待操作者执行的步骤；开发交付只在云开发环境中模拟测试，没有连接 NAS、MCPHub 或理想账号。首版无需访问任何理想域名。MCPHub 产品版本/实际配置结构未提供，下面是协议契约，不声称已经打通该网关。

## NAS 部署

1. 检查 NAS 支持 Linux 容器、Python 3.12 镜像及 Docker Compose v2。开发环境验证 Linux amd64；ARM64 镜像和 NAS 品牌平台需要另行验证。
2. 拉取审阅后的代码，在 NAS 本地运行 README 的配置生成与 Compose 步骤。不要复用文档中的示例文本作凭据；生成器使用高熵随机值。保持 runtime 目录 0700、token 文件 0600。
3. `runtime/auth.json` 只有凭据摘要和授权映射。Compose file secret 绑定文件需要容器 uid 10001 可读，示例使用该文件 0644 配合父目录 0700。无秘密的 `config.docker.local.json` 也需要容器可读（0644）。可改用宿主文件 ACL 或真正的 secret 管理器实现最小读取权限；不要放宽原始 token 文件权限。
4. 容器根文件系统只读、非 root，持久卷 `/data` 由镜像 uid 10001 拥有。绑定自建宿主 data 目录时自行设为 uid/gid 10001。单进程单副本，不增加 uvicorn workers，不复制活动数据目录部署另一实例。
5. 只绑定 `127.0.0.1:8000`；通过 NAS 同机反代或受控隧道访问。公网上必须使用 TLS；优先私有网络/mTLS，并在防火墙限制到网关出口。不要直接把 8000 暴露公网。
6. `docker compose ps` 和 `/healthz` 只表示进程可用、mock 后端运行，不表示理想账号连通或车辆在线。用官方客户端 smoke 验证 MCP。

`config.docker.local.json` 中 `allowed_hosts` 要列出到达 Python 服务时的实际 Host（不带端口）；默认 localhost/127.0.0.1。反代可把后端 Host 固定到 localhost，或将专用服务域名加入白名单。Origin 缺省可接受；出现 Origin 时必须精确匹配 `allowed_origins` 的完整 scheme/host/port。不要使用通配符。JSON/SSE Accept、Authorization 和 MCP 相关协议头需要被正确转发，`/mcp` 不应被改写成其他路径。

反代应限制请求速率、并发连接、头大小和读超时；服务另有限制 16 KiB 请求体和 32 个后台任务。禁止反代记录 bearer、请求体或上游响应体。不使用 query token。

## MCPHub 契约

- 对客户端提供 MCPHub 自己的 OAuth 授权流程，授权对象/资源是网关的 MCP 接入。
- 注册 Streamable HTTP 后端 URL：`https://<专用私有或受控域名>/mcp`。
- 在安全存储中为每个用户配置 NAS 生成的专用后端凭据，发送 `Authorization: Bearer <该用户的后端凭据>`。
- 客户端 OAuth access token 在网关终止；NAS 后端凭据只发 NAS；理想登录材料将来只在 NAS 使用。这三者不能互换。
- 开始只赋 `vehicle:read`；位置是独立 consent 和 NAS grant；空调还需 NAS `enable_control`。MCPHub 的确认 UI 可以额外限制调用，但不能取代 NAS 权限检查。
- 用户 A/B 必须分别配置不同凭据与 Principal。若网关只有一个全局 backend credential，只能作为单用户服务；不要开放给其他用户后再靠模型声明身份。
- NAS 当前不提供 OAuth discovery、authorization endpoint 或 token exchange。若网关只支持后端 OAuth 且不能发送独立 bearer，则需要实现并审阅适配层后才能接入，不能开启匿名模式绕过。

建议验收：不同用户车辆列表相互隔离；只读凭据无法读位置或写空调；伪造 X-User-Id 无效；断开连接后重复相同 idempotency_key 获得同一 operation_id；未知状态不重复发送。美国网关到 NAS 的延迟、SSE 代理缓冲、DNS、证书、OAuth 回调和网关版本均需实际环境联调，尚未验证。

## 更新、备份和停止

`docker compose stop` 停止服务；停止后备份整个数据卷及受保护配置，保留同一幂等记录。更新镜像用 `docker compose up -d --build --wait`。重启遇到未结束操作会保守标为 unknown。不要在保存幂等语义的服务上用 `docker compose down -v`；它只适合丢弃的模拟测试环境。

撤销用户访问：在本地 auth 文件移除对应 credential，维护窗口重启。轮换后端凭据：生成新高熵凭据，把 SHA-256 和同一个 Principal 写入服务端配置，网关安全存储同步更新，然后移除旧摘要并重启。不要把明文 token 写进仓库、issue、PR、终端录屏或命令参数。

## 受控构建网络

如构建机器通过自定义 CA 的 HTTPS 代理联网，Dockerfile 支持可选 `build_ca` BuildKit secret，分别供 pip 和 uv 验证证书；默认普通网络不需要它。可用 `docker build --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --secret id=build_ca,src=/安全路径/ca-bundle.pem -t lixiang-mcp:local .`，随后 `docker compose up -d --no-build --wait`。需确保构建容器能解析代理域名。不要禁用 TLS 验证，或把代理凭据/CA bundle 复制进项目与运行镜像。
