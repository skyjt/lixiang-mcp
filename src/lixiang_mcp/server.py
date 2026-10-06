from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import ASGIApp

from .auth import BackendAuth, principal
from .backend import Backend, MockBackend
from .cloud.backend import CloudBackend
from .cloud.config import load_cloud_config
from .cloud.transport import ProtocolError
from .config import AuthConfig, Settings
from .models import ClimateCommand, ServiceError, VehicleId
from .safe_logging import configure_logging
from .service import VehicleService
from .store import OperationStore


async def public_call(call: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
    try:
        value = await call()
        if isinstance(value, list):
            return {"vehicles": [v.model_dump(mode="json") for v in value]}
        return dict(value.model_dump(mode="json"))
    except (ServiceError, ProtocolError) as exc:
        return {"error": {"code": exc.code}}
    except Exception:
        return {"error": {"code": "backend_unavailable"}}


def create_app(settings: Settings, *, backend: Backend | None = None) -> ASGIApp:
    auth = AuthConfig.model_validate_json(settings.auth_file.read_text())
    if backend is None:
        if settings.backend == "lixiang":
            if settings.vehicle_secrets_file is None:
                raise ValueError("vehicle_secrets_file_required")
            backend = CloudBackend(
                load_cloud_config(settings.vehicle_secrets_file), ttl=settings.stale_after_seconds
            )
        else:
            backend = MockBackend(fault=settings.mock_fault, ttl=settings.stale_after_seconds)
    store = OperationStore(settings.database)
    try:
        service = VehicleService(
            backend,
            store,
            enable_control=settings.enable_control,
            command_timeout=settings.command_timeout,
        )
    except Exception:
        store.close()
        raise

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await service.close()

    mcp = FastMCP(
        "lixiang-mcp",
        instructions="检查 simulated 标记。未知结果不得重发控车命令。位置需独立权限。",
        stateless_http=True,
        json_response=True,
        # Host and Origin checks live in the outer middleware with explicit operator configuration.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    @mcp.tool(annotations=read)
    async def list_vehicles() -> dict[str, Any]:
        """列出当前后端凭据获准访问且属于绑定车辆账号的车辆。"""
        return await public_call(lambda: service.vehicles(principal()))

    @mcp.tool(annotations=read)
    async def get_vehicle_capabilities(vehicle_id: VehicleId) -> dict[str, Any]:
        """返回车型及当前授权下的有效能力；未知车型不可控。"""
        return await public_call(lambda: service.capabilities(principal(), vehicle_id))

    @mcp.tool(annotations=read)
    async def get_vehicle_state(vehicle_id: VehicleId) -> dict[str, Any]:
        """读取电量、续航、胎压、门窗及空调；每项含采样时间/陈旧标记，不含位置。"""
        return await public_call(lambda: service.state(principal(), vehicle_id))

    @mcp.tool(annotations=read)
    async def get_connection_status(vehicle_id: VehicleId) -> dict[str, Any]:
        """读取车辆连接信号；connected 不代表可唤醒或可执行命令。"""
        result = await public_call(lambda: service.state(principal(), vehicle_id))
        if "signals" in result:
            result["signals"] = {"connected": result["signals"]["connected"]}
        return result

    @mcp.tool(annotations=read)
    async def get_charging_status(vehicle_id: VehicleId) -> dict[str, Any]:
        """只读充电状态；不支持启停充电、限额或预约。"""
        result = await public_call(lambda: service.state(principal(), vehicle_id))
        if "signals" in result:
            result["signals"] = {
                k: v for k, v in result["signals"].items() if k.startswith("charg")
            }
        return result

    @mcp.tool(annotations=read)
    async def get_vehicle_location(vehicle_id: VehicleId) -> dict[str, Any]:
        """独立 vehicle:location 权限读取位置；模拟模式返回虚构 (0,0)。"""
        return await public_call(lambda: service.location(principal(), vehicle_id))

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    async def set_climate(command: ClimateCommand) -> dict[str, Any]:
        """提交有限空调操作，立即返回 operation_id。开启须指定16–30整数温度；关闭不传温度。

        相同请求重试必须复用 idempotency_key；超时/unknown 不得生成新键重新控车。
        服务端启用控制、车辆归属、车型和权限均通过后才接受，不接收 confirmed 参数。
        """
        return await public_call(lambda: service.submit(principal(), command))

    @mcp.tool(annotations=read)
    async def get_operation(operation_id: str) -> dict[str, Any]:
        """只读当前授权用户的操作状态；云端完成不等于车辆状态确认。"""
        return await public_call(lambda: service.operation(principal(), operation_id))

    try:
        mcp_app = mcp.streamable_http_app()
        app = Starlette(routes=[Mount("/", app=mcp_app)], lifespan=lifespan)
        return BackendAuth(app, auth, settings)
    except Exception:
        store.close()
        raise


def main() -> None:
    configure_logging()
    try:
        settings = Settings.from_env()
        app = create_app(settings)
    except Exception:
        raise SystemExit(
            "configuration_or_store_invalid; check local config and permissions"
        ) from None
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        proxy_headers=False,
        workers=1,
    )
