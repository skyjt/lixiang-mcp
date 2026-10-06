# 上游证据与许可

研究基线：[C3H3-AI/ha-lixiang v1.3.2](https://github.com/C3H3-AI/ha-lixiang/tree/7e9726bb7f78c5de376c88b271ac08fac8e2b997)，commit `7e9726bb7f78c5de376c88b271ac08fac8e2b997`。开发时用 `git ls-remote ... refs/tags/v1.3.2` 及固定 checkout 核验一致；没有运行上游的登录/控车代码。

通过 AST 检查 `custom_components/lixiang_auto/li_api.py`、`pake_login.py`、`policy.py` 的 imports，三个文件均无 Home Assistant 导入。但 `custom_components/lixiang_auto/__init__.py` 直接导入 Home Assistant，因此不能直接导入其包来实现独立服务。

| 上游文件/函数 | 首版使用方式 | 重要差异 |
| --- | --- | --- |
| `li_api.py:get_vss_state` | `protocol.parse_vss` 参考 `items/path/dp/value/tsFormat` 结构 | 显式信号白名单、补全 missing 为 unknown/stale、保留时区；不截断时间字符串 |
| `li_api.py` pushState/resultCode 常量与 `_poll_result` | `protocol.cloud_result` 适配 5/7 和成功结果码 0/-15/-8 | 缺失或未知成功码保守失败；云端成功不直接当车辆确认 |
| `climate.py` `_send_ac_on` / `async_turn_off` | `protocol.climate_payload` 抽取前排开关、15 分钟定时、数值温度字段 | 不导入 HA、无乐观成功、温度严格验证、不开万能 command_key 参数 |
| `policy.py:run_with_retry` | 仅研究，不复制 | 上游命令 401 可重试一次；首版写入不重试，不记录上游异常文本 |
| `pake_login.py` / `li_api.py` 登录签名链 | 仅检查结构，保留独立 LoginProvider/SigningProvider 接口 | 不复制硬编码签名秘密、设备身份、账号/会话材料；没有可用真实登录实现 |
| `li_api.py:get_vehicles` | 仅研究 API 分层 | 上游宽列表含授权中等关系；未来真实适配器需过滤归属，不以返回 VIN 即获授权 |

本项目 `protocol.py` 是少量重新组织的派生代码，文件头注明原项目、版本和许可。上游 LICENSE 原文为 `MIT License`，版权行仅为 `Copyright (c) 2026`，未列姓名；已逐字保存在 [licenses/ha-lixiang-MIT.txt](../licenses/ha-lixiang-MIT.txt)。文件头的 contributors 是来源归属说明，未替换上游版权行。

未纳入上游的 secrets/const 签名材料、app_config/sub_token_data、车型 JSON 资源包、品牌图片、APK 逆向资源、测试账号数据或整套 HA 集成。无真实手机号、VIN、token、设备 ID 或车辆坐标进入本仓库。模拟位置使用虚构 `(0,0)`，车辆 ID 使用 `demo-*` / `other-*` 别名。

协议字段来自该基线的实现陈述，**不等同本项目实车验证**。尤其充电控制、宠物模式没有在本项目实现或声明支持。上游客户端也包含与本首版无关的高风险命令，均未暴露。

其他依赖由 `uv.lock` 固定并从包源安装；MCP 接入使用 [官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0) v1.26.0。授权边界参考 [MCP Security Best Practices](https://modelcontextprotocol.io/docs/2025-11-25/tutorials/security/security_best_practices)，明确不做 token passthrough。
