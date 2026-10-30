# Real sensor input

The application consumes readings; it does not infer temperatures from a camera, query your home network, or control an appliance. The built-in producers can share a snapshot using an operating-system file lock. Direct third-party writers must follow the same locking contract or use a single aggregator.

## CSV contract

```csv
sensor,temperature_c,observed_at
freezer,-18.2,1700000000.0
cooker,76.5,1700000000.0
```

The timestamps above illustrate the format and will appear stale today. Use `python -m thermal_watch sample` for fresh sample data.

- IDs must match your config. Header names and order must match exactly.
- Temperatures must be finite numbers between -100 and 350 °C. Physical adapters should enforce their own tighter operating ranges.
- Timestamps are Unix seconds. A sample more than five seconds in the future is rejected; old or duplicate samples cannot replace a newer accepted sample during normal clock operation. After a system clock rollback, a fresh current sample can recover a clock fault.
- A snapshot is limited to 1 MiB. The built-in writer keeps one current row per sensor, preserving other sensors while replacing its own row. There is no row-count lifetime limit or manual rotation requirement for built-in producers. A larger file from another producer is not read or changed: the dashboard faults every sensor and names the 1 MiB limit, and `record` refuses to write to it.
- For a legacy file with repeated IDs, only the final physical row for each sensor is its current observation. This also permits recovery from old future-dated rows after clock rollback. An invalid final row faults that sensor; older invalid rows superseded by a valid final row are historical, not current observations.
- A malformed header or unknown ID rejects the whole snapshot and marks every configured sensor as faulty, including sensors with otherwise valid rows. This fails closed when the file may belong to the wrong configuration. Remove the unknown ID or update the configuration, then provide fresh valid readings to recover. An invalid value for a known ID faults only that sensor. Do not replace missing readings with a made-up normal number.
- Choose an update interval comfortably below `stale_seconds`, for example five seconds for a 20-second freshness budget.

The `record` command is useful for a one-off reading. A custom Python producer should call `append_reading` (the compatibility function name now performs an atomic upsert). It locks a sidecar `<snapshot>.lock` file with `msvcrt.locking` on Windows or `fcntl.flock` on POSIX, reads the previous snapshot, keeps the final row for each ID, fsyncs a temporary file, then atomically replaces the snapshot. Cooperative concurrent producers preserve each other's rows. A writer waits up to two seconds for the lock; sensor mode reports a failed write and tries again at its next interval. The operating system releases the lock if a process exits. Keep the sidecar file in place; deleting it while producers run breaks coordination.

The writer supports up to 64 IDs as a storage bound; the dashboard config supports up to 16 sensors. Snapshot replacement also retries a temporary Windows file-sharing conflict for up to two seconds. A reader never sees a partially written snapshot. A direct third-party writer must coordinate with this lock and atomic replacement protocol; independently appending without coordination is unsupported.

## Linux DS18B20

Enable the kernel's 1-Wire support for your board and connect a correctly powered probe following its manufacturer and board documentation. Probe wiring, waterproofing, electrical ratings, and temperature ratings vary. A DS18B20 is not an oven probe. Its supported range here is -55 to 125 °C. Do not put a general-purpose cable or unverified probe in a cooker.

The Linux kernel exposes a device file such as:

```text
/sys/bus/w1/devices/28-000000000000/w1_slave
```

Replace the example identifier with your device's actual identifier, then run:

```sh
python -m thermal_watch sensor --device /sys/bus/w1/devices/28-000000000000/w1_slave --file readings.csv --sensor freezer --interval 5
```

Start the dashboard in another terminal:

```sh
python -m thermal_watch serve --csv readings.csv --config config.local.json
```

The producer accepts a two-line response only when the first line ends in `YES`, then reads the millidegree Celsius value after `t=`. Invalid CRC, missing data, parse errors, and values outside the probe range produce no replacement reading; the dashboard will become stale if valid data does not resume. The operating-system driver provides the CRC check. Fixture tests verify parsing, not electrical behavior.

An exact 85 °C can be a legitimate cooker reading or a DS18B20 power-up value. A number alone cannot distinguish those cases, so this parser accepts a CRC-valid 85 °C. Configure and validate the driver's conversion-success checking where supported, and verify initial conversions against a reference thermometer before depending on the reading. The [Linux `w1_therm` documentation](https://docs.kernel.org/w1/slaves/w1_therm.html) describes the interface and the conversion-error features.

## Commissioning checklist

1. Compare the probe with an independent thermometer at relevant temperatures.
2. Disconnect the probe and confirm that the dashboard reports a fault or stale input.
3. Produce a sustained out-of-range reading and verify the local alarm and recovery.
4. If you enable webhooks, confirm delivery at your own endpoint and test a failed delivery.
5. Stop the producer, then the dashboard, and observe how your separate monitoring process detects those failures.

No physical hardware was available during this project's automated verification. These steps remain a responsibility of each installation.
