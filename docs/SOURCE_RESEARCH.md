# 指定上游的源码研究与适配决策

基线：`C3H3-AI/ha-lixiang` tag `v1.3.2` = `7e9726bb7f78c5de376c88b271ac08fac8e2b997`。本文件基于固定 checkout 的代码与调用关系，不把上游注释中的“实测”当作本项目验证。所有新测试只使用合成响应；不运行上游登录或控车程序。

## 模块和实际调用链

```text
HA config_flow / auth_web → ConfigEntry 中的账号、设备、签名配置
 __init__.async_setup_entry → LiApiClient + LiCarClient + coordinator
  LiApiClient._login → LixiangDirectLogin.login → SSO cookie + 主 token
  _get_scoped → _exchange(cookie /api/auth) → VSS / SAOS / MESH / VAT
  get_vehicles / get_vss_state / send_command_raw / get_command_result
  _signed_call → x-chj HMAC headers + scope bearer → api-app
 coordinator → executor 同步调用 + 分级轮询/缓存 → HA entities
 climate → gate → send_command（隐式 wakeup、send、poll）→ 乐观实体状态
```

| 文件（固定版本） | 证据位置与用途 | 适配决定 |
| --- | --- | --- |
| [pake_login.py](https://github.com/C3H3-AI/ha-lixiang/blob/7e9726bb7f78c5de376c88b271ac08fac8e2b997/custom_components/lixiang_auto/pake_login.py#L80) | 80–132 seed/proof；201–279 登录六步；282–293 refresh | 重写为异步、受限 HTTP 流程和纯加密函数；不带默认设备、密码或第三方参数 |
| [li_api.py](https://github.com/C3H3-AI/ha-lixiang/blob/7e9726bb7f78c5de376c88b271ac08fac8e2b997/custom_components/lixiang_auto/li_api.py#L345) | 345–449 cookie 会话和 scope 缓存；453–486 签名 | 这是主链路。拆为账号 AuthSession、Signer、窄 API；按账号及 scope/audience/VIN 隔离 |
| [auth.py](https://github.com/C3H3-AI/ha-lixiang/blob/7e9726bb7f78c5de376c88b271ac08fac8e2b997/custom_components/lixiang_auto/auth.py#L43) / client.py | curl Bearer-only 换 token、旧 commandKey 控车 | 不作为主适配：与 LiApiClient 的 cookie / 双 token / cmdKey 流程不一致；不复制默认设备身份 |
| [signer.py](https://github.com/C3H3-AI/ha-lixiang/blob/7e9726bb7f78c5de376c88b271ac08fac8e2b997/custom_components/lixiang_auto/signer.py#L78) | 78–129 MD5、11 行、末尾换行、HMAC-SHA256 | 可复用算法；密钥只接受外部原始 32 字节的明确编码，不保留 ASCII 猜测回退 |
| auth_web.py / config_flow.py / login_page.html | HA HTTP 视图、临时登录会话、官方 H5 辅助与密码轮询 | 研究但不移植；短信/滑块需单独可信交互设计，当前明确阻断 |
| identity.py | 121–170 按账号存设备 ID，HA executor 持久化 | 不读取用户既有身份文件，不生成“可信设备”或复制 bootstrap 身份；设备由外部配置明确提供 |
| [vehicle_role.py](https://github.com/C3H3-AI/ha-lixiang/blob/7e9726bb7f78c5de376c88b271ac08fac8e2b997/custom_components/lixiang_auto/vehicle_role.py#L108) | 108–167 owned / authorized / inviting / transferring；未知 role 默认 family | 拒绝未知 role、邀请、转移/接收中及不可用状态；配置别名 + 当前归属双重匹配。owner 缺少明确的非过户接收标志不放行写入。绝不选列表第一辆 |
| [signals.py](https://github.com/C3H3-AI/ha-lixiang/blob/7e9726bb7f78c5de376c88b271ac08fac8e2b997/custom_components/lixiang_auto/signals.py#L165) / rendering.py | 信号路径、单位、枚举解释 | 抽取本首版小型白名单，严格类型/范围；未知枚举/缺失采样不猜值；位置单独路径 |
| features.py / vehicle_ability.py | 能力来源优先级：APK 车型资源 → variableModel → 硬编码 → VSS 探测 | 不复制 APK 资源；VSS 有值不证明硬件存在。要求外部经审阅的精确 modelId 能力配置；默认未知、不可控 |
| [policy.py](https://github.com/C3H3-AI/ha-lixiang/blob/7e9726bb7f78c5de376c88b271ac08fac8e2b997/custom_components/lixiang_auto/policy.py#L117) / gate.py | 命令401重试一次；缺少 HA entry 时允许控制 | 不复用这些放行/重发语义；本项目写入不重发、失败关闭 |

## 登录、挑战和凭据生命周期

1. PKCE：随机 code_verifier → SHA256/S256 challenge；随机 state；向 `id.lixiang.com/api/auth` 提交 code 授权请求（上游接受 HTTP 200/300）。
2. 打开 `account.lixiang.com/login` 初始化页面会话；`/api/devices` 注册**明确提供**的设备 profile。设备登记参数只采纳固定版本证据，不猜另一个移动平台的参数。
3. `/api/idps` 提交 LI_USER/PASSWORD、用户提示和 seed。seed 是 SHA256(password) 的大整数模固定 128-bit 模数，补齐 32 hex。seed 是密码派生敏感值，不记录。
4. `t_login_use` 的 `option`/`kdf` 指定 bcrypt salt 头；盐来自 `salted`（hex 编码字符，跳过零字节）或 `seeded`（16 字节转 bcrypt 字母表）。bcrypt 输出再 SHA256 得到 Ed25519 signing seed；签名消息是 SHA256(snonce 后缀 hex + 随机 cnonce)。拒绝未知算法、畸形/超大挑战和过高 bcrypt cost，防止挑战放大计算。
5. `/api/login` 接受 200/300/302，读取 Location/JSON location 中授权码；`require=SMS_CODE` 表示额外验证，必须停止。上游 `config_flow.py:275–417` / `auth_web.py:114–171` 提供官方 H5 页面辅助：用户在浏览器完成滑块/短信，后台再用同一设备+密码检测信任；`async_step_sms` 转交浏览器步骤。它没有可直接抽取的服务端短信/验证码完成协议。本项目不复制 HA 登录页面会话、自动反复试密码或默认设备引导，不跳过挑战或自造接口。适配器检查 redirect 目标与 state，不跟随返回地址发送凭据；缺少 state 也拒绝，并列为需实测兼容项。
6. `/api/token` 用 code + verifier 换主 token 和 refresh_token。主 token refresh 轮换不等价于重建 SSO cookie。上游 `_sync_parent_domain_cookies` 为 H5 把 cookie 扩散到 `.lixiang.com`；NAS 不使用 H5，因此不扩大 cookie 域。

真正 VSS/控制链路的 scope exchange 是带会话 cookie 的 `/api/auth`（response_type=token），从 Location fragment 取 access_token。不能拿旧 `exchange_scope_token(main_bearer, ...)` 当成已验证替代品。缓存键必须包含 audience、完整 scope 和车辆，不采用单一 `vat` 名称混用多车。

上游 `_login` 优先使用 xdev，`_exchange` 却使用另一个 `_device_id`；适配器只保留一个一致设备 ID，用于 cookie、登记、IDaaS、交换及签名。设备/密钥是否匹配只可由后续安全配置和真实验证确认。

本项目账号对象独占 cookie jar；续期/登录/scope exchange 共用账号锁；使用响应 expires_in（并受上游保守 TTL 上界限制），不把未验签 JWT 的 claims 当授权事实。cookie 登录失效可在**读请求前**受限重建一次；不在控车 POST 后通过重登重发。风险挑战进入阻断态，等待外部人工处理，避免反复撞风控。

主 token/refresh_token 和 cookie 仅存于进程私有内存；首版不持久化到普通配置或 SQLite。后续 secret-store 保存轮换 token 是独立扩展，当前进程重启需要重新建立会话。不能声称已经实现 HA ConfigEntry 的持久化等价物。

## 签名和请求隔离

签名串为 env、appVersion、keyId、deviceId、method、Accept、Content-Language、base64(MD5(实际发送 body bytes))、Content-Type、timestamp、nonce，逐项换行且末尾也有换行。签名是 base64(HMAC-SHA256(原始密钥字节, 签名串))。path 不在该基线签名串中，因此 HTTP 层必须固定 host 和端点，禁止签名变成任意 URL 请求入口。

签名所需 key、key_id、设备 ID、app token，以及登录 client/audience/版本参数都由独立外部安全文件注入，不带上游默认值。API client 和账号登录 client 分离 cookie；不把 SSO cookie 或主 token发往 api-app。MCPHub bearer 永远不进入这些对象。禁用自动 redirects、环境代理继承和 HTTP 自动 retries，保留 TLS 验证、超时和响应大小上限。

## 车辆、VSS 与能力

`get_vehicles`（li_api 1060–1085）返回 SAOS basics 的原始列表，查询包括 owned/transferring/authorized/inviting。上游 `get_primary_vin`（1111–1122）优先第一辆 owned；本项目禁止这一默认，配置不透明 vehicle_id → 私密 VIN → account，返回时只显示本地别名和本地 model 标签。每次读写重新检查账号列表归属，敏感原始记录不回传模型。

VSS（906–973）请求 body 含 VIN 与 paths，返回 items/path/dp/value/tsFormat；无效 path 会使整批 HTTP 400。适配器只对明确 invalid_path 错误对固定只读白名单二分降级；不能因任意 400 拆分或扩展路径。不存在/无时区/未来/陈旧时间均如实标记；不复制上游截断到秒而丢失时区的做法。

`FOffStatus` 才是空调开关；`ExSpeedStatus` 在此版本被纠正为快冷快热，不作为风速或开关回退。窗户是百分比，应返回位置百分比和可靠的 open 布尔，而不是任意非零字符串判真。连接分 5G/hu-f/xcu；只根据已知且新鲜值归并，不能把缺失通道直接当离线。续航、充电功率、胎压等只采纳有明确路径/单位的字段；不为缺失值合成零。

## 空调提交与结果

- `climate.py` 250–289：唯一 commandKey 为 `remoteVehACSmartControl`，前排 `frtACSw`，`ON/OFF`、15 分钟倒计时字符串，温度是数字。参数范围取该版本 16–30°C 整数步进；模型能力仍需精确匹配。
- `li_api.py` 1132–1189：POST cmd/send；Authorization=MESH，body.token=VAT，body 含 VIN/cmdKey/cmdData/domain=xcu/jobExpire=900/expire=900/expireAt=now+900000。原代码申请12个VAT权限；本项目只申请空调 scope 与当前 VIN，其服务端兼容性未验证，不为“兼容”申请解锁等权限。
- 1201–1306：requestId 可在根或 data；无 receipt 属不确定接受，不能当“安全失败”后重发。预先获取必要 token 后只发送一次；HTTP401/5xx/响应丢失均不重发控车 POST。
- GET cmd-result/{requestId} 是只读，可受限刷新 token 后再查。requestId 必须验证格式且绑定原账号/车辆。禁止跨车查询或把任意 path 作为 receipt。
- **冲突证据**：上游对 pushState=5 的未知非零 rc 仍返回“成功”，且 pushState=7 与成功 rc 也当成功。现有实现把 ps=7 全当失败同样过度确定；修正为只有一致的 ps=5/已知成功码标记 cloud_completed，一致明确失败标记 failed，矛盾/缺字段保留 unknown。
- 不调用隐式 wakeup，不采用 HA 乐观状态。必须在云端成功后读取新的车辆状态确认；超时、矛盾或进程中断阻断该车后续写入，不让模型清除未知记录。

## HA 与许可边界、剩余未知

三个核心文件无 HA import，但包 `__init__`、coordinator、gate、climate 和实体类有 HA 类型/注册/存储/线程调度耦合。适配器以独立纯 Python 包重写协议，不 import 上游 HA package，不复制 HA gate 的失败放行行为。

MIT 原文保留在 `licenses/ha-lixiang-MIT.txt`，派生的 proof、signing、API、信号模块均注明来源。**MIT 不自动覆盖 APK/品牌/设备身份数据的再分发权**；不纳入车型 JSON、app_config、sub_token_data、图片或上游硬编码秘密。协议常量与小范围代码按源码证据重新组织；外部配置示例只有字段结构与不可运行占位。

未解决且不猜测：短信/验证码/其他风控挑战；真实 SSO 有效期与 refresh 恢复 cookie 能力；返回 redirect 是否总携带 state；最小 VAT scope 是否被服务端接受；role 的过户 receiver 未知实现；完整车型/年款能力数据库；真实 tsFormat 时区/信号枚举和空调结果冲突的权威语义；secret-store 轮换持久化；MCPHub 实际逐用户凭据和 NAS 实机。模拟 HTTP 测试能证明编码、隔离、状态机和失败策略，不能证明账号或车辆端接受。
