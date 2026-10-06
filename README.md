# lixiang-mcp

独立 Python 理想汽车业务服务 + Streamable HTTP MCP，可在 NAS 容器中运行，不需要安装 Home Assistant。**默认模拟后端，可直接部署联调；另有基于固定上游源码实现的实验性 HTTP 协议适配器，尚未进行真实登录或实车验证。** 模拟返回标记 `simulated: true`，真实模式标记 `false`；不要把模拟测试通过当作实车协议已验证。

参考 [C3H3-AI/ha-lixiang v1.3.2](https://github.com/C3H3-AI/ha-lixiang/tree/7e9726bb7f78c5de376c88b271ac08fac8e2b997)，已研究登录/续期/签名/车辆选择/VSS/控制全链，并独立实现无默认秘密的协议层。详见 [源码证据与拆分决策](docs/SOURCE_RESEARCH.md) 及 [上游许可](docs/UPSTREAM.md)。

本地接手请先读 [本地开发与只读验证交接](docs/LOCAL_HANDOFF.md)：包含精确开发分支/代码基线、依赖安装、mock/MCP/Docker 自检、真实材料清单与不打印秘密的配置步骤。代码仍在 `feat/standalone-mcp-v1` 和 [Draft PR #1](https://github.com/skyjt/lixiang-mcp/pull/1)，main 尚未合并；真实账号仅由本人以后在本地配置。

## 能做什么

| MCP 工具 | 功能 | 服务端权限 |
| --- | --- | --- |
| `list_vehicles` | 获准且属于绑定账号的车辆列表 | `vehicle:read` |
| `get_vehicle_capabilities` | 当前配置、授权和车型共同决定的有效能力 | `vehicle:read` |
| `get_connection_status` | 车辆连接信号 | `vehicle:read` |
| `get_vehicle_state` | 电量、续航、四轮胎压、四门/窗、空调状态，**不含位置** | `vehicle:read` |
| `get_charging_status` | 充电、插枪、功率及剩余时间，只读 | `vehicle:read` |
| `get_vehicle_location` | 单独授权的位置读取；模拟坐标固定为虚构 `(0,0)`；真实模式单独请求位置 | `vehicle:location` |
| `set_climate` | 有限前排空调开关/整数温度，异步提交 | `vehicle:climate` + 本地 `enable_control` |
| `get_operation` | 查询本人操作及阶段历史 | `vehicle:climate` + 当前车辆归属 |

默认只读、无位置权限、禁用控制。只提供前排空调开关及 16–30°C 整数温度；开启必须明确温度，关闭不得带温度。模拟 SIM-L6/SIM-L7 的能力表不是实车车型兼容声明。未知车型不可控。没有任意 API/命令入口，不暴露解锁、授权驾驶、尾门/车窗开闭、远程拍照、充电控制或宠物模式。

## 本地快速运行

需要 Linux / NAS Linux、Python 3.12 和 `uv==0.12.19`。版本及完整传递依赖/哈希固定在 `pyproject.toml`、`uv.lock`。

```bash
uv sync --frozen
python scripts/create_demo_config.py
uv run lixiang-mcp
```

另一个终端：

```bash
uv run python scripts/smoke_client.py
```

默认地址 `http://127.0.0.1:8000/mcp`，健康检查 `/healthz`。脚本生成随机**模拟服务后端凭据**，写入忽略的 `runtime/demo-token`，服务端只保存 SHA-256；它不是理想账号 token。脚本不会打印凭据或覆盖已有配置。`auth.example.json` 是不可直接启动的占位模板。

若环境主目录只读，可设置 `UV_CACHE_DIR=/tmp/lixiang-uv-cache`。`LIXIANG_CONFIG` 可指向自己的 JSON 配置文件。缺少配置、无效配置、真实模式缺少私密协议配置、重复凭据或另一个进程持有同一数据库时均拒绝启动。

## Docker / NAS

```bash
python scripts/create_demo_config.py   # 已生成则跳过
cp config.docker.example.json config.docker.local.json
# Compose 的 file secret 是只读绑定文件。允许容器 uid 10001 读取摘要授权文件；
# runtime 目录仍保持 0700，原始 demo-token 文件保持 0600。
chmod 644 runtime/auth.json config.docker.local.json
docker compose up -d --build --wait
uv run python scripts/smoke_client.py
```

默认端口只绑定 NAS 本机，镜像非 root、只读根目录；SQLite 操作记录使用持久卷，只有一个进程/副本。**不要在真实使用中删除该卷或复制成两个运行中的实例**，幂等历史会丢失或分叉。NAS、反代、跨境网络及停止/更新步骤见 [部署与 MCPHub 接入](docs/DEPLOYMENT.md)。本仓库不自动安装 NAS 或修改网关。

## 模拟空调联调

仅在本地配置把 `enable_control` 设为 `true`，并在 `runtime/auth.json` 中为对应用户加入 `vehicle:climate`；位置则单独加入 `vehicle:location`。重启生效。这是运维端授权，不接受模型传 `confirmed=true` 或自报用户 ID。容器读取 `config.docker.local.json`，本机进程读取 `config.local.json`。

`set_climate` 工具参数示例（全部为模拟 ID）：

```json
{
  "command": {
    "vehicle_id": "demo-l6",
    "enabled": true,
    "temperature_c": 23,
    "idempotency_key": "example-request-0001"
  }
}
```

返回 `operation_id` 和 `phase: submitted` 后用 `get_operation` 查询。`submitted` 表示本地持久化接收，**不表示车辆云端已经接收**。同用户同幂等键同参数返回同一操作，换参数会报 `idempotency_conflict`。每次业务调用重新验证车辆归属与权限；操作不跨用户可见。

阶段为 `submitted → running → cloud_completed → vehicle_confirmed`，也可能 `failed` 或 `unknown`。`cloud_completed` 仅代表云端结果成功，必须读取下发之后的非陈旧车辆信号匹配才确认。提交/查询/确认超时都不重发命令；`unknown` 会拦截该车后续新操作及已排队操作。重启把未结束操作记为 `unknown`，不自动恢复执行。详见 [架构与可靠性](docs/ARCHITECTURE.md)。

管理员可通过 `mock_fault` 选择 `none`、`timeout`、`reject`、`unconfirmed`、`stale` 进行演练；MCP 工具不能修改这些配置。业务拒绝以 `{"error":{"code":"..."}}` 返回；协议参数错误由 MCP 返回 `isError: true`。

## 实验性协议适配器

`cloud/` 已实现 PKCE/PAKE、cookie 登录会话、主 token refresh、scope 缓存、外部密钥 HMAC、SAOS 列表、受限 VSS 和空调双 token 请求。完整流程用模拟 HTTP 契约及合成加密向量验证，也通过 MCP 路由测试。尚未使用真实材料联网，默认配置不会创建真实连接。

真实模式必须另行提供私密 `vehicle_secrets_file`，没有上游硬编码秘密或自动凭据发现。短信/验证码不支持，未知车型不能控制；真实控制还要求单独的 `allow_real_control`、精确车型能力和用户 grant。参数和剩余未知见 [协议适配与安全配置](docs/PROTOCOL_ADAPTER.md)。

## 检查

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
python scripts/check_public_tree.py
docker compose config --quiet
```

CI 同时构建容器、运行官方 MCP 客户端冒烟和 Gitleaks 秘密扫描。验证记录见 [验证说明](docs/VALIDATION.md)，边界及尚未实现事项见 [SECURITY.md](SECURITY.md)。

## 许可

本项目 MIT。保留上游原文 [MIT 许可](licenses/ha-lixiang-MIT.txt)；修改来源与不纳入的资源见 [UPSTREAM.md](docs/UPSTREAM.md)。没有复制上游签名秘密、设备身份、账号材料、车型资源包、图片或用户数据。
