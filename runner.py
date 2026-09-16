"""CAN adapter ownership for the shared Haldex flash/readout CLI.

The protocol sees one logical bus (0) and can_send/can_recv/can_clear.
Only this module knows how adapters are opened, normalized, and closed.
Optional hardware libraries are imported only when opening their adapter.
"""
import sys
import struct
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def add_adapter_arguments(parser, default="j2534"):
    parser.add_argument("--adapter", choices=("j2534", "panda", "socketcan"), default=default)
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
                # Current Panda: (address, data, bus); older releases include
                # the hardware timestamp as the second of four fields.
                if len(frame) == 3:
                    address, data, bus = frame
                else:
                    address, _, data, bus = frame
                if bus == self.bus:  # excludes TX echoes (bus | 0x80)
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
            self.device.set_safety_mode(0)  # silent after releasing ownership
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
        if message is None or message.is_error_frame or message.is_remote_frame or message.is_extended_id:
            return []
        return [(message.arbitration_id, bytes(message.data), 0)]

    def can_clear(self, flags=0xffff):
        # Bound draining even on a busy vehicle bus.
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
        # Configure the Linux interface bitrate before invocation, using the
        # operating system. Opening a bus does not reconfigure its link.
        bus = can.Bus(interface="socketcan", channel=args.channel, receive_own_messages=False)
        return SocketCANAdapter(bus, can.Message, args.channel)
    raise ValueError(f"Unknown CAN adapter: {args.adapter}")


def main(argv=None):
    from flasher.haldex_flash import main as haldex_main
    return haldex_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
