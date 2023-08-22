import enum
import logging
from datetime import timedelta

import yarl
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    ConfigEntryNotReady,
    DataUpdateCoordinator,
)

from . import api
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class ApplianceKind(enum.StrEnum):
    ADORA_DISH = enum.auto()
    ADORA_WASH = enum.auto()

    @classmethod
    def from_model_description(cls, desc: str):
        desc = desc.lower()
        if "adorawash" in desc:
            return cls.ADORA_WASH
        if "adoradish" in desc:
            return cls.ADORA_DISH
        return None


StateCoordinator = DataUpdateCoordinator[api.AggState]
UpdateCoordinator = DataUpdateCoordinator[api.AggUpdateStatus]
UPDATE_COORD_IDLE_INTERVAL = timedelta(hours=6)
UPDATE_COORD_ACTIVE_INTERVAL = timedelta(seconds=5)

ConfigCoordinator = DataUpdateCoordinator[api.AggConfig]


class Shared:
    hass: HomeAssistant
    client: api.VZugApi

    state_coord: StateCoordinator
    update_coord: UpdateCoordinator
    config_coord: ConfigCoordinator

    unique_id_prefix: str
    appliance_kind: ApplianceKind | None
    device_info: DeviceInfo

    def __init__(
        self,
        hass: HomeAssistant,
        base_url: yarl.URL,
    ) -> None:
        self.hass = hass
        self.client = api.VZugApi(async_get_clientsession(hass), base_url)

        self.state_coord = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name="state",
            update_interval=timedelta(seconds=30),
            update_method=self._fetch_state,
        )
        self.update_coord = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name="update",
            update_interval=UPDATE_COORD_IDLE_INTERVAL,
            update_method=self._fetch_update,
        )
        self.config_coord = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name="config",
            update_interval=timedelta(minutes=5),
            update_method=self._fetch_config,
        )

        # the rest will be set on first refresh
        self.unique_id_prefix = ""
        self.appliance_kind = None
        self.device_info = DeviceInfo()
        self._first_refresh_done = False

    async def async_config_entry_first_refresh(self) -> None:
        await self.state_coord.async_config_entry_first_refresh()
        await self.update_coord.async_config_entry_first_refresh()
        await self.config_coord.async_config_entry_first_refresh()

        try:
            await self._post_first_refresh()
        except Exception as exc:
            _LOGGER.exception("init failed")
            raise ConfigEntryNotReady() from exc

    async def async_shutdown(self) -> None:
        await self.state_coord.async_shutdown()
        await self.update_coord.async_shutdown()
        await self.config_coord.async_shutdown()

    async def _post_first_refresh(self) -> None:
        meta = await self.client.aggregate_meta()
        device = self.state_coord.data.device

        self.unique_id_prefix = device.get("deviceUuid", "")
        self.appliance_kind = ApplianceKind.from_model_description(
            meta.model_description
        )
        self.device_info.update(
            DeviceInfo(
                configuration_url=str(self.client.base_url),
                identifiers={(DOMAIN, device.get("Serial", ""))},
                name=get_device_name(device, meta.model_description),
                hw_version=self.update_coord.data.ai_fw_version.get("HW"),
                sw_version=self.update_coord.data.ai_fw_version.get("SW"),
                connections={
                    (dr.CONNECTION_NETWORK_MAC, dr.format_mac(meta.mac_address))
                },
                model=meta.model_description,
            )
        )

        self._first_refresh_done = True

    async def _fetch_state(self) -> api.AggState:
        return await self.client.aggregate_state(
            default_on_error=self._first_refresh_done
        )

    async def _fetch_update(self) -> api.AggUpdateStatus:
        data = await self.client.aggregate_update_status(
            default_on_error=self._first_refresh_done
        )
        if data.update.get("status") in ("idle", None):
            self.update_coord.update_interval = UPDATE_COORD_IDLE_INTERVAL
        else:
            self.update_coord.update_interval = UPDATE_COORD_ACTIVE_INTERVAL
        return data

    async def _fetch_config(self) -> api.AggConfig:
        return await self.client.aggregate_config()


def get_device_name(device: api.DeviceStatus, model_name: str | None) -> str:
    name = device.get("DeviceName", "")
    if not name:
        name = model_name or ""
    if not name:
        name = device.get("Serial", "")
    return name