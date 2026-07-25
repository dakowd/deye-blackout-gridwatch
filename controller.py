"""
The actual automation loop.

Logic:
  - Poll GridVoltageL1L2 every POLL_INTERVAL_SECONDS.
  - If voltage < GRID_DOWN_VOLTAGE_THRESHOLD for DEBOUNCE_POLLS in a row,
    consider the grid DOWN. If it's >= threshold for that many polls in a
    row, consider it UP.
  - Only WRITE Max Charge Current when the debounced state actually changes
    (not every poll) -- this avoids hammering the inverter's EEPROM and
    avoids unnecessary API calls.
  - Before writing, it reads back the CURRENT value and skips the write
    entirely if it already matches the target -- avoids redundant writes
    on startup or if you already set it manually to the right value.
  - On startup, we don't assume a state -- we wait for DEBOUNCE_POLLS worth
    of consistent readings before taking any action, so a restart doesn't
    immediately fire a write based on a single noisy reading.

Manual changes via the Deye app: this controller does NOT continuously
re-assert its target value. If you manually change Max Charge Current
while the grid state doesn't change, your change is left alone -- the
controller only looks at the current value at the moment of a grid state
transition, and will overwrite it then if it doesn't match that state's
target. In short: manual changes survive until the next real grid
up/down transition, then get superseded. This is deliberate -- the
automation shouldn't fight you if you intervene by hand mid-outage.

Run with:
    python controller.py

Stop with Ctrl+C -- it exits cleanly without leaving anything in a bad state
(whatever the last-applied setting was stays applied, which is what you want).
"""

import os
import sys
import time
from datetime import datetime, time as dt_time
from typing import Optional

from dotenv import load_dotenv

from client import DeyeCloudClient
from logging_setup import get_logger

load_dotenv()
logger = get_logger()

DEVICE_SN = os.getenv("DEYE_DEVICE_SN")
GRID_DOWN_THRESHOLD = float(os.getenv("GRID_DOWN_VOLTAGE_THRESHOLD", "100"))
NORMAL_MAX_CHARGE = int(os.getenv("NORMAL_MAX_CHARGE_CURRENT", "100"))
OUTAGE_MAX_CHARGE = int(os.getenv("OUTAGE_MAX_CHARGE_CURRENT", "200"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
DEBOUNCE_POLLS = int(os.getenv("DEBOUNCE_POLLS", "3"))

ACTIVE_START_TIME = datetime.strptime(os.getenv("ACTIVE_START_TIME", "06:00"), "%H:%M").time()
ACTIVE_END_TIME = datetime.strptime(os.getenv("ACTIVE_END_TIME", "17:00"), "%H:%M").time()
OUTSIDE_WINDOW_SLEEP = int(os.getenv("OUTSIDE_WINDOW_SLEEP_SECONDS", "600"))

GRID_UP = "GRID_UP"
GRID_DOWN = "GRID_DOWN"


def log(msg: str):
    """Kept as a thin wrapper so the rest of the file doesn't need to change --
    routes to the rotating file + console logger instead of a bare print()."""
    if msg.startswith("ERROR"):
        logger.error(msg)
    else:
        logger.info(msg)


def log_warn(msg: str):
    """For grid-offline events specifically -- logged at WARNING level so
    they're easy to grep out of the log file separately from routine polls
    (e.g. `grep WARNING logs/deyeblackoutgridwatch.log*` surfaces every
    outage the controller ever detected)."""
    logger.warning(msg)


def is_within_active_window(now: Optional[dt_time] = None) -> bool:
    """
    Uses the system's local time -- make sure your Pi's timezone is set
    correctly (check with `timedatectl` on Linux). Assumes a same-day
    window (e.g. 06:00-17:00); doesn't support windows that cross midnight.
    """
    now = now or datetime.now().time()
    return ACTIVE_START_TIME <= now <= ACTIVE_END_TIME


def raw_grid_state(voltage: Optional[float]) -> Optional[str]:
    """None means we couldn't read a value this cycle -- treat as unknown, not a state."""
    if voltage is None:
        return None
    return GRID_DOWN if voltage < GRID_DOWN_THRESHOLD else GRID_UP


def main():
    if not DEVICE_SN:
        log("ERROR: DEYE_DEVICE_SN not set in .env. Run test_read.py first to find it.")
        sys.exit(1)

    client = DeyeCloudClient()
    log(f"Starting controller for device {DEVICE_SN}")
    log(f"Grid-down threshold: {GRID_DOWN_THRESHOLD}V | Normal charge: {NORMAL_MAX_CHARGE}A | "
        f"Outage charge: {OUTAGE_MAX_CHARGE}A | Poll every {POLL_INTERVAL}s | Debounce: {DEBOUNCE_POLLS} polls")
    log(f"Active window: {ACTIVE_START_TIME.strftime('%H:%M')}-{ACTIVE_END_TIME.strftime('%H:%M')} "
        f"(local time) -- outside this window the controller sleeps without polling")

    applied_state = None       # the state we've actually written to the inverter
    candidate_state = None     # the state we're currently seeing, not yet confirmed
    candidate_count = 0
    was_active = None          # tracks last window state, so we only log the transition once

    while True:
        active_now = is_within_active_window()

        if active_now != was_active:
            if active_now:
                log("Entering active window -- resuming polling.")
            else:
                log(f"Outside configured active window ({ACTIVE_START_TIME.strftime('%H:%M')}-"
                    f"{ACTIVE_END_TIME.strftime('%H:%M')}) -- pausing polling until it reopens. "
                    f"Checking again every {OUTSIDE_WINDOW_SLEEP}s.")
                # Reset debounce state -- start fresh once the window reopens tomorrow
                candidate_state = None
                candidate_count = 0
            was_active = active_now

        if not active_now:
            time.sleep(OUTSIDE_WINDOW_SLEEP)
            continue

        try:
            grid_reading = client.get_grid_reading(DEVICE_SN)
            voltage = grid_reading.voltage
            collected_at = grid_reading.collected_at
            collected_at_str = collected_at.strftime("%Y-%m-%d %H:%M:%S") if collected_at else "unknown"

            reading = raw_grid_state(voltage)

            if reading is None:
                log(f"Could not read grid voltage this cycle (got: {voltage}) -- skipping")
            else:
                log(f"Grid voltage: {voltage}V (collected at {collected_at_str}) -> reads as {reading}")

                if reading == candidate_state:
                    candidate_count += 1
                else:
                    candidate_state = reading
                    candidate_count = 1

                # Debounced state has been stable long enough -- act if it's new
                if candidate_count >= DEBOUNCE_POLLS and candidate_state != applied_state:
                    target_amps = OUTAGE_MAX_CHARGE if candidate_state == GRID_DOWN else NORMAL_MAX_CHARGE

                    if candidate_state == GRID_DOWN:
                        log_warn(f"GRID OFFLINE detected -- voltage {voltage}V as of {collected_at_str} "
                                 f"(Deye's own reading time). Confirmed after {candidate_count} consecutive polls.")
                    else:
                        log(f"Grid back ONLINE -- voltage {voltage}V as of {collected_at_str}. "
                            f"Confirmed after {candidate_count} consecutive polls.")

                    try:
                        current_amps = client.get_current_max_charge_current(DEVICE_SN)
                    except Exception as e:
                        log(f"ERROR reading current Max Charge Current, skipping this cycle: {e}")
                        current_amps = None

                    if current_amps == target_amps:
                        log(f"State change confirmed: {applied_state} -> {candidate_state}, but Max Charge "
                            f"Current is already {current_amps}A -- no write needed.")
                        applied_state = candidate_state
                    elif current_amps is not None:
                        log(f"State change confirmed: {applied_state} -> {candidate_state}. "
                            f"Current setting is {current_amps}A, target is {target_amps}A. Writing...")
                        try:
                            result = client.set_max_charge_current(DEVICE_SN, target_amps)
                            log(f"Write result: {result}")
                            applied_state = candidate_state
                        except Exception as e:
                            log(f"ERROR writing Max Charge Current: {e}. Will retry next cycle.")

        except Exception as e:
            log(f"ERROR during poll cycle: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped by user.")
