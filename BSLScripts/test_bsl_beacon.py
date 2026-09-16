"""
ST10F272M BSL Hardware Execution Test Beacon
Purpose: Verify if uploaded 32-byte code actually executes on the ST10 core.
Pattern: Transmits 0x55 (01010101b) continuously over ASC0 (TxD0).
Zero Flash access, zero memory reads, zero stack access, zero Stage-2.
"""

import sys
import os
import time
import argparse

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("[!] 'pyserial' is required. Install with: pip install pyserial")
    sys.exit(1)

# ==============================================================================
# 32-Byte Continuous 0x55 Beacon
# ==============================================================================
#
# Pacing is done off the ASC0 transmit flag S0TIR (S0TIC.7, bitoff 0xB6) instead of a
# cycle-counted delay loop. This self-times to whatever baud the autobaud locked, and it
# never executes the undocumented 0xF8 byte that the old blind-delay version stepped through.
BEACON_32B = bytes([
    0xE6, 0xF0, 0x55, 0x00,  # 0xFA40: MOV R0, #0x0055
    0x7F, 0xB6,              # 0xFA44: BSET S0TIR (force "buffer free" so first pass proceeds)
    # LOOP (0xFA46):
    0x9A, 0xB6, 0xFE, 0x70,  # 0xFA46: JNB S0TIR, 0xFA46 (wait until transmit flag sets)
    0x7E, 0xB6,              # 0xFA4A: BCLR S0TIR
    0xF6, 0xF0, 0xB0, 0xFE,  # 0xFA4C: MOV 0xFEB0, R0 (word write 0x55 to S0TBUF)
    0x0D, 0xFA,              # 0xFA50: JMPR cc_UC, 0xFA46 (repeat forever)
    # Padded with NOPs to exactly 32 bytes:
    0xCC, 0x00,              # 0xFA52: NOP
    0xCC, 0x00,              # 0xFA54: NOP
    0xCC, 0x00,              # 0xFA56: NOP
    0xCC, 0x00,              # 0xFA58: NOP
    0xCC, 0x00,              # 0xFA5A: NOP
    0xCC, 0x00,              # 0xFA5C: NOP
    0xCC, 0x00               # 0xFA5E: NOP
])
assert len(BEACON_32B) == 32

def find_serial_ports():
    ports = list(serial.tools.list_ports.comports())
    return [p.device for p in ports]

def run_beacon_test(port: str = None, baud: int = 19200):
    print("=" * 72, flush=True)
    print(" ST10F272M BSL Hardware Execution Test Beacon")
    print(" Goal: Verify code execution at 0xFA40 via continuous 0x55 pulse")
    print("=" * 72, flush=True)

    if not port:
        available = find_serial_ports()
        if not available:
            print("[!] No COM ports detected!")
            return False
        port = available[0]

    print(f"[*] Opening {port} @ {baud} baud (8N1)...")

    try:
        ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.15,
            write_timeout=1.0
        )
    except Exception as e:
        print(f"[!] Failed to open {port}: {e}")
        return False

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        print("\n" + "#" * 65)
        print(" >>> ACTION REQUIRED: POWER ON OR RESET HALDEX CONTROLLER NOW <<<")
        print(" (Ensure P0L.4 is connected to GND for BSL entry)")
        print("#" * 65 + "\n")

        print(f"[*] Baud = {baud}. Sending ONE 0x00 every ~1s. Only a genuine 0xD5 is accepted as ACK.")
        print("[*] The 0x00 is the ST10 autobaud byte - it just has to arrive once after a real reset.")
        start_time = time.time()
        ack = False
        timeout = 60.0
        ser.timeout = 1.0  # one probe per second - no flooding, no stray-byte race
        probes = 0

        while time.time() - start_time < timeout:
            # Drop anything stale (e.g. 0x55 from a still-running prior upload, or line noise)
            # so a leftover byte can never be mistaken for a fresh ACK.
            ser.reset_input_buffer()
            ser.write(b"\x00")
            ser.flush()
            probes += 1

            rx = ser.read(1)  # blocks up to 1.0s for the reply
            if rx:
                val = rx[0]
                if val == 0xD5:
                    ack = True
                    break
                # Anything else means the chip is NOT in a fresh BSL (running code / noise).
                print(f"\n  [ignoring 0x{val:02X}: not a real 0xD5 ACK - power-cycle for a fresh BSL]")

            rem = int(timeout - (time.time() - start_time))
            print(f"\r  ... probe #{probes}, waiting for genuine 0xD5 ACK [{rem}s left] ...", end="", flush=True)

        if not ack:
            print("\n\n[-] No genuine 0xD5 ACK. The ST10 needs a real power cycle/reset into BSL (P0L.4 low).")
            return False

        print(f"\n\n[*** SUCCESS ***] Genuine ST10 BSL ACK (0xD5) after {probes} probe(s).")

        # Chip is now reading EXACTLY 32 bytes. Send them immediately, with no extra 0x00.
        time.sleep(0.01)
        print("[*] Uploading 32-byte Beacon code to 0xFA40 (no extra bytes)...")
        ser.write(BEACON_32B)
        ser.flush()

        print("[+] Beacon uploaded! Listening for 0x55 pulses on serial line...\n")

        count_55 = 0
        total_rx = 0
        listen_start = time.time()
        ser.timeout = 0.5

        while time.time() - listen_start < 10.0:
            byte = ser.read(1)
            if byte:
                val = byte[0]
                total_rx += 1
                now_str = time.strftime('%H:%M:%S')
                if val == 0x55:
                    count_55 += 1
                    print(f"[{now_str}] >>> BEACON CONFIRMED: 0x{val:02X} (Count: {count_55}) <<<")
                else:
                    print(f"[{now_str}] [RX] Byte: 0x{val:02X} (bin: {val:08b})")

            if count_55 >= 20:
                print(f"\n[*** VERIFIED ***] ST10 IS EXECUTING USER CODE AT 0xFA40! ({count_55} beacons received)")
                return True

        if total_rx == 0:
            print("\n[-] No bytes received after beacon upload.")
        else:
            print(f"\n[-] Received {total_rx} bytes, but expected 0x55 beacon.")

        return count_55 > 0

    finally:
        ser.close()
        print("\n[+] Serial port closed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ST10 BSL Execution Test Beacon")
    parser.add_argument("port", nargs="?", default=None, help="COM port")
    parser.add_argument("baud", nargs="?", type=int, default=19200, help="Baud rate")
    args = parser.parse_args()
    run_beacon_test(args.port, args.baud)
