# 协议适配状态与安全配置契约

实现以 [固定版本源码研究](SOURCE_RESEARCH.md) 为依据。`cloud/` 包是实际 HTTP 协议实现，默认部署仍用 MockBackend。契约测试通过 `httpx.MockTransport` 驱动同一份登录、签名、车辆 API 和 CloudBackend 代码，并在 MCP 工具层跑通；不是在 cloud 模式中返回固定车辆数据。

**没有真实登录或实车验证。** 已实现流程不等于理想当前服务器接受所有参数；缺失/未知信息会报错，不生成假成功。当前任务不会配置或执行真实模式。

## 已实现及证据边界

| 流程 | 实现与测试 | 尚需验证/限制 |
| --- | --- | --- |
| PKCE/PAKE | seed、bcrypt salted/seeded 两支、Ed25519 proof、code/state 校验、code 换 token | 只覆盖上游密码链；短信/验证码/新挑战明确拒绝；state 缺失也拒绝 |
| 账号会话 | 每账号 cookie jar 和锁，主 token refresh 轮换，cookie 失效限定重登一次 | 内存会话，不持久化 refresh/cookie；重启重建；真实风控未知 |
| scope token | cookie /api/auth、Location fragment、按 audience+完整 scope 缓存、expires_in 保守上界 | 不使用未验签 JWT claims 授权；缺失响应 scope 只能依赖服务器按请求授权 |
| MESH/VAT | 同锁成对获取；若其中一次交换重建会话，重新获取整组 | 只请求空调 VAT 和 send/result MESH；最小 scope 服务端兼容性未实测 |
| x-chj 签名 | 实际 body bytes 的 MD5、11 字段及末尾换行、原始 key HMAC；上游合成向量对照 | app/device/key 一致性由外部材料保证；没有默认第三方秘密 |
| 车辆列表 | SAOS basics、指定账号、私密 VIN 到本地别名映射、当前归属过滤 | 无“选第一辆”；owner 和已知 family 只读，family 写入禁用；owner 缺少明确的非过户接收标志也只读；试驾/未知关系拒绝 |
| VSS | 小型固定路径白名单、invalid_path 二分只读降级、严格值/采样解析 | 真实单位/枚举/时间格式与车型待验证；无时区不猜时区 |
| 空调 | 固定前排开关/数值温度/倒计时，900 秒云端有效期，单次 POST、receipt 绑定原车原账号 | 两个运维开关及精确 modelId 能力确认才可用；不唤醒、不支持其它命令 |
| 操作确认 | 一致成功 → cloud_completed → 新鲜车辆采样；冲突/缺失终态 → unknown | 采样匹配不证明唯一因果；未知操作没有自动清除/重发入口 |

`signals.py` 明确保留充电原始 `charge_status_code`，只映射有源码证据的布尔状态；不把上游 raw 与 normalized 状态码混为一谈。CLTC 总续航只在电/油两项都存在时求和，缺失项不补零（纯电车型可能总续航为 unknown，纯电续航仍可读）。位置由专用路径读取，普通状态请求不会请求位置。

## 外部文件（仅未来经授权配置）

`vehicle-protocol.example.json` 是结构模板，**刻意不可运行**：不含有效账号、手机号、VIN、设备 ID、签名材料、第三方 client/audience 或 app 版本参数。操作者需要通过安全渠道获取有权使用的材料，把私密文件放在 Git 忽略的目录或 secret manager 挂载点。

- `profile`：client/redirect/login scope、各 audience、登录与签名版本、两个 User-Agent。固定服务域名/端点不开放给配置或 MCP 参数。
- `accounts`：账号别名和实际 phone/password/device_id/key_id/hac_key_hex/app_token。签名 key 必须明确是 32 字节的 64 hex；不做 base64/ASCII 猜测，不自动发现既有 HA、浏览器、环境变量或设备文件。
- `vehicles`：本地别名、账号别名、私密 VIN、经审阅的精确云端 modelId 和能力。车辆列表只返回本地 label/model_label，不回传原始 VIN 或云端任意字符串。不同账号不能把同一 VIN 配成两个 vehicle_id，避免绕过串行锁。
- 私密文件必须是非符号链接的普通文件，权限无 group/other 位（如 0600/0400），且运行 uid 可读；检查在读取内容之前执行。JSON 大小上限 128 KiB。标准 Pydantic repr 将秘密隐藏；不要主动 model_dump 真实秘密。
- `vehicle_secrets_file` 是唯一显式加载入口；不复制材料到 Docker build context。默认 Compose 不挂载它。可选覆盖文件 `compose.lixiang.example.yaml` 仅展示挂载契约；容器 uid 10001 需要该文件的私有读权限（宿主文件属主/安全 secret 管理器配置）。不能沿用 demo auth 摘要文件的 0644 权限到车辆秘密文件。

未来启用读操作时，管理员需把服务配置设为 `backend: lixiang` 并指向该文件；`config.lixiang.example.json` 展示容器路径。网关 `auth.json` 中 Principal.account_id / vehicle_ids 必须对应新绑定，不能沿用 demo-account/demo-l6。为真实模式使用独立数据库，后端类型或账号/VIN 别名绑定变化会触发数据库命名空间拒绝；不要删除操作记录绕过检查。

真实控制需要同时满足：`Settings.enable_control=true`、私密配置 `allow_real_control=true`、精确 modelId 匹配且 `climate_supported=true`、当前 owned 归属且存在明确为 false 的过户接收标志、后端 Principal 的 vehicle:climate grant。位置独立要求 scope、匹配车型和 `location_supported=true`。这些均是可信运维配置，模型参数无法开启。

## 失败与隐私

密码/额外挑战、未知 redirect/state、scope 不足会使该账号进入阻断状态；后续工具返回 `account_requires_operator_attention`，避免反复登录。需要操作者确认原因并重启/替换安全配置，MCP 不提供解除风控工具。Transport 异常和上游响应文本不会出现在公共错误中，仅返回固定错误码；日志白名单继续生效。

读取遇到 401 最多重新获取相关 scope 后重试一次。控车 POST 不因 401、5xx、网络错误或无 receipt 重发。云端状态矛盾不算成功/明确失败，转 unknown 并拦截该车的新写入。命令可能在本地超时之后继续在云端有效期内执行；不得据 unknown 认为车辆没有变化。

本版本不实现：短信/验证码的完成流程、设备注册以外的设备信任建立、第三方签名材料获取、持久化 cookie/refresh、车型资源数据库、充电/宠物模式、唤醒和任意命令、未知操作的人工恢复管理 API。所有真实账号/设备/网关/NAS 验证仍留待明确授权的后续阶段。
