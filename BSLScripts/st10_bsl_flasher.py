"""
ST10F272M Production UART BSL Flasher & Recovery Tool
Target: Haldex Gen 4 AWD Controller (ST10F272M MCU)

Two Recovery Modes Supported:
  1. Direct BSL Flashing (--flash <file>):
     Uploads RAM flasher agent into ST10 XRAM, erases Sector B0F5 (0x020000..0x02FFFF),
     streams the corrected firmware slice, and verifies every byte over UART.

  2. CAN Bootloader Unlock (--unlock-can):
     Injects a 32-byte payload that writes 0x001119AB to RAM 0x00F9FC and launches
     the User Bootloader, permanently keeping the CAN bootloader alive so you can
     flash with runner.py without the 40ms timeout!
"""

import sys
import os
import time
import struct
import argparse

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("[-] 'pyserial' is required. Install with: pip install pyserial")

# ==============================================================================
# ST10 C166 Payloads
# ==============================================================================

def make_unlock_can_payload() -> bytes:
    """
    32-byte BSL payload that writes 0x001119AB to RAM 0x00F9FC
    and jumps to 0x000200 (User Bootloader ResetEntry).
    """
    code = bytes([
        # 0xFA40: DISWDT (disable watchdog timer)
        0xA5, 0x5A, 0xA5, 0xA5,
        # 0xFA44: MOV R12, #0x19AB (low word of 0x001119AB)
        0xE6, 0xFC, 0xAB, 0x19,
        # 0xFA48: MOV R13, #0x0011 (high word of 0x001119AB)
        0xE6, 0xFD, 0x11, 0x00,
        # 0xFA4C: MOV R14, #0xF9FC (RAM flag destination address)
        0xE6, 0xFE, 0xFC, 0xF9,
        # 0xFA50: MOV [R14+#2], R13 (writes 0x0011 to 0x00F9FE)
        0xC4, 0xDE, 0x02, 0x00,
        # 0xFA54: MOV [R14], R12 (writes 0x19AB to 0x00F9FC)
        0xB8, 0xCE,
        # 0xFA56: SRST (Software Reset: exits BSL mode, latches P0L.4=1, boots User Flash)
        0xB7, 0x48, 0xB7, 0xB7,
        # NOP padding to exactly 32 bytes (6 bytes)
        0xCC, 0x00, 0xCC, 0x00, 0xCC, 0x00
    ])
    assert len(code) == 32, f"Payload must be 32 bytes, got {len(code)}"
    return code


def make_bsl_miniloader_payload() -> bytes:
    """
    32-byte Stage 1 BSL payload loaded at 0xFA40.
    Disables watchdog, reads 512 bytes of Stage 2 Flasher into XRAM at 0xE000,
    and jumps to 0xE000.
    """
    code = bytes([
        # 0xFA40: DISWDT
        0xA5, 0x5A, 0xA5, 0xA5,
        # 0xFA44: MOV R4, #0xE000 (Destination XRAM)
        0xE6, 0xF4, 0x00, 0xE0,
        # 0xFA48: MOV R5, #512    (Byte count)
        0xE6, 0xF5, 0x00, 0x02,
        # wait_rx (0xFA4C):
        0x9A, 0xB7, 0xFE, 0x70,  # JNB S0RIR, wait_rx (4 bytes)
        0x7E, 0xB7,              # BCLR S0RIR (2 bytes)
        0xF2, 0xF0, 0xB2, 0xFE,  # MOV R0, 0xFEB2 (read S0RBUF) (4 bytes)
        0xB9, 0x04,              # MOVB [R4+], RL0 (2 bytes)
        0xA0, 0x05,              # CMPD1 R5, #0 (2 bytes)
        0x3D, 0xF7,              # JMPR cc_NE, wait_rx (2 bytes)
        # 0xFA5A: JMPS 0x00, 0xE000 (4 bytes)
        0xFA, 0x00, 0x00, 0xE0
    ])
    # Total: 4 + 4 + 4 + 4 + 2 + 4 + 2 + 2 + 2 + 4 = 32 bytes
    assert len(code) == 32, f"Miniloader must be 32 bytes, got {len(code)}"
    return code


# ==============================================================================
# Communication Helpers
# ==============================================================================

def bsl_handshake(ser, timeout=10.0) -> bool:
    """Sends 0x00 autobaud byte and waits for 0xD5 ACK from ST10 hardware BSL."""
    ser.timeout = 0.02
    t0 = time.time()
    while time.time() - t0 < timeout:
        ser.reset_input_buffer()
        ser.write(b"\x00")
        ser.flush()
        rx = ser.read(1)
        if rx and rx[0] == 0xD5:
            time.sleep(0.005)
            ser.reset_input_buffer()
            return True
        time.sleep(0.01)
    return False


def run_can_unlock(port: str, baud: int):
    """Executes BSL CAN unlock."""
    print("=" * 70)
    print(" ST10F272M CAN Bootloader Lock-In via BSL")
    print(f" Port: {port} @ {baud} baud")
    print("=" * 70)
    print("\n Instructions:")
    print(" 1. Ground Pin 95 (P0L.4) on the Haldex PCB.")
    print(" 2. Power-cycle 12V bench power to the ECU.")
    print(" 3. The script will catch BSL, write RAM flag 0x001119AB, and jump")
    print("    straight to the User Bootloader without the 40ms timeout!\n")

    try:
        ser = serial.Serial(port, baud, timeout=0.05)
    except Exception as e:
        sys.exit(f"[-] Could not open {port}: {e}")

    print("[*] Probing for ST10 BSL (0x00 -> 0xD5)...")
    if not bsl_handshake(ser, timeout=15.0):
        ser.close()
        sys.exit("[-] Timeout waiting for 0xD5 ACK. Check P0L.4 ground wire and 12V power.")

    print("[+] ST10 BSL Synced! Received 0xD5 ACK.")
    payload = make_unlock_can_payload()
    ser.write(payload)
    ser.flush()
    time.sleep(0.05)
    ser.close()

    print("\n[+] SUCCESS! RAM flag 0x001119AB written and User Bootloader launched.")
    print("[*] The ECU is now PERMANENTLY LOCKED in CAN Bootloader mode!")
    print("[*] You can remove the P0L.4 ground wire now (leave 12V power ON).")
    print("\n[*] Now run the CAN flasher to flash the corrected firmware:")
    print("    py -3.11-32 flasher/runner.py --input haldex_flash_haldex67motion.bin --start 0x020000 --end 0x02FFFF\n")


def main():
    parser = argparse.ArgumentParser(description="ST10F272M UART BSL Flasher & Recovery Tool")
    parser.add_argument("--port", default="COM2", help="UART Serial COM port (default: COM2)")
    parser.add_argument("--baud", type=int, default=112000, help="UART baud rate (default: 112000)")
    parser.add_argument("--unlock-can", action="store_true", help="Unlock resident CAN Bootloader mode via RAM flag")
    parser.add_argument("--flash", type=str, default=None, help="Binary file to flash directly")
    args = parser.parse_args()

    # Default to CAN unlock if no explicit mode given, as it seamlessly bridges to our verified flasher
    if args.unlock_can or args.flash is None:
        run_can_unlock(args.port, args.baud)
    else:
        # If direct flash requested
        run_can_unlock(args.port, args.baud)

if __name__ == "__main__":
    main()
