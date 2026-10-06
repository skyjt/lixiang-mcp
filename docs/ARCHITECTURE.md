# 架构与可靠性

```text
MCP 客户端
  → 美国 MCPHub：用户 OAuth、用户授权与后端凭据选择
  → 私有隧道或 TLS/mTLS 反代
  → NAS /mcp：专用后端 Bearer 凭据 → 服务端 Principal
  → VehicleService：scope + 车辆授权列表 + 账号实时归属 + 能力 + 参数
  → MockBackend（首版唯一后端，无外网请求）
  → SQLite 操作记录 / 每车 asyncio.Lock
```

业务服务是与 MCP 无关的 Python 模块，MCP 是薄适配层；首版同一 Python 进程部署，避免额外暴露一套业务 HTTP API。模型和传输均不依赖 Home Assistant。`Backend`、`LoginProvider`、`SigningProvider` 是后续扩展边界，不通过动态插件/任意 URL 让模型指定后端。

## 可信身份和授权

- OAuth 由 MCPHub 负责。NAS 不是 OAuth 授权服务器，不接收理想凭据作为 MCP token，也不把 MCPHub OAuth token 传给理想。
- NAS 只接受本地授权文件中摘要匹配的专用随机后端凭据。此凭据同时是该固定 Principal 的身份和授权能力。每个用户分别配置，服务端绑定 `subject`、`account_id`、`vehicle_ids`、`scopes`。
- 任意 `X-User-Id`、`X-Account-Id`、`X-Forwarded-*`、工具参数内用户身份、`confirmed` 都不是授权依据。HTTP stateless 模式逐请求鉴权，不靠会话 ID 授权。
- 共享一个后端凭据即共享同一个 Principal；不能用一个全权凭据加未签名用户头模拟多租户隔离。若 MCPHub 无法按用户选择凭据，使用独立用户实例/连接，或先实现可验证 audience/issuer 的专用断言交换层。
- 后端车辆账号返回的归属与管理员 grant 必须同时匹配。未来真实适配器必须只返回可用的 owned/authorized 关系，拒绝 inviting/transferring 等未完成授权，不可直接使用上游宽列表作为权限。
- 本地授权文件、配置、SQLite 和反代管理权限属于可信运维边界。配置变更需要重启；撤销凭据后在维护窗口重启，执行中的命令可能已发送，不能声称撤销了车辆动作。
- 位置不进入普通状态响应或操作记录。业务 scope 单独检查，工具可被发现不代表调用被允许。

## 操作和恢复

1. 入参验证、scope、显式 vehicle_id、当前账号归属、车型能力、温度范围/步进。
2. SQLite 唯一 `(subject, hash(idempotency_key))`；对 vehicle_id / 开关 / 温度建立指纹，持久化操作后返回。没有把原始幂等键或凭据写入日志/数据库。
3. 每车一个锁。同车从发送到轮询及车辆确认完整串行；不同车可并行。锁键为全局模拟车辆 ID，真实适配器必须保证同一实车不因账号别名获得两个锁键。
4. 进入队列后再次检查归属及该车是否有未知操作。最多 32 个未完成任务，超出拒绝。
5. 只调用一次 `submit_climate`。云端接受、网络断开、结果轮询超时不能可靠区分，保守记 unknown；没有自动重试写入。读取云端结果可轮询，但不重新提交命令。
6. 云端结果成功仍要验证车辆信号：时间不早于本次开始、非陈旧、开关及需要的温度匹配。读到目标值只证明状态匹配，不证明唯一因果来源。
7. 中断/重启时，submitted/running/cloud_completed 统一转 unknown；不恢复后台任务，不重放命令。单机 flock 防止第二 worker 误把活跃任务当中断；仅支持 Linux 本地文件系统单副本，禁止 NFS 共享、多主复制。

SQLite 用 WAL + synchronous FULL 持久化幂等和阶段事件；不设置幂等自动过期，避免晚到重试重新执行。长时间运行需运维监控磁盘容量，首版未做归档、配额或人工 reconcile 工具。数据库写入故障应停服检查，不通过删除记录“修复”。

unknown 后没有模型可调用的清除/重试接口。演练时可以停止服务并为**全新模拟环境**指定新的数据库路径；不要清理旧日志后把同样做法用于真实车辆。真实后端上线前需要可信人工核对及审计的恢复流程。

## 时间与连接

每个信号带 `sampled_at`、`stale` 和单位，状态整体带 `observed_at` 和 stale。模拟信号显式生成采样时间；协议解析器保留上游时区，不把读取时间伪造成上游采样时间。缺失时间、无时区、未来偏移超过 5 秒、超 TTL 或缺失值均陈旧。普通读取不会唤醒车辆。连接信号不保证命令可执行。

## 账号会话续期

`SessionManager` 按 `account_id` 加锁并在锁内重查缓存，20 个并发读取只触发一次续期；不同账号隔离。`VehicleSession` 不在 repr 中显示 token。`UnconfiguredLogin` / `UnconfiguredSigner` 默认抛错；没有加载真实账号文件、PAKE 参数或签名材料。首版 MockBackend 不需要登录，不调用这些接口，测试通过模拟 Provider 验证续期逻辑。

## 日志

进程关闭 HTTP access log；根 handler 使用固定事件白名单，其他文字统一 `external_event_redacted`，移除参数和异常堆栈。为了减少敏感日志风险，牺牲了部分诊断细节。禁止临时启用请求体、headers、第三方协议 DEBUG 日志；外部反代也必须关闭 Authorization、query/body 和坐标记录。数据库仅包含模拟车辆别名、用户别名、指纹、阶段时间及固定错误码，没有位置、凭据或原始响应。

MockBackend 的车辆状态仅在内存中演练，进程重启后回到合成初始值；SQLite 中旧操作仍是历史结果，不能当作当前车辆状态。模拟环境每次读取会生成新的模拟采样，真实适配器必须使用车辆端真实采样时间。
