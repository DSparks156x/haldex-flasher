"""Adapter-neutral command line for the shared Haldex flash engine."""
import argparse
import json
import logging
import struct
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def number(value):
    return int(value, 0)


def add_adapter_arguments(parser, default="j2534"):
    parser.add_argument("--adapter", choices=("j2534", "panda", "socketcan"), default=default)
    parser.add_argument("--dll", help="J2534 DLL path (default: registry discovery)")
    parser.add_argument("--baud", type=int, default=500000)
    parser.add_argument("--bus", type=int, choices=(0, 1, 2), default=0,
                        help="Physical Panda CAN bus; protocol sees logical bus 0")
    parser.add_argument("--serial", help="Panda serial number")
    parser.add_argument("--channel", default="can0", help="SocketCAN interface (default can0)")


class PandaAdapter:
    def __init__(self, device, bus):
        self.device = device
        self.bus = bus
        self.description = f"Panda CAN {bus}"

    def can_send(self, address, data, bus=0):
        self.device.can_send(address, data, self.bus)

    def can_recv(self, timeout_ms=10):
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            frames = []
            for frame in self.device.can_recv():
                if len(frame) == 3:
                    address, data, bus = frame
                else:
                    address, _, data, bus = frame
                if bus == self.bus:
                    frames.append((address, bytes(data), 0))
            if frames or time.monotonic() >= deadline:
                return frames
            time.sleep(0.001)

    def can_clear(self, flags=0xffff):
        self.device.can_clear(flags)

    def disconnect(self):
        pass

    def close(self):
        try:
            self.device.set_safety_mode(0)
        finally:
            self.device.close()


class SocketCANAdapter:
    def __init__(self, bus, message_type, channel):
        self.device = bus
        self.message_type = message_type
        self.description = f"SocketCAN {channel}"

    def can_send(self, address, data, bus=0):
        self.device.send(self.message_type(arbitration_id=address, data=data,
                                           is_extended_id=False))

    def can_recv(self, timeout_ms=10):
        message = self.device.recv(timeout=timeout_ms / 1000)
        if (message is None or message.is_error_frame or message.is_remote_frame
                or message.is_extended_id):
            return []
        return [(message.arbitration_id, bytes(message.data), 0)]

    def can_clear(self, flags=0xffff):
        for _ in range(4096):
            if self.device.recv(timeout=0) is None:
                break

    def disconnect(self):
        pass

    def close(self):
        self.device.shutdown()


def open_adapter(args):
    """Open one adapter; caller owns close(), including on protocol failures."""
    if args.adapter == "j2534":
        if args.bus != 0:
            raise ValueError("--bus is a Panda option; J2534 uses bus 0")
        if struct.calcsize('P') != 4:
            raise RuntimeError("J2534 requires 32-bit Python; use the project's Python311-32 interpreter")
        from flasher.j2534 import J2534Device
        device = J2534Device(args.dll)
        try:
            device.open()
            device.connect_can(baudrate=args.baud)
            device.description = f"J2534 {device.dll_path}"
            return device
        except BaseException:
            device.close()
            raise
    if args.adapter == "panda":
        from panda import Panda
        device = Panda(args.serial) if args.serial else Panda()
        adapter = PandaAdapter(device, args.bus)
        try:
            device.set_can_speed_kbps(args.bus, args.baud / 1000)
            device.can_clear(0xffff)
            device.set_safety_mode(Panda.SAFETY_ALLOUTPUT)
            return adapter
        except BaseException:
            adapter.close()
            raise
    if args.adapter == "socketcan":
        if args.bus != 0:
            raise ValueError("Use --channel to select SocketCAN; --bus is a Panda option")
        import can
        bus = can.Bus(interface="socketcan", channel=args.channel, receive_own_messages=False)
        return SocketCANAdapter(bus, can.Message, args.channel)
    raise ValueError(f"Unknown CAN adapter: {args.adapter}")


def build_parser():
    parser = argparse.ArgumentParser(description="Haldex Gen4 shared flasher/readout")
    add_adapter_arguments(parser)
    parser.add_argument("--module", type=number, default=0x0A)
    parser.add_argument("--input", help="320 KiB CPU-linear image to flash")
    parser.add_argument("--start", type=number, default=0x18000)
    parser.add_argument("--end", type=number, default=0x4ffff,
                        help="Inclusive end address")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Skip the destructive YES prompt")
    parser.add_argument("--ident-only", action="store_true")
    parser.add_argument("--readout", action="store_true")
    parser.add_argument("--out", default="data/raw/readout/haldex")
    parser.add_argument("--reference")
    parser.add_argument("--readout-passes", type=int, choices=(1, 2), default=1)
    parser.add_argument("--readout-window", type=number, default=0x10000)
    parser.add_argument("--recovery", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--simulator-mode", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--log")
    parser.add_argument("--verbose", action="store_true")
    return parser


