# 架构与可靠性

```text
MCP 客户端
  → 美国 MCPHub：用户 OAuth、用户授权与后端凭据选择
  → 私有隧道或 TLS/mTLS 反代
  → NAS /mcp：专用后端 Bearer 凭据 → 服务端 Principal
  → VehicleService：scope + 车辆授权列表 + 账号实时归属 + 能力 + 参数
  → MockBackend（默认，无外网请求） / CloudBackend（实验性、显式私密配置）
  → SQLite 操作记录 / 每车 asyncio.Lock
```

业务服务是与 MCP 无关的 Python 模块，MCP 是薄适配层；首版同一 Python 进程部署，避免额外暴露一套业务 HTTP API。模型和传输均不依赖 Home Assistant。`Backend` 为业务适配边界；cloud 包实现独立的 AuthSession / Signer / VehicleAPI，不通过动态插件/任意 URL 让模型指定后端。

## 可信身份和授权

- OAuth 由 MCPHub 负责。NAS 不是 OAuth 授权服务器，不接收理想凭据作为 MCP token，也不把 MCPHub OAuth token 传给理想。
- NAS 只接受本地授权文件中摘要匹配的专用随机后端凭据。此凭据同时是该固定 Principal 的身份和授权能力。每个用户分别配置，服务端绑定 `subject`、`account_id`、`vehicle_ids`、`scopes`。
- 任意 `X-User-Id`、`X-Account-Id`、`X-Forwarded-*`、工具参数内用户身份、`confirmed` 都不是授权依据。HTTP stateless 模式逐请求鉴权，不靠会话 ID 授权。
- 共享一个后端凭据即共享同一个 Principal；不能用一个全权凭据加未签名用户头模拟多租户隔离。若 MCPHub 无法按用户选择凭据，使用独立用户实例/连接，或先实现可验证 audience/issuer 的专用断言交换层。
- 后端车辆账号返回的归属与管理员 grant 必须同时匹配。CloudBackend 只返回可用的 owned/已知 family authorized 关系，拒绝 inviting/transferring 等未完成授权，不可直接使用上游宽列表作为权限。
- 本地授权文件、配置、SQLite 和反代管理权限属于可信运维边界。配置变更需要重启；撤销凭据后在维护窗口重启，执行中的命令可能已发送，不能声称撤销了车辆动作。
- 位置不进入普通状态响应或操作记录。业务 scope 单独检查，工具可被发现不代表调用被允许。

## 操作和恢复

1. 入参验证、scope、显式 vehicle_id、当前账号归属、车型能力、温度范围/步进。
2. SQLite 唯一 `(subject, hash(idempotency_key))`；对 vehicle_id / 开关 / 温度建立指纹，持久化操作后返回。没有把原始幂等键或凭据写入日志/数据库。
3. 每车一个锁。同车从发送到轮询及车辆确认完整串行；不同车可并行。锁键为全局唯一的本地车辆别名；CloudConfig 拒绝同一 VIN 绑定多个别名或账号，保证本实例内同一实车只有一个锁键。仅支持单实例单副本。
4. 进入队列后再次检查归属及该车是否有未知操作。最多 32 个未完成任务，超出拒绝。
5. 只调用一次 `submit_climate`。提交前确定拒绝、提交路径提供的明确未接受证据、有效数字错误码对应的云端拒绝可记 failed。开始提交后的普通业务异常（包括归属暂时缺失、结果查询或确认失败）都记 unknown，不能因异常名为 `cloud_rejected` 就认定拒绝。空白/非数字结果码同样未知。没有自动重试写入；读取云端结果可轮询，但不重新提交命令。
6. 云端结果成功仍要验证车辆信号：采样时间不早于本次提交响应、非陈旧、开关及需要的温度匹配。读到目标值只证明状态匹配，不证明唯一因果来源。
7. 中断/重启时，submitted/running/cloud_completed 统一转 unknown；不恢复后台任务，不重放命令。单机 flock 防止第二 worker 误把活跃任务当中断；仅支持 Linux 本地文件系统单副本，禁止 NFS 共享、多主复制。

