from pathlib import Path

import pytest
import pytest_asyncio

from lixiang_mcp.backend import MockBackend
from lixiang_mcp.models import Principal
from lixiang_mcp.service import VehicleService
from lixiang_mcp.store import OperationStore


@pytest.fixture
def actor():
    return Principal(
        subject="demo-user",
        account_id="demo-account",
        vehicle_ids={"demo-l6", "demo-l7", "demo-unknown"},
        scopes={"vehicle:read", "vehicle:climate", "vehicle:location"},
    )


@pytest_asyncio.fixture
async def service(tmp_path: Path):
    service = VehicleService(
        MockBackend(),
        OperationStore(tmp_path / "ops.sqlite"),
        enable_control=True,
        command_timeout=0.15,
    )
    yield service
    await service.close()
