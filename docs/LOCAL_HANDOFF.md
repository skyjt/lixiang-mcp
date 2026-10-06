# 本地开发与只读验证交接

先在本机跑通模拟模式，再由本人在本地配置真实材料。仓库中的协议代码已实现，真实登录和实车兼容性尚未验证；这不是已经接通理想账号的交付。开发环境没有接触真实账号，也没有部署 NAS 或 MCPHub。

## 1. 获取正确分支与代码基线

- 仓库：`https://github.com/skyjt/lixiang-mcp`
- 开发分支：`feat/standalone-mcp-v1`
- [Draft PR #1](https://github.com/skyjt/lixiang-mcp/pull/1)，保持未合并；**main 仍是初始仓库，不能用 main 运行本说明。**
- 协议适配与四项复核修复的代码基线：`af9328a663005cacbb33d96e94b9c241b4c00e9a`。本交接文档随后单独提交；最终交付 SHA 见 PR head 和交付消息。拉取时记录实际 HEAD，后续提交可能更新文档或代码。

```bash
git clone --branch feat/standalone-mcp-v1 --single-branch https://github.com/skyjt/lixiang-mcp.git
cd lixiang-mcp
git branch --show-current
git rev-parse HEAD
git merge-base --is-ancestor af9328a663005cacbb33d96e94b9c241b4c00e9a HEAD
```

最后一条退出码为 0 表示已包含该复核修复。已有工作目录时，先保存本地修改，再 `git fetch origin`、切换开发分支并 `git pull --ff-only`；不要为更新代码覆盖已有私密配置或操作数据库。需要严格复现基线时，可在另一个干净 checkout 使用 `git switch --detach af9328a663005cacbb33d96e94b9c241b4c00e9a`；该基线不含本交接文档。

## 2. 安装依赖与运行 mock

已验证 Linux amd64、Python 3.12.14。原生 Windows 请使用 Linux/WSL2；其他架构及 Docker Desktop 尚需自行验证。需要 Python 3.12（含 venv/pip）、Git；容器方式另需 Docker Compose v2。项目使用 `fcntl` 单进程锁，不支持多 worker/多副本。

已有 `uv==0.12.19` 可跳过工具安装。否则把 uv 装进独立工具环境，避免修改系统 Python：

```bash
python3.12 -m venv "$HOME/.local/share/lixiang-tools"
"$HOME/.local/share/lixiang-tools/bin/python" -m pip install 'uv==0.12.19'
export PATH="$HOME/.local/share/lixiang-tools/bin:$PATH"
uv --version
uv sync --frozen --python 3.12
python3.12 scripts/create_demo_config.py
uv run lixiang-mcp
```

生成器只创建 mock 的随机后端凭据、授权映射和配置，不打印 token，也不覆盖已有文件。所有命令在仓库根目录执行。没有理想账号也能完成以下所有 mock 检查。

另一个终端（同样确保 uv 在 PATH）：

```bash
uv run python scripts/smoke_client.py
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
python3.12 scripts/check_public_tree.py
```

预期：smoke 输出 `PASS: initialize, 8 tools, 3 mock vehicles, sampled state`；当前基线 120 项测试通过。smoke 使用官方 MCP Python SDK 完成 initialize、tools/list、tools/call，不是只检查 HTTP 200。`/healthz` 仅证明服务已构造，不能证明账号或车辆在线。已知 SDK lifespan 前向引用警告见 [验证记录](VALIDATION.md)。结束本机服务用 Ctrl-C。

`scripts/smoke_client.py` **只适用于 mock**：它固定检查三辆模拟车和 `demo-l6`，不要用它验证真实账号。默认后端凭据仅有 `vehicle:read`，模拟控制和位置也需另外本地授权。

## 3. Docker mock 演练

先停止占用 8000 端口的本机服务；若上一步已生成配置，不要再次运行生成器。

```bash
cp config.docker.example.json config.docker.local.json
chmod 700 runtime
chmod 600 runtime/demo-token
chmod 644 runtime/auth.json config.docker.local.json
docker compose config --quiet
docker compose up -d --build --wait
uv run python scripts/smoke_client.py
docker compose stop
```

`runtime/auth.json` 只含摘要和授权映射；0644 配合 0700 父目录让容器 uid 10001 读取绑定文件，不能把这个权限用于车辆秘密。默认映射为 `127.0.0.1:8000`。SQLite 卷保留，停止不会删除历史；不要用 `down -v` 更新服务。代理 CA、NAS 和 MCPHub 契约见 [部署说明](DEPLOYMENT.md)。上面的 `cp` 适用于首次演练；已有容器配置应先备份再修改。

## 4. 真实材料准备：只在本人本地完成

**手机号和密码并不足以完成配置。** 当前还需要合法持有且相互匹配的应用参数、设备身份、签名材料和车辆绑定。项目不提供获取这些材料、建立设备信任或绕过验证的方法，也不含可直接使用的第三方默认秘密。材料不齐时继续使用 mock；不要从公开项目复制硬编码密钥、借用其他设备身份或猜参数。

| 模板字段 | 需要本人提供的内容与用途 |
| --- | --- |
| `profile.client_id` / `redirect_uri` | 对应应用的客户端标识和登记的精确回调；回调不能含 query/fragment/用户名，不会由本服务自动创建 |
| `login_audience` / `login_scope` | 主账号密码登录所用受众和权限字符串 |
| `vehicles_audience` / `vss_audience` | 车辆列表与只读信号接口各自的 token 受众 |
| `mesh_audience` / `vat_audience` | 控车接口和逐车权限 token 的受众；只读检查不会申请控车 scope，但完整 profile 结构仍要求这两个字段 |
| `login_app_version` / `sdk_version` / `sign_app_version` | 登录设备登记、IDaaS 和签名所用版本；不是本项目版本号 |
| `login_user_agent` / `api_user_agent` | 登录链和车辆 API 各自的客户端标识，不能假定相同 |
| `accounts[].account_id` | 自己定义的无敏感信息的本地账号别名，例如 `owner-account`；不是手机号 |
| `phone` / `password` | 本人账号；可用带国家码的号码。密码由登录挑战派生 proof，不写入日志 |
| `device_id` / `key_id` / `hac_key_hex` / `app_token` | 配套的私密设备/签名材料；`hac_key_hex` 必须是原始 32 字节密钥的 64 位 hex，不接受随意 base64/ASCII 猜测 |
| `vehicles[].vehicle_id` / `account_id` / `vin` | 本地不透明车辆别名、所属账号别名、真实 VIN 的私密绑定；同一 VIN 只能出现一次 |
| `label` / `model_label` | 展示用的本地文本，避免写手机号、VIN 等个人信息 |
| `model_id` | 精确云端车型标识，不能按 L6/L7 名称猜。尚未核验时可临时写 `UNVERIFIED` 做只读探索，预期 `model_known=false`，能力开关保持 false |
| `climate_supported` / `location_supported` / `allow_real_control` | 初始全部为 false；它们是管理员能力/控制开关，不是待模型填写的确认参数 |

主 access token、refresh token、scope token 和 cookie 由已实现的登录链生成，只存进程内存；不需要手填，也不支持导入既有 HA/浏览器会话。网关后端 bearer 是另一个独立随机凭据，下节生成；它和 `app_token`、OAuth token 都不能互换。

在仓库根目录准备私密模板（已有文件时不要覆盖）：

```bash
umask 077
mkdir -p secrets
chmod 700 secrets
test ! -e secrets/vehicle-protocol.json && install -m 600 vehicle-protocol.example.json secrets/vehicle-protocol.json
vim -Nu NONE -n -i NONE -c 'set nobackup nowritebackup' -- secrets/vehicle-protocol.json
git check-ignore secrets/vehicle-protocol.json runtime/auth-readonly.json runtime/backend-readonly-token
git status --short
```

也可用可信本地编辑器，关闭云同步、插件上传、自动备份和交换文件。文件须由运行用户独占读取，保持 0600/0400、非符号链接；限制在读取内容前检查。不要把密码/token 写入 shell 参数、环境导出、curl `-H` 文本、终端粘贴的 heredoc、聊天或 PR。不要 `cat` 私密配置、打印 Pydantic 校验异常、开启协议 DEBUG 或 `git add -f` 绕过忽略。`.gitignore` 与 `.dockerignore` 是防误操作措施，不替代本人检查暂存区；发现材料被提交应先撤销/轮换，单独删文件不能清除历史泄露。

## 5. 离线校验并生成本机只读接入配置

首次只配置一个账号。以下代码从私密文件读取材料，只做结构/权限检查，**不会创建 HTTP 客户端或登录**。它生成专用后端 bearer 和对应摘要，只授予 `vehicle:read`，使用独立数据库与 8001 端口；命令中没有真实材料。

```bash
uv run python - <<'PY'
import hashlib
import json
import os
import secrets
from pathlib import Path
from lixiang_mcp.cloud.config import load_cloud_config

os.umask(0o077)
try:
    cfg = load_cloud_config(Path('secrets/vehicle-protocol.json'))
    if len(cfg.accounts) != 1 or cfg.allow_real_control:
        raise ValueError()
    if any(v.climate_supported or v.location_supported for v in cfg.vehicles):
        raise ValueError()
except Exception:
    raise SystemExit('离线检查未通过：核对字段、单账号、关闭能力及文件权限；不要打印原始异常') from None

runtime = Path('runtime')
runtime.mkdir(mode=0o700, exist_ok=True)
runtime.chmod(0o700)
paths = [runtime / 'backend-readonly-token', runtime / 'auth-readonly.json', runtime / 'config-readonly.json']
if any(p.exists() or p.is_symlink() for p in paths):
    raise SystemExit('已有只读配置，保留并人工检查，不覆盖凭据或数据库')
token = secrets.token_urlsafe(32)
auth = {'credentials': [{'token_sha256': hashlib.sha256(token.encode()).hexdigest(), 'principal': {
    'subject': 'local-owner', 'account_id': cfg.accounts[0].account_id,
    'vehicle_ids': [v.vehicle_id for v in cfg.vehicles], 'scopes': ['vehicle:read']}}]}
settings = {'backend': 'lixiang', 'vehicle_secrets_file': 'secrets/vehicle-protocol.json',
    'auth_file': str(paths[1]), 'database': 'runtime/readonly-operations.sqlite',
    'enable_control': False, 'command_timeout': 60, 'stale_after_seconds': 120,
    'allowed_hosts': ['127.0.0.1', 'localhost'], 'allowed_origins': [],
    'host': '127.0.0.1', 'port': 8001}
for path, content in zip(paths, [token, json.dumps(auth, indent=2), json.dumps(settings, indent=2)], strict=True):
    with path.open('x') as stream:
        stream.write(content + '\n')
print('PASS: 离线配置已校验，独立只读凭据和配置已生成；没有联网')
PY
```

如果失败，请本地对照字段表和 [协议配置契约](PROTOCOL_ADAPTER.md) 检查；模板中的 `CONFIGURE_*` 不是可用默认值。离线通过只证明文件格式可接受，不证明材料有效。首次建议本机 Python 运行，避免同时排查容器 secret 属主和真实协议。

## 6. 本人决定开始只读网络验证

从这一步起，工具读取会真正访问理想服务，可能进行设备登记和密码挑战；**只读车辆接口不等于完全没有账号侧变更**。不要在无人监督的循环、自动化任务或外部模型中首次尝试。发现短信、验证码、未知跳转或风险挑战即停止，当前实现不能完成这些交互。

```bash
LIXIANG_CONFIG=runtime/config-readonly.json uv run lixiang-mcp
```

该命令只指定无秘密内容的文件路径。服务启动本身不验证登录；第一次车辆读取才触发账号流程。保持本机回环访问，先不接 MCPHub。

另一个终端可使用下列官方 SDK 只读检查。它只从文件读 bearer，只连接固定 loopback 地址，不调用控制或位置，也不打印车辆原始状态和身份信息：

```bash
uv run python - <<'PY'
import asyncio
import re
from pathlib import Path
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def check():
    token = Path('runtime/backend-readonly-token').read_text().strip()
    async with httpx.AsyncClient(headers={'Authorization': f'Bearer {token}'}, trust_env=False) as client:
        async with streamable_http_client('http://127.0.0.1:8001/mcp', http_client=client) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                async def call(name, arguments=None):
                    result = await session.call_tool(name, arguments or {})
                    body = result.structuredContent
                    if isinstance(body, dict) and isinstance(body.get('error'), dict):
                        code = body['error'].get('code')
                        if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,80}', code):
                            print('只读错误码:', code)
                        raise RuntimeError('readonly_call_failed')
                    if result.isError or not isinstance(body, dict) or 'error' in body:
                        raise RuntimeError('readonly_call_failed')
                    return body
                vehicles = (await call('list_vehicles'))['vehicles']
                if not vehicles:
                    raise RuntimeError('no_authorized_vehicles')
                for vehicle in vehicles:
                    if vehicle['simulated'] is not False:
                        raise RuntimeError('unexpected_backend')
                    arguments = {'vehicle_id': vehicle['vehicle_id']}
                    caps = await call('get_vehicle_capabilities', arguments)
                    if caps['climate'] or caps['location']:
                        raise RuntimeError('expected_readonly_permissions')
                    print('model_known:', caps['model_known'])
                    for tool in ('get_connection_status', 'get_vehicle_state', 'get_charging_status'):
                        state = await call(tool, arguments)
                        signals = state['signals']
                        print(tool, 'signals:', len(signals), 'stale:', sum(s['stale'] for s in signals.values()))
                print('PASS: MCP 只读接口完成；tools:', len(tools.tools), 'vehicles:', len(vehicles))
try:
    asyncio.run(check())
except Exception:
    raise SystemExit('只读检查未通过；停止重复尝试，检查本地固定错误码及配置，勿上传原始响应') from None
PY
```

脚本完成不代表全部车辆信号可信；按顺序核对以下项目后再继续本地开发：

1. 确认 `backend=lixiang`、返回 `simulated=false`，两个控制开关和车型能力开关仍为 false，grant 只有 `vehicle:read`。
2. 列表只含本地明确绑定且当前账号获准的车辆。空列表先检查绑定/账号关系，不改成“默认第一辆车”或放宽角色过滤。
3. `model_known` 未知时保持未知。比对本人官方 App 的电量、续航、胎压、门窗和充电状态；缺字段、异常单位、无时区/过旧采样应记录为待核验，不能补零或把读取时间当采样时间。
4. 普通状态不请求位置。首次验收不调用位置；以后需要位置时单独审阅 `location_supported`、车型和 `vehicle:location` grant，并限制本地结果留存。
5. 核对权限与错误策略仍生效。控车拒绝测试用 mock/自动化回归完成，不拿真实车试开关是否可靠。
6. 遇到 `login_challenge_required`、`login_state_or_code_invalid`、`invalid_auth_redirect`、`invalid_upstream_redirect`、`scope_not_granted` 或 `account_requires_operator_attention`，停下人工核对。不要用循环重启清除阻断；缺少挑战支持应作为待实现项。
7. 只在本地比对和保存必要结果；反馈公开 issue 时使用版本、固定错误码及重新构造的合成 fixture，不上传原始日志/响应/数据库/配置。

确认本机只读流程后，才另行处理 NAS 或网关。实验性容器模板是 `config.lixiang.example.json` 加 `compose.lixiang.example.yaml`；必须配套更新后端账号授权，使用独立数据库，并让 uid 10001 对车辆秘密文件具有**私有**读取权限。不要直接把宿主机 0600 文件改为 0644 来排障；宿主 UID/ACL/secret 管理器映射没有验证前，继续使用本机模式。完整网关边界见 [部署说明](DEPLOYMENT.md)。

## 7. 已实现、未支持及操作结果语义

`cloud/` 是实际 HTTP 协议代码，PKCE/PAKE、cookie 登录、主 token refresh、scope 交换、签名、SAOS/VSS、受限空调和结果查询已用同一实现的 MockTransport 契约测试覆盖。第三方材料模板故意没有值；它不是尚未编写的登录函数。旧 `session.py` 的 `Unconfigured*` 接口保留兼容，实际 CloudBackend 不调用它们。源码证据、配置限制见 [SOURCE_RESEARCH.md](SOURCE_RESEARCH.md) 和 [PROTOCOL_ADAPTER.md](PROTOCOL_ADAPTER.md)。

当前不支持：短信/验证码完成、可信设备建立、材料自动获取、cookie/refresh 持久化、完整车型数据库、唤醒、解锁、授权驾驶、尾门/窗控制、远程拍照、充电控制、宠物模式、任意命令、unknown 人工恢复管理 API，以及多副本。真实账号/车型、最小 scope、时区枚举、NAS ARM64 和 MCPHub 均未实测。

真实空调需要服务端 `enable_control`、私密配置 `allow_real_control`、精确车型且 `climate_supported`、当前 owned 关系与明确非过户接收标志、用户 `vehicle:climate` grant 同时通过。首次只读交接不启用这些条件；模型不能靠 `confirmed=true` 放行。

后续开发若在获准环境测试写入：必须显式 vehicle_id，重试同一请求复用原 idempotency_key。`submitted` 仅本地接收，`running` 处理中，`cloud_completed` 仅云端报告完成，`vehicle_confirmed` 才表示新鲜车辆采样匹配。确定拒绝是 `failed`；超时、矛盾结果、提交后归属/确认异常是 `unknown`。未知命令可能已经执行或仍在云端 900 秒有效期内；禁止更换键、删库、重启后重发。该车后续写入会被屏障阻止，首版没有清除未知状态的工具，应停止控制并在本人官方 App/实车侧人工核对，保留历史待后续受审阅的恢复流程。