SQLite 用 WAL + synchronous FULL 持久化幂等和阶段事件；不设置幂等自动过期，避免晚到重试重新执行。长时间运行需运维监控磁盘容量，首版未做归档、配额或人工 reconcile 工具。数据库写入故障应停服检查，不通过删除记录“修复”。

unknown 后没有模型可调用的清除/重试接口。演练时可以停止服务并为**全新模拟环境**指定新的数据库路径；不要清理旧日志后把同样做法用于真实车辆。真实使用前仍需要可信人工核对及审计的恢复流程。

## 时间与连接

每个信号带 `sampled_at`、`stale` 和单位，状态整体带 `observed_at` 和 stale。模拟信号显式生成采样时间；协议解析器保留上游时区，不把读取时间伪造成上游采样时间。缺失时间、无时区、未来偏移超过 5 秒、超 TTL 或缺失值均陈旧。普通读取不会唤醒车辆。连接信号不保证命令可执行。

## 账号会话续期

CloudBackend 实际使用 `cloud.auth.AuthSession`：每个账号一套 cookie jar 和锁；锁内串行登录、主 token refresh、scope 交换和缓存失效。缓存键为 audience+完整 scope（VAT 包含 VIN）。成对获取 MESH/VAT 时检查会话 generation，重建 cookie 会话会重取整组。账号挑战失败关闭后阻断，不反复登录。可从与设备/profile 绑定的私密快照恢复主 token/refresh/cookie。`persist_vehicle_sessions` 默认关闭，向导生成的本机配置开启它；SessionFile 在独立锁内核对配置摘要、原子写回，失败不忽略。没有将凭据写入操作 SQLite。

旧 `session.py` 的通用 Provider 接口及其测试保留供兼容；CloudBackend 不经过其中的 Unconfigured 占位类。实际调用链、外部安全配置和剩余未知见 [PROTOCOL_ADAPTER.md](PROTOCOL_ADAPTER.md)。

## 独立本机接入

`lixiang-setup` 是另一个仅监听 loopback 的进程，不挂载到 MCP。本机浏览器能力凭据、精确 Host/Origin/来源检查、16 KiB 请求上限、禁止 iframe 和缓存保护账号输入。普通表单只有手机号/密码；版本化 profile 不含设备或签名秘密。

Checkpoint 记录进度版本、账号设备、尝试次数与待选车辆；每次接受动作先落盘，再启动可取消任务。状态 GET 不登录，旧 revision 不执行；每账号 15 分钟最多 3 次显式尝试，每次最多一次密码登录。中断恢复需本人继续，取消阻止后台任务写回。官方 H5 使用同一设备；签名材料换设备需重新登录。

账号会话成功仍可能停在签名材料缺失，不能跳过。选车后仅导出 vehicle:read，两个控制开关及能力均为 false。已有连接先停止 MCP 才可重新登录；取消恢复原配置，数据库保留，账号/车辆集合锁定。文件权限/加密与真实兼容性限制见 [ONBOARDING.md](ONBOARDING.md)。

## 日志

进程关闭 HTTP access log；根 handler 使用固定事件白名单，其他文字统一 `external_event_redacted`，移除参数和异常堆栈。为了减少敏感日志风险，牺牲了部分诊断细节。禁止临时启用请求体、headers、第三方协议 DEBUG 日志；外部反代也必须关闭 Authorization、query/body 和坐标记录。数据库仅包含本地车辆别名、用户别名、指纹、阶段时间及固定错误码，没有位置、凭据或原始响应。

MockBackend 的车辆状态仅在内存中演练，进程重启后回到合成初始值；SQLite 中旧操作仍是历史结果，不能当作当前车辆状态。模拟环境每次读取会生成新的模拟采样，真实适配器必须使用车辆端真实采样时间。

数据库还保存后端绑定命名空间，拒绝把 mock 操作历史或重新映射 VIN 的别名用于另一个后端。命名空间只存绑定摘要，不存原始 VIN；真实操作标记 simulated=false。
