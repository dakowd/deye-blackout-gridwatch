"""
Thin wrapper around the DeyeCloud API v1 endpoints.

Confirmed directly from the DeyeCloud Developer Portal API docs (the ones
you're logged into) -- not guesses:
  - POST /v1.0/station/list             -> list your stations
  - POST /v1.0/station/latest           -> latest aggregate data for a station
  - POST /v1.0/station/device           -> list devices under station(s), batched
  - POST /v1.0/config/battery           -> read maxChargeCurrent, maxDischargeCurrent, etc.
  - POST /v1.0/config/system            -> read systemWorkMode, maxSellPower, etc.
  - POST /v1.0/order/battery/parameter/update  -> WRITE MAX_CHARGE_CURRENT etc.
  - POST /v1.0/order/battery/modeControl       -> enable/disable GRID_CHARGE / GEN_CHARGE

One thing still unconfirmed: the exact batch payload shape for
POST /v1.0/device/latest. Your saved docs page didn't have that specific
panel expanded, so `get_device_latest()` below is a best-guess based on the
pattern used elsewhere in this same API (batched arrays like "deviceSns").
If it 500s/400s, expand that section in the docs UI and send it over.

Everything here operates per-device using `deviceSn` (the inverter/logger
serial number as a string) -- that's the identifier Deye's API uses
throughout, not a numeric ID.
"""

from auth import BASE_URL, get_access_token
from logging_setup import get_logger
from collections import namedtuple
from datetime import datetime
import requests

logger = get_logger()

# voltage: float or None. collected_at: datetime (from Deye's own
# collectionTime for that reading) or None if unavailable.
GridReading = namedtuple("GridReading", ["voltage", "collected_at"])


class DeyeCloudClient:
    def __init__(self):
        self._token = get_access_token()

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
        }

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{BASE_URL}{path}"
        response = requests.post(url, headers=self._headers(), json=payload, timeout=15)

        if response.status_code == 401:
            self._token = get_access_token(force_refresh=True)
            response = requests.post(url, headers=self._headers(), json=payload, timeout=15)

        if not response.ok:
            logger.debug(f"{path} -> HTTP {response.status_code}")
            logger.debug(f"Request payload: {payload}")
            logger.debug(f"Response body: {response.text}")

        response.raise_for_status()
        return response.json()

    # ---------- READ ----------

    def list_stations(self, page: int = 1, size: int = 20) -> dict:
        return self._post("/station/list", {"page": page, "size": size})

    def get_station_latest(self, station_id: int) -> dict:
        return self._post("/station/latest", {"stationId": station_id})

    def list_devices(self, station_ids: list, page: int = 1, size: int = 20) -> dict:
        return self._post(
            "/station/device",
            {"stationIds": station_ids, "page": page, "size": size},
        )

    def get_battery_config(self, device_sn: str) -> dict:
        return self._post("/config/battery", {"deviceSn": device_sn})

    def get_system_config(self, device_sn: str) -> dict:
        return self._post("/config/system", {"deviceSn": device_sn})

    def get_device_latest(self, device_sns: list) -> dict:
        """
        Confirmed shape: request key is "deviceList" (array of deviceSn
        strings), up to 10 per call. Response gives generic telemetry as
        dataList: [{"key": ..., "unit": ..., "value": ...}, ...] per device --
        use get_measure_points() to discover what keys exist for your device.
        """
        return self._post("/device/latest", {"deviceList": device_sns})

    def get_measure_points(self, device_sn: str, device_type: str = "INVERTER") -> dict:
        """
        Lists every telemetry key name available for this device (e.g. "SOC",
        "TotalChargeEnergy", possibly grid-related keys). Use this to find
        the exact key to look for in get_device_latest() when checking grid
        status.
        """
        return self._post(
            "/device/measurePoints",
            {"deviceSn": device_sn, "deviceType": device_type},
        )

    def get_current_max_charge_current(self, device_sn: str):
        """Reads back the currently-applied Max Charge Current, in Amps."""
        resp = self.get_battery_config(device_sn)
        return resp.get("maxChargeCurrent")

    def get_grid_reading(self, device_sn: str) -> "GridReading":
        """
        Pulls GridVoltageL1L2 from the device's live telemetry, along with
        Deye's own `collectionTime` for that reading -- this is when the
        inverter/datalogger actually recorded the value, which can lag
        slightly behind "now" due to datalogger and cloud processing time.
        Useful for knowing precisely when a grid outage actually started,
        not just when we happened to notice it.

        Returns GridReading(voltage=None, collected_at=None) if the device
        didn't report a usable value this cycle -- logs a [DEBUG] line
        explaining why (e.g. device briefly offline vs. key genuinely
        missing), so it's not a silent mystery.
        """
        resp = self.get_device_latest([device_sn])
        device_data = resp.get("deviceDataList", [])
        if not device_data:
            logger.debug(f"get_grid_reading: no deviceDataList in response for {device_sn} "
                         f"-- device may be briefly offline. Raw response: {resp}")
            return GridReading(None, None)

        first_device = device_data[0]
        device_state = first_device.get("deviceState")
        data_list = first_device.get("dataList", [])

        collected_at = None
        raw_collection_time = first_device.get("collectionTime")
        if raw_collection_time is not None:
            try:
                collected_at = datetime.fromtimestamp(int(raw_collection_time))
            except (TypeError, ValueError, OSError):
                logger.debug(f"get_grid_reading: unparseable collectionTime "
                             f"({raw_collection_time!r}) for {device_sn}")

        for item in data_list:
            if item.get("key") == "GridVoltageL1L2":
                try:
                    return GridReading(float(item["value"]), collected_at)
                except (TypeError, ValueError):
                    logger.debug(f"get_grid_reading: GridVoltageL1L2 present but unparseable "
                                 f"(value={item.get('value')!r}) for {device_sn}")
                    return GridReading(None, collected_at)

        logger.debug(f"get_grid_reading: GridVoltageL1L2 key not found for {device_sn} "
                     f"(deviceState={device_state}). Available keys this cycle: "
                     f"{[i.get('key') for i in data_list]}")
        return GridReading(None, collected_at)

    # ---------- WRITE (confirmed) ----------

    def set_max_charge_current(self, device_sn: str, amps: int) -> dict:
        return self._post(
            "/order/battery/parameter/update",
            {
                "deviceSn": device_sn,
                "paramterType": "MAX_CHARGE_CURRENT",
                "value": amps,
            },
        )

    def set_battery_parameter(self, device_sn: str, parameter_type: str, value) -> dict:
        return self._post(
            "/order/battery/parameter/update",
            {"deviceSn": device_sn, "paramterType": parameter_type, "value": value},
        )

    def set_battery_charge_mode(self, device_sn: str, mode: str, enable: bool) -> dict:
        return self._post(
            "/order/battery/modeControl",
            {
                "deviceSn": device_sn,
                "batteryModeType": mode,
                "action": "on" if enable else "off",
            },
        )


if __name__ == "__main__":
    import json

    client = DeyeCloudClient()
    stations = client.list_stations()
    print(json.dumps(stations, indent=2))
