# 本地开发与只读验证交接

代码在 `feat/standalone-mcp-v1`，对应 [Draft PR #1](https://github.com/skyjt/lixiang-mcp/pull/1)，**main 仍未合并**。请拉取该分支并记录 `git rev-parse HEAD`；最终交付 SHA 以 PR head/交付消息为准。早期 `af9328a` 是协议与四项修复的基线，不包含本轮本地登录向导，不要切回它执行新步骤。

```bash
git clone --branch feat/standalone-mcp-v1 --single-branch https://github.com/skyjt/lixiang-mcp.git
cd lixiang-mcp
git branch --show-current
git rev-parse HEAD
```

已有目录时先保存修改，`git fetch origin`、切换开发分支并 `git pull --ff-only`。不要覆盖私密文件或操作数据库。所有命令从仓库根目录执行。

## 1. 安装并跑 mock

已验证 Linux amd64 / Python 3.12.14。需要 Python 3.12（含 venv/pip）、Git；Windows 使用 WSL2，其他平台/架构待验证。已有 `uv==0.12.19` 可跳过工具环境安装：

```bash
python3.12 -m venv "$HOME/.local/share/lixiang-tools"
"$HOME/.local/share/lixiang-tools/bin/python" -m pip install 'uv==0.12.19'
export PATH="$HOME/.local/share/lixiang-tools/bin:$PATH"
uv sync --frozen --python 3.12
python3.12 scripts/create_demo_config.py
uv run lixiang-mcp
```

生成器只创建随机 mock 后端凭据和只读配置，不打印 token、不覆盖已有文件。另开终端：

```bash
uv run python scripts/smoke_client.py
uv run python scripts/readonly_client.py --config config.local.json --token-file runtime/demo-token
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
python3.12 scripts/check_public_tree.py
```

smoke 预期输出 `PASS: initialize, 8 tools, 3 mock vehicles, sampled state`，使用官方 SDK 的 initialize/tools/list/tools/call。readonly 客户端仅访问本机，只读连接/状态/充电，并输出信号数量/陈旧数量，不打印原始数据或凭据。`/healthz` 只代表服务可用，不代表账号/车辆在线。用 Ctrl-C 停止本机服务。

## 2. Docker mock

需要 Docker Compose v2。先停止占用 8000 端口的本机服务；已经生成 demo 配置时不要重复运行生成器。首次创建容器配置：

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

auth 文件只有后端凭据摘要和授权映射，0644 配合 0700 父目录供容器 uid 10001 读取；原始 token/车辆秘密保持 0600。不要用 `down -v` 更新实际服务。NAS、反代、代理 CA 和 MCPHub 契约见 [部署说明](DEPLOYMENT.md)。

## 3. 本人本地账号接入

```bash
uv run lixiang-setup
```

浏览器打开独立的本机页面，普通输入只有手机号和密码。内置版本化公开 profile，设备自动生成/复用；必要时打开携带相同设备 ID 的官方 H5 页面，完成验证后返回本地继续。详细步骤、取消/重启、签名材料边界和设备切换见 [首次接入指南](ONBOARDING.md)。

没有真实账号也能完成前两节；本次开发的所有验证都是合成数据。普通用户不需要手填完整应用配置或 VIN。密码登录没有生成 HMAC 签名材料的上游实现，缺少本人合法材料时，向导明确停在该步骤；不复制作者默认密钥，不伪装车辆可用。

导入本人签名材料并完成必要的设备重新验证后，在页面选择车辆。向导显示生成的 `server.json` 绝对路径；保持默认只读。停止向导，在自己的终端执行（把示意路径换成页面显示的路径）：

```bash
LIXIANG_CONFIG=/你的私密目录/export-对应目录/server.json uv run lixiang-mcp
```

另开终端，使用同一个实际配置路径：

```bash
uv run python scripts/readonly_client.py --config /你的私密目录/export-对应目录/server.json
```

客户端从该目录 `backend-token` 私密文件读取后端凭据，只连接 `127.0.0.1`。配置和 token 均不得上传、打印或提交。mock 的 `smoke_client.py` 固定检查三辆模拟车，不用于真实配置。

## 4. 只读逐步验收

1. 先确认配置为 lixiang、返回 simulated=false，两个控制开关/车型能力开关为 false，grant 只有 vehicle:read。本次云开发没有启用真实模式。
2. 列表只包含显式所选且当前账号获准的车辆；空列表先检查账号与关系，不能默认选第一辆。
3. 用本人官方 App 对照电量、续航、胎压、门窗和充电。检查 sampled_at/stale/单位，缺失和陈旧值不能补零或当实时值。
4. 初次不授予位置权限，不做真实控制试验；授权拒绝及控车故障用 mock 和回归测试验证。
5. 出现挑战、无效跳转、签名错误、账号失效或 `account_requires_operator_attention`，停止盲试，通过本机向导明确继续/改正。模型不能接管登录。
6. 公开反馈仅给版本、固定错误码和重新构造的 fixture；不提供原始响应/日志/手机号/VIN/设备 ID/坐标/token/私密目录。
7. 本机只读通过后，再处理 NAS、MCPHub 用户映射和网络；目前没有用户环境部署实测。

## 5. 开发回归和限制

可用已有 Chromium 跑完整合成浏览器流程；测试脚本不会下载浏览器，也不会打开真实官网：

```bash
uv sync --frozen --group browser
uv run --group browser python scripts/qa_onboarding_browser.py --browser-executable /本机已有的/chromium
```

测试覆盖本地登录、官网验证后返回、签名设备不一致、选车和只读导出。pytest 另覆盖重复点击、取消、中断恢复、失效、授权、日志和四项已修复问题。验证记录见 [VALIDATION.md](VALIDATION.md)。

尚未验证真实账号、官方 H5 信任、材料兼容、车型/单位/时间格式、NAS ARM64 和 MCPHub。没有个人签名材料自动生成/提取、完整车型数据库、未知操作人工恢复 API 或多副本。没有唤醒、解锁、授权驾驶、尾门/窗控制、远程拍照、充电控制或宠物模式。

已实现的空调仍需可信运维开关、用户 grant、车型和当前归属同时通过，向导不会启用这些条件。submitted 仅本地接收；cloud_completed 仅云端完成；vehicle_confirmed 表示新的车辆采样匹配。超时、矛盾、无效结果码及提交后确认失败都保留 unknown 屏障。未知命令可能已执行或仍在云端有效期内，禁止换键、删库、重启后重发；原幂等历史必须保留。
