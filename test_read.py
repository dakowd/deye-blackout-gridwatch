"""
Run this first, in this order, to sanity-check your setup:

    pip install -r requirements.txt
    copy .env.example .env      # then fill in your real credentials
    python auth.py               # step 1: confirm you can get a token at all
    python test_read.py          # step 2: confirm you can read real data

If a call fails: client.py prints a [DEBUG] block with the raw request
payload and Deye's raw response body -- that tells you exactly what Deye
didn't like, instead of a generic HTTPError.
"""

from client import DeyeCloudClient


def main():
    client = DeyeCloudClient()

    print("=== Stations ===")
    stations_resp = client.list_stations()
    print(stations_resp)

    station_list = stations_resp.get("stationList", [])
    if not station_list:
        print("\nNo stations found -- inspect the raw payload above.")
        return

    first_station = station_list[0]
    station_id = first_station["id"]
    print(f"\nUsing first station: {first_station['name']} (id {station_id})")

    print("\n=== Station latest ===")
    print(client.get_station_latest(station_id))

    print("\n=== Devices under this station ===")
    devices_resp = client.list_devices([station_id])
    print(devices_resp)

    device_items = devices_resp.get("deviceListItems", [])
    if not device_items:
        print("\nNo devices found -- inspect the raw payload above.")
        return

    inverter = next((d for d in device_items if d.get("deviceType") == "INVERTER"), device_items[0])
    device_sn = inverter["deviceSn"]
    print(f"\nUsing device: {device_sn} (type {inverter.get('deviceType')})")

    print("\n=== Battery config (includes current Max Charge Current) ===")
    print(client.get_battery_config(device_sn))

    print("\n=== System config ===")
    print(client.get_system_config(device_sn))

    print("\n=== Available measure points (telemetry key names) ===")
    measure_points_resp = client.get_measure_points(device_sn, inverter.get("deviceType", "INVERTER"))
    print(measure_points_resp)

    keys = measure_points_resp.get("measurePoints", [])
    grid_related = [k for k in keys if "grid" in k.lower() or "utility" in k.lower() or "ac" in k.lower()]
    print(f"\nKeys that might relate to grid status: {grid_related}")

    print("\n=== Live telemetry for this device ===")
    latest_resp = client.get_device_latest([device_sn])
    print(latest_resp)


if __name__ == "__main__":
    main()