def _validate_args(parser, args):
    from flasher.haldex_patcher import selected_blocks, validate_image
    if args.readout:
        conflicts = args.input or args.recovery or args.ident_only or args.simulator_mode
        if conflicts:
            parser.error("--readout cannot be combined with flash, recovery, identification, or simulator options")
        selected_blocks(args.start, args.end)
        if args.reference:
            validate_image(Path(args.reference).read_bytes())
        return
    if args.recovery:
        parser.error("--recovery was removed; the shared engine resumes safely through the normal flash path")
    if args.ident_only:
        if args.input or args.dry_run or args.simulator_mode:
            parser.error("--ident-only cannot be combined with flash or dry-run options")
        return
    if not args.input:
        parser.error("--input is required unless --readout or --ident-only is selected")
    selected_blocks(args.start, args.end)


def _progress(stage, percent, detail="", speed=0.0, eta_sec=0.0):
    suffix = f" {speed:.1f} B/s" if speed else ""
    if eta_sec:
        suffix += f" ETA {eta_sec:.1f}s"
    print(f"[{stage:>10}] {percent:6.1f}% {detail}{suffix}", flush=True)


def _configure_logging(args):
    handlers = [logging.StreamHandler(sys.stderr)]
    if args.log:
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def run_ident(args):
    from flasher.haldex_flasher import HaldexFlasher
    device = open_adapter(args)
    flasher = HaldexFlasher(device=device, module=args.module,
                            device_factory=lambda: open_adapter(args), progress_cb=_progress)
    result = flasher.read_ecu_info()  # owns and closes the injected adapter
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def run_flash(args):
    from flasher.artifacts import prepare_image
    prepared = prepare_image(args.input, args.start, args.end,
                             simulator_mode=args.simulator_mode)
    if args.dry_run:
        print(json.dumps(dict(prepared["metadata"], status="validated", dry_run=True),
                         indent=2, sort_keys=True))
        return 0
    print(json.dumps(prepared["metadata"], indent=2, sort_keys=True))
    if not args.yes and input("Type YES to erase and flash the selected sectors: ") != "YES":
        print("Cancelled before adapter access.")
        return 1
    from flasher.haldex_flasher import HaldexFlasher
    device = open_adapter(args)
    flasher = HaldexFlasher(device=device, module=args.module,
                            device_factory=lambda: open_adapter(args), progress_cb=_progress)
    result = flasher.flash_binary(args.input, args.start, args.end,
                                  simulator_mode=args.simulator_mode)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def run_readout(args):
    from flasher.haldex_flash import (ApplicationReader, IMAGE_SIZE, TP20Transport,
                                      application_checksums, capture, compare_reference)
    length = args.end - args.start + 1
    plan = {"operation": "readout", "adapter": args.adapter, "start": args.start,
            "end": args.end, "length": length, "passes": args.readout_passes,
            "window": args.readout_window, "hardware_access": not args.dry_run,
            "firmware_writes": False}
    if args.dry_run:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=False)
    events = []
    cleanup_errors = []
    report = dict(plan, status="in_progress", cleanup_errors=cleanup_errors)
    device = None
    tp = None
    reader = None
    try:
        device = open_adapter(args)
        tp = TP20Transport(device, module=args.module, timeout=2.0, debug=args.verbose)
        reader = ApplicationReader(tp, events.append)
        report["identification_hex"] = reader.identify().hex()
        reader.enter()
        captures = capture(
            reader, output, args.start, length, args.readout_passes,
            args.readout_window, events.append,
            lambda p, done, total: _progress(f"READ {p}", 100 * done / total,
                                             f"{done}/{total} bytes"),
        )
        report["captures"] = captures
        report["saved_ranges"] = {
            item["path"]: {"start": args.start, "end_exclusive": args.end + 1}
            for item in captures
        }
        captured = (output / captures[0]["path"]).read_bytes()[args.start:args.end + 1]
        checksums = application_checksums(captured, args.start)
        report["application_checksums"] = checksums
        if args.reference:
            report["comparisons"] = [compare_reference(captured, args.start, args.reference)]
        invalid = any(row["valid"] is False for row in checksums)
        report["status"] = "checksum_mismatch" if invalid else "captured_checksums_valid"
    except Exception as exc:
        report.update(status="requires_attention", error=f"{type(exc).__name__}: {exc}")
    finally:
        if reader is not None:
            cleanup_errors.extend(reader.leave())
        if tp is not None:
            try:
                tp.disconnect()
            except Exception as exc:
                cleanup_errors.append(f"disconnect: {exc}")
        if device is not None:
            try:
                device.close()
            except Exception as exc:
                cleanup_errors.append(f"adapter close: {exc}")
        if cleanup_errors:
            report["status"] = "requires_attention"
        (output / "events.json").write_text(json.dumps(events, indent=2), encoding="utf-8")
        (output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "captured_checksums_valid" else 1


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _validate_args(parser, args)
    except ValueError as exc:
        parser.error(str(exc))
    _configure_logging(args)
    if args.readout:
        return run_readout(args)
    if args.ident_only:
        return run_ident(args)
    return run_flash(args)


if __name__ == "__main__":
    raise SystemExit(main())
