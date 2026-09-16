import time
import struct
from typing import Optional, List, Tuple, Callable

BROADCAST_ADDR = 0x200

class MessageTimeoutError(TimeoutError):
    pass

class TP20Transport:
    def __init__(self, device, module: int = 0x0A, bus: int = 0, timeout: float = 2.0, debug: bool = False, log_fn=None, intercept: bool = False):
        self.device = device
        self.module = module
        self.bus = bus
        self.timeout = timeout
        self.msgs: List[Tuple[int, bytes]] = []

        self.tx_seq = 0
        self.rx_seq = 0
        self.time_between_packets = 0.005

        self.rx_addr = 0
        self.tx_addr = 0
        self.debug = debug
        self.log_fn = log_fn or print

        if intercept:
            self.intercept_channel(module)
        else:
            self.open_channel(module)

    def log(self, msg: str):
        if self.debug:
            self.log_fn(msg)

    def can_recv(self, addr: Optional[int] = None) -> bytes:
        if addr is None:
            addr = self.rx_addr

        start_time = time.monotonic()
        while time.monotonic() - start_time < self.timeout:
            for idx, (a, dat) in enumerate(self.msgs):
                if a == addr:
                    return self.msgs.pop(idx)[1]

            raw_msgs = self.device.can_recv(10)
            for a, dat, bus in raw_msgs:
                if bus != self.bus:
                    continue
                # Only queue TP2.0 relevant messages, ignore broadcast bus chatter (e.g. 0x2C0)
                if a == addr or a in (BROADCAST_ADDR, BROADCAST_ADDR + self.module, self.rx_addr, self.tx_addr):
                    self.log(f"  [TP20 CAN RX] 0x{a:03X} -> {dat.hex()}")
                    self.msgs.append((a, dat))

            for idx, (a, dat) in enumerate(self.msgs):
                if a == addr:
                    return self.msgs.pop(idx)[1]

        err_info = f"Timed out waiting for message on ID 0x{addr:X} (timeout={self.timeout}s)."
        if self.msgs:
            err_info += f" Currently queued: {[(hex(a), d.hex()) for a, d in self.msgs]}"
        self.log(f"  [TP20 TIMEOUT] {err_info}")
        raise MessageTimeoutError(err_info)

    def can_send(self, dat: bytes, addr: Optional[int] = None):
        if addr is None:
            addr = self.tx_addr

        self.log(f"  [TP20 CAN TX] 0x{addr:03X} <- {dat.hex()}")
        self.device.can_send(addr, dat, self.bus)
        time.sleep(self.time_between_packets)

    def open_channel(self, module: int):
        # Dest: <module> (0x0A for Haldex AWD)
        # Opcode 0xc0 (setup)
        # RX ID: V = 1 (invalid), 0x1000
        # TX ID: 0x300 + V = 0 (valid), 0x0300
        # Application type: 0x01 (KWP2000)
        setup_payload = bytes([module]) + b"\xc0\x00\x10\x00\x03\x01"
        self.log(f"[TP20] Establishing channel to module 0x{module:02X} via broadcast 0x{BROADCAST_ADDR:03X}...")
        self.can_send(setup_payload, BROADCAST_ADDR)

        # Channel setup response (ECU responds on 0x200 + logical ID, e.g. 0x20A)
        start_time = time.monotonic()
        dat = None
        while time.monotonic() - start_time < self.timeout:
            msgs = self.device.can_recv(20)
            for a, payload, _ in msgs:
                if 0x200 <= a <= 0x2FF and len(payload) >= 7 and payload[1] == 0xD0:
                    dat = payload
                    self.log(f"[TP20] Received setup response from 0x{a:03X}: {dat.hex()}")
                    break
            if dat is not None:
                break

        if dat is None:
            raise MessageTimeoutError(f"Timed out waiting for channel setup response from module 0x{module:02X}")

        status, rx, tx, _ = struct.unpack("<xBHHB", dat)
        if status != 0xD0:
            raise RuntimeError(f"Failed to setup channel, got status 0x{status:02X}: {dat.hex()}")

        self.rx_addr = rx
        self.tx_addr = tx
        self.log(f"[TP20] Channel open! ECU listens on 0x{tx:03X}, transmits on 0x{rx:03X}")

        # Set timing parameters
        # Opcode: 0xa0 (Parameters request)
        # Block size: 0x0f
        # T1: 0x8a (100ms timeout)
        # T2: 0xff
        # T3: 0x0a (1ms)
        # T4: 0xff
        self.can_send(b"\xa0\x0f\x8a\xff\x0a\xff")

        dat = self.can_recv()
        self.log(f"[TP20] Timing params response: {dat.hex()}")
        opcode = dat[0]
        if opcode != 0xA1:
            self.log(f"[TP20] Warning: timing params response was 0x{opcode:02X}")

        self.time_between_packets = 0.015
        self.tx_seq = 0
        self.rx_seq = 0

    def intercept_channel(self, module: int, timeout: float = 30.0):
        setup_payload = bytes([module]) + b"\xc0\x00\x10\x00\x03\x01"
        self.log(f"[TP20] Intercept active: rapidly broadcasting 0x{BROADCAST_ADDR:03X} every 10ms for module 0x{module:02X}...")
        start_time = time.monotonic()
        dat = None
        while time.monotonic() - start_time < timeout:
            self.device.can_send(BROADCAST_ADDR, setup_payload, self.bus)
            t_chk = time.monotonic()
            while time.monotonic() - t_chk < 0.010:
                msgs = self.device.can_recv(10)
                for a, payload, _ in msgs:
                    if 0x200 <= a <= 0x2FF and len(payload) >= 7 and payload[1] == 0xD0:
                        dat = payload
                        self.log(f"[TP20] INTERCEPTED setup response from 0x{a:03X}: {dat.hex()}")
                        break
                if dat is not None:
                    break
                time.sleep(0.002)
            if dat is not None:
                break

        if dat is None:
            raise MessageTimeoutError(f"Timed out waiting for channel setup response from module 0x{module:02X} after {timeout}s")

        status, rx, tx, _ = struct.unpack("<xBHHB", dat)
        self.rx_addr = rx
        self.tx_addr = tx
        self.log(f"[TP20] Intercept established! ECU listens on 0x{tx:03X}, transmits on 0x{rx:03X}")

        # Send timing parameters immediately (zero delay)
        self.can_send(b"\xa0\x0f\x8a\xff\x0a\xff")
        dat = self.can_recv()
        self.log(f"[TP20] Timing params response: {dat.hex()}")

        self.time_between_packets = 0.005
        self.tx_seq = 0
        self.rx_seq = 0

    def wait_for_ack(self):
        expected_seq = (self.tx_seq + 1) & 0xF
        expected_ack = 0xB0 | expected_seq
        start_time = time.monotonic()
        while time.monotonic() - start_time < self.timeout:
            try:
                dat = self.can_recv()
            except MessageTimeoutError:
                break

            if not dat:
                continue

            if dat[0] == expected_ack:
                return

            # If it's a data packet, queue it for later (implicit ACK)
            if (dat[0] >> 4) in (0x0, 0x1, 0x2, 0x3):
                self.msgs.append((self.rx_addr, dat))
                return

            if (dat[0] & 0xF0) == 0xB0:
                self.log(f"  [TP20] Discarding stale ACK 0x{dat.hex()}, expected 0x{expected_ack:02X}")
                continue

        raise RuntimeError(f"Did not receive expected ACK 0x{expected_ack:02X}")

    def send_ack(self):
        seq = (self.rx_seq + 1) & 0xF
        self.can_send(bytes([0xB0 | seq]))

    def send(self, dat: bytes):
        if len(dat) > 0xFF:
            raise ValueError("Packet longer than 255 bytes not supported")

        # Discard any stale ACKs or keepalives from previous transaction
        self.msgs = [(a, d) for a, d in self.msgs if (d[0] & 0xF0) not in (0xB0, 0xA0)]
        payload = struct.pack(">H", len(dat)) + dat

        while payload:
            last = len(payload) <= 7

            to_send = bytes([(0x10 if last else 0x20) | self.tx_seq])
            to_send += payload[:7]

            self.can_send(to_send)

            if last:
                self.wait_for_ack()

            self.tx_seq = (self.tx_seq + 1) & 0xF
            payload = payload[7:]

    def recv(self) -> bytes:
        payload = b""
        expected_len: Optional[int] = None
        expected_seq: Optional[int] = None
        start_time = time.monotonic()

        while time.monotonic() - start_time < self.timeout:
            try:
                dat = self.can_recv()
            except MessageTimeoutError:
                break

            if not dat:
                continue

            typ, seq = (dat[0] >> 4), (dat[0] & 0xF)

            # Handle keepalive / test frame (0xA3)
            if (dat[0] & 0xF0) == 0xA0:
                if dat[0] == 0xA8:
                    raise ConnectionError('ECU closed the TP2.0 channel (A8) during receive')
                if dat[0] == 0xA3:
                    self.log("  [TP20] Keepalive frame 0xA3 received, responding...")
                    try:
                        self.can_send(b"\xa3")
                    except Exception:
                        pass
                continue

            # Stray ACK packet (0xB0..0xBF)
            if typ == 0xB:
                self.log(f"  [TP20] Ignoring unexpected ACK packet 0x{dat.hex()}")
                continue

            # TP2 data opcodes: 0=more/ACK, 1=last/ACK,
            # 2=more/no ACK, 3=last/no ACK. Large upload responses
            # request an ACK at the negotiated block boundary (15 frames).
            if typ not in (0x0, 0x1, 0x2, 0x3):
                self.log(f"  [TP20] Ignoring unknown frame type 0x{dat[0]:02X}")
                continue

            if expected_seq is not None and seq != expected_seq:
                raise RuntimeError(f"TP2.0 receive sequence mismatch: expected {expected_seq:X}, got {seq:X}")
            expected_seq = (seq + 1) & 0xF
            self.rx_seq = seq
            payload += dat[1:]

            if expected_len is None and len(payload) >= 2:
                expected_len = struct.unpack(">H", payload[:2])[0]

            if typ in (0x0, 0x1):
                self.send_ack()

            if typ in (0x1, 0x3):
                break

            if expected_len is not None and len(payload) >= expected_len + 2:
                raise RuntimeError("TP2.0 declared length reached without a final data frame")

        if expected_len is None or len(payload) < expected_len + 2:
            err_msg = (
                f"TP2.0 recv timed out. Expected length: {expected_len}, "
                f"Received payload: {payload.hex()} ({len(payload)} bytes)"
            )
            self.log(f"  [TP20 ERROR] {err_msg}")
            raise MessageTimeoutError(err_msg)

        data = payload[2 : expected_len + 2]
        return data

    def disconnect(self):
        # Disconnect channel: Opcode 0xa8
        try:
            self.log(f"[TP20] Disconnecting channel (opcode 0xa8)...")
            self.can_send(b"\xa8")
        except Exception:
            pass
