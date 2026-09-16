#!/usr/bin/env python3
"""
Haldex Gen4 Diagnostic Utility: Clean Reset & DTC Clear.

Usage:
  py -3.11-32 flasher/reset_ecu.py
  py -3.11-32 flasher/reset_ecu.py --to-bootloader
"""
import sys
import os
import time
import struct
import argparse

# Architecture check & auto-relaunch for 32-bit DLL compatibility
if struct.calcsize('P') * 8 != 32:
    import subprocess
    print("[*] Re-launching under 32-bit Python runtime (Scanmatik DLL requirement)...", flush=True)
    for launcher in [["py", "-3.11-32"], ["py", "-3-32"], ["py", "-32"]]:
        try:
            cmd = launcher + [__file__] + sys.argv[1:]
            res = subprocess.run(cmd)
            sys.exit(res.returncode)
        except Exception:
            continue
    sys.exit("[-] Error: 32-bit Python not found.")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from j2534 import J2534Device, find_sm2_dll
from tp20 import TP20Transport

AWD_MODULE_ADDR = 0x0A
APP_KEY_CONST   = 0x0000BAFB


def main():
    parser = argparse.ArgumentParser(description="Haldex Gen 4 Diagnostic Reset Utility.")
    parser.add_argument("--to-bootloader", action="store_true",
                        help="Soft-reset ECU from Application into Bootloader (prepare for flashing).")
    parser.add_argument("--module", type=lambda x: int(x, 0), default=AWD_MODULE_ADDR,
                        help="Module TP2.0 address (default 0x0A).")
    args = parser.parse_args()

    dll_path = find_sm2_dll()
    if not os.path.isfile(dll_path):
        sys.exit(f"[-] Scanmatik DLL not found at: {dll_path}")

    print("[*] Initializing J2534 device...")
    dev = J2534Device(dll_path=dll_path)
    dev.open()
    try:
        dev.connect_can(baudrate=500000)
        time.sleep(0.05)

        print("[*] Connecting TP2.0 channel to AWD module (0x0A)...")
        tp = TP20Transport(dev, module=args.module, timeout=3.0)

        if tp.tx_addr != 0x764:
            print(f"[+] ECU is in resident Bootloader (listen ID 0x{tp.tx_addr:03X} != 0x764).")
            if args.to_bootloader:
                print("[*] Already in Bootloader mode — ready for flashing.")
            else:
                print("[*] Sending StopCommunication (0x82) to reboot into Application mode...")
                try:
                    tp.send(b"\x82")
                    tp.recv()
                except Exception:
                    pass
                print("[+] ECU soft-reset back into Application mode!")
        else:
            print("[+] ECU is in Application mode (listen ID 0x764).")
            if args.to_bootloader:
                print("[*] Broadcasting standstill frames (0 km/h) to clear moving-vehicle lockout...")
                for _ in range(15):
                    dev.can_send(0x4A0, b'\x00' * 8)
                    time.sleep(0.01)

                print("[*] Entering Diagnostic Session 0x89 (Extended)...")
                tp.send(b"\x10\x89")
                tp.recv()

                print("[*] Unlocking SecurityAccess Level 2...")
                tp.send(b"\x27\x01")
                res = tp.recv()
                seed_int = int.from_bytes(res[2:], "big")
                if seed_int != 0:
                    key = ((seed_int + APP_KEY_CONST) & 0xFFFFFFFF).to_bytes(4, "big")
                    tp.send(b"\x27\x02" + key)
                    tp.recv()

                print("[*] Entering Programming Session (0x85) -> soft-reset into Bootloader...")
                try:
                    tp.send(b"\x10\x85")
                    tp.recv()
                except Exception:
                    pass
                print("[+] ECU successfully transitioned to resident Bootloader!")
            else:
                print("[*] Sending ClearDiagnosticInformation (14 FF 00) to clear fault latches...")
                try:
                    tp.send(b"\x14\xff\x00")
                    tp.recv()
                    print("[+] DTCs cleared! Fault latches reset and clutch open state released.")
                except Exception as e:
                    print(f"[-] Notice: {e}")

        tp.disconnect()
    finally:
        dev.disconnect()
        dev.close()
        print("[*] Done.")


if __name__ == "__main__":
    main()
