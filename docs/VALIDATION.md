# 首版验证记录

验证日期：2026-10-06。仅在独立云开发环境 Linux amd64 / Python 3.12.14 中使用模拟数据，没有连接 NAS、MCPHub、理想登录或真实车辆。

| 检查 | 结果 |
| --- | --- |
| `uv sync --frozen` | 依赖锁可安装 |
| `ruff check .` / `ruff format --check .` | 通过 |
| `mypy`（strict） | 19 个源文件通过 |
| `pytest -q` | 120 passed |
| 官方 MCP SDK + 本地真实 HTTP | initialize、8 tools、读取、模拟写入/查询、幂等复用通过 |
| Dockerfile 多阶段镜像构建 | 成功，固定 Python/uv/lock；开发代理需 build_ca 临时 secret |
| Compose config / 非 root 容器 / healthcheck | 通过，uid/gid 10001，健康状态 healthy |
| 容器官方 MCP 客户端 smoke | `PASS: initialize, 8 tools, 3 mock vehicles, sampled state` |
| 本地交接流程演练 | 干净 clone 安装锁定依赖、生成 mock、官方客户端 smoke；私密配置生成和只读 SDK 示例均用合成材料验证，网络部分仅运行 mock |
| wheel 打包 | 成功，包含本项目 LICENSE 和上游 MIT 原文 |
| 拟提交文件 hygiene | 通过，扫描禁止路径、JWT、VIN 形态、手机号形态等 |
| Gitleaks v8.30.0 拟公开目录扫描 | no leaks found |
| 上游敏感赋值字面量对照 / MIT 原文校验 | 未匹配签名/凭据等候选字面量；MIT 文件逐字节一致 |

测试包括：20 个并发重复调用只提交一次、幂等键冲突、持久化重启/不重放、第二 worker 拒绝启动、同车串行/不同车并行、账号续期加锁、不同账号/车辆隔离、排队时撤销归属/禁用控制、网络响应丢失（车辆可能已改变但结果仍 unknown）、拒绝控制、云端成功未确认、未知车型、旧采样/时间缺失、位置独立权限、错误温度/额外 confirmed 参数、未知 MCP 命令、伪造用户头、Host/Origin 检查、请求体限制和日志敏感参数/异常堆栈脱敏。

已知非失败提示：MCP SDK 1.26.0 与锁定的 pydantic-settings 在初始化时仍产生一条 `IncompleteFieldDefinitionWarning`（lifespan 前向引用）；120 项测试和容器完整链路通过，未屏蔽此提示。

环境问题与处理：Docker 初始构建缺少环境代理 DNS 和 CA，采用已有代理、构建期主机映射和可选 BuildKit `build_ca` secret 后构建成功，没有禁用 TLS 校验；非 root 容器要求只读配置文件可读，README/CI 已明确设置。全工作区扫描曾把刚生成的、Git 忽略的 mock 后端凭据摘要识别为 generic-api-key；公开目录导出扫描不含该本地文件，结果零发现。没有将本地凭据或构建 CA 放入镜像/提交。

GitHub Actions 工作流已配置 Python 检查、秘密扫描、Docker 构建和 smoke。以上记录是本地已完成的验证，远程 Actions 状态请以 PR checks 为准。Docker 运行验证结束后停止测试容器，不进行部署或发布。

这些结果不能证明真实账号可登录、车型协议正确或 MCPHub 已接通；完整限制见 [SECURITY.md](../SECURITY.md)。


## 协议适配扩展验证

`tests/cloud/` 共 81 项契约/加密测试（全项目总计 120 项），涵盖完整 PKCE/PAKE/cookie/token 交换、bcrypt salted/seeded 两种盐、Ed25519 proof、HMAC 黄金向量、主 token/refresh 轮换、多账号 cookie 隔离、MESH/VAT 会话 generation 一致性、部分 scope 拒绝、登录挑战阻断、只读401有限重试、VSS invalid_path 拆分、车辆角色/过户状态、秘密文件权限、数据库后端绑定、协议响应大小、MCP→CloudBackend→协议 HTTP→操作确认全链路。

独立复核的四项问题均先用合成响应复现，再修复：提交后的归属/业务异常保留 unknown 屏障；提交与结果接口的空白/非数字码不能证明拒绝；畸形登录/scope 跳转转为脱敏协议错误并阻断再次登录；gzip/deflate 响应仅解码一次。回归同时确认明确数字拒绝仍记 failed、控制 POST 不重发、解压后 1 MiB 上限与不携带原始请求的边界保持。

加密黄金值是在固定上游中**只提取纯函数**后用合成密码、合成 key/device/nonce 生成的；不 import HA 包，不实例化或执行上游网络客户端。没有使用上游的签名秘密或捕获身份。协议测试把默认 AsyncHTTPTransport 改为直接失败，所有厂商请求只能经过 MockTransport；既有本地 MCP 网络测试只访问 loopback。

新增协议实现仍未获真实账号或车辆验证。官方 H5 的人工滑块/短信辅助流程已研究，尚未移植；当前风险挑战以固定错误码阻断。详细源码证据见 [SOURCE_RESEARCH.md](SOURCE_RESEARCH.md)。
