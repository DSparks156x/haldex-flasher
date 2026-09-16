"""
ST10F272M CAN Bootloader Lock-In via BSL
Writes 0x001119AB to RAM 0x00F9FC and jumps to 0x000000 (User Bootloader).
This forces the ECU to stay in resident CAN Bootloader mode indefinitely,
preventing the 200ms jump to application and allowing unlimited CAN flashing.
"""

import sys
import time
import argparse
import serial
import serial.tools.list_ports

def make_lockin_payload() -> bytes:
    code = bytes([
        # 0xFA40: MOV R12, #0xF9FC (address of bootloader RAM flag)
        0xE6, 0xFC, 0xFC, 0xF9,
        # 0xFA44: MOV R13, #0x19AB (low word of 0x001119AB)
        0xE6, 0xFD, 0xAB, 0x19,
        # 0xFA48: MOV [R12], R13
        0xC4, 0xDC,
        # 0xFA4A: MOV R13, #0x0011 (high word of 0x001119AB)
        0xE6, 0xFD, 0x11, 0x00,
        # 0xFA4E: MOV [R12+2], R13
        0xC4, 0xDE, 0x02, 0x00,
        # 0xFA52: JMPS 0x00, 0x0000 (Jump to User Flash Bootloader ResetEntry)
        0xFA, 0x00, 0x00, 0x00,
        # NOP padding to 32 bytes
        0xCC, 0x00, 0xCC, 0x00, 0xCC, 0x00, 0xCC, 0x00, 0xCC, 0x00
    ])
    assert len(code) == 32
    return code

def main():
    parser = argparse.ArgumentParser(description="Lock ST10 into CAN Bootloader mode via UART BSL")
    parser.add_argument("--port", default="COM2", help="UART BSL COM port (default: COM2)")
    parser.add_argument("--baud", type=int, default=112000, help="UART baud rate (default: 112000)")
    args = parser.parse_args()

    print("=" * 70)
    print(" ST10F272M CAN Bootloader Lock-In via BSL")
    print(f" Port: {args.port} @ {args.baud} baud")
    print("=" * 70)

    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.05)
    except Exception as e:
        sys.exit(f"[-] Could not open {args.port}: {e}")

    print("[*] Waiting for ST10 BSL handshake (0x00 -> 0xD5)...")
    print("    Ensure P0L.4 is grounded and cycle 12V power if needed.")

    t0 = time.time()
    synced = False
    while time.time() - t0 < 10.0:
        ser.reset_input_buffer()
        ser.write(b"\x00")
        ser.flush()
        rx = ser.read(1)
        if rx and rx[0] == 0xD5:
            print(f"[+] Received 0xD5 ACK from ST10 BSL!")
            synced = True
            break
        time.sleep(0.01)

    if not synced:
        ser.close()
        sys.exit("[-] Timeout waiting for 0xD5 ACK from ST10.")

    time.sleep(0.005)
    ser.reset_input_buffer()

    payload = make_lockin_payload()
    print("[*] Uploading 32-byte lock-in payload...")
    ser.write(payload)
    ser.flush()
    time.sleep(0.05)
    ser.close()

    print("\n[+] SUCCESS! RAM flag 0x001119AB written and User Bootloader launched.")
    print("[*] The ECU is now locked in CAN Bootloader mode indefinitely.")
    print("[*] You may now disconnect the UART adapter and flash over CAN:")
    print("    py -3.11-32 flasher/runner.py --input haldex_flash_haldex67motion.bin --start 0x020000 --end 0x02FFFF")

if __name__ == "__main__":
    main()
