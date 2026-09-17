#!/usr/bin/env python3
"""
Haldex Gen4 (0BR907554A sw3016) OBD Flasher — J2534 / Scanmatik SM2 Pro variant.

Comprehensive logging, frame tracing, and diagnostics for bench and in-car flashing.
"""
import sys
import os
import time
import struct
import logging
from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json

# Support both `python -m flasher.haldex_flash` and direct script invocation.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flasher.tp20 import TP20Transport, MessageTimeoutError
from flasher.haldex_patcher import (calculate_c5_sum, application_checksums,
                                   APP_SECTORS, validate_image, selected_blocks, patch_firmware)
# ---- Setup Logger ---------------------------------------------------------
logger = logging.getLogger("HaldexFlasher")
logger.setLevel(logging.DEBUG)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_fmt = logging.Formatter("[%(asctime)s.%(msecs)03d] %(message)s", datefmt="%H:%M:%S")
console_handler.setFormatter(console_fmt)
logger.addHandler(console_handler)

#: Set by enable_file_logging() when --log is passed; None means console only.
log_filename = None


def enable_file_logging() -> str:
    """Start writing a DEBUG-level session log. Off unless --log is given.

    Every run used to create a file whether anyone wanted one or not, which
    buried flasher/logs/ in hundreds of transcripts of runs nobody was going
    to read. Console output is unchanged either way; this only adds the file,
    which is the thing worth having when a flash goes wrong and worth not
    having the rest of the time.
    """
    global log_filename
    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_filename = os.path.join(
        logs_dir, f"flasher_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    file_handler = logging.FileHandler(log_filename, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter("[%(asctime)s.%(msecs)03d] [%(levelname)s] %(message)s",
                                 datefmt="%Y-%m-%d %H:%M:%S")
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)
    return log_filename


# ---- ECU-specific constants -----------------------------------------------
AWD_MODULE_ADDR   = 0x0A        # AWD TP2.0 logical addr (VCDS "22" is the display name); setup reply on 0x20A
APP_KEY_CONST     = 0x0000BAFB  # app SecurityAccess level 1->2: key = seed + const
LOADER_SEED       = bytes([0x11, 0x22, 0x33, 0x44])  # fixed, informational
LOADER_KEY        = bytes([0x11, 0x22, 0xEE, 0x3F])  # fixed key the loader expects

SESSION_EXTENDED   = 0x89
SESSION_PROGRAMMING = 0x85
SA_APP_REQUEST_SEED = 0x01
SA_APP_SEND_KEY     = 0x02
SA_LDR_REQUEST_SEED = 0x01
SA_LDR_SEND_KEY     = 0x02
RC_ERASE    = 0xC4
RC_CHECKSUM = 0xC5
IDENT_ECU_IDENT    = 0x9B
IDENT_STATUS_FLASH = 0x9C

ERASE_STAMP = bytes([0x20, 0x26, 0x01, 0x01, 0x00, 0x01])
CHUNK_SIZE = 240

DEFAULT_START = 0x018000
DEFAULT_END   = 0x04FFFF

KWP_NRC = {
    0x10: "GeneralReject",
    0x11: "ServiceNotSupported",
    0x12: "SubFunctionNotSupported / InvalidFormat",
    0x21: "Busy - RepeatRequest",
    0x22: "ConditionsNotCorrect or RequestSequenceError",
    0x23: "RoutineNotComplete",
    0x31: "RequestOutOfRange",
    0x33: "SecurityAccessDenied",
    0x35: "InvalidKey",
    0x36: "ExceedNumberOfAttempts (Lockout Active)",
    0x37: "RequiredTimeDelayNotExpired (Penalty Timer Running)",
    0x40: "DownloadNotAccepted",
    0x42: "CantDownloadToSpecifiedAddress",
    0x43: "CantDownloadNumberOfBytesRequested",
    0x71: "TransferSuspended",
    0x72: "TransferAborted",
    0x74: "IllegalAddressInBlockTransfer",
    0x75: "IllegalByteCountInBlockTransfer",
    0x77: "BlockTransferDataChecksumError",
    0x78: "RequestCorrectlyReceived - ResponsePending",
}


def describe_kwp(req: bytes) -> str:
    """Returns human-readable description for KWP request"""
    if not req:
        return "Empty"
    sid = req[0]
    if sid == 0x10 and len(req) >= 2:
        return f"StartDiagSession (0x{req[1]:02X})"
    elif sid == 0x27 and len(req) >= 2:
        sub = req[1]
        desc = "RequestSeed" if sub % 2 != 0 else "SendKey"
        return f"SecurityAccess {desc} (sub=0x{sub:02X})"
    elif sid == 0x1A and len(req) >= 2:
        sub = req[1]
        sub_name = {
            0x9B: "ECU_IDENT (0x9B)",
            0x9C: "STATUS_FLASH (0x9C)",
            0x90: "VIN (0x90)",
            0x97: "SYSTEM_NAME (0x97)",
        }.get(sub, f"id=0x{sub:02X}")
        return f"ReadEcuIdent ({sub_name})"
    elif sid == 0x34:
        return "RequestDownload"
    elif sid == 0x36:
        return f"TransferData ({len(req)-1} bytes)"
    elif sid == 0x37:
        return "RequestTransferExit"
    elif sid == 0x31 and len(req) >= 2:
        return f"StartRoutine (0x{req[1]:02X})"
    elif sid == 0x33 and len(req) >= 2:
        return f"RoutineResults (0x{req[1]:02X})"
    elif sid == 0x11:
        return "ECUReset"
    elif sid == 0x3E:
        return "TesterPresent"
    return f"SID 0x{sid:02X}"


class Kwp:
    def __init__(self, tp: TP20Transport, debug: bool = True):
        self.tp = tp
        self.debug = debug

    def raw(self, req: bytes) -> bytes:
        desc = describe_kwp(req)
        logger.info(f"[KWP TX] {req.hex()} ({desc})")
        self.tp.send(req)
        resp = self.tp.recv()
        deadline = time.monotonic() + 30.0
        busy_count = 0
        pending_count = 0
        while resp and resp[0] == 0x7F:
            if len(resp) != 3 or resp[1] != req[0]:
                raise RuntimeError("Malformed or mismatched KWP negative response")
            if resp[2] == 0x78:
                pending_count += 1
                if time.monotonic() >= deadline or pending_count > 30:
                    raise TimeoutError("KWP pending deadline exceeded")
                resp = self.tp.recv()  # Pending means wait; never repeat a write.
                continue
            if resp[2] == 0x21 and req[0] in (0x1A, 0x33) and busy_count < 3:
                busy_count += 1
                time.sleep(0.2)
                self.tp.send(req)
                resp = self.tp.recv()
                continue
            break
        logger.info(f"[KWP RX] {resp.hex()}")

        if resp and resp[0] == 0x7F:
            service_id = resp[1] if len(resp) > 1 else -1
            nrc = resp[2] if len(resp) > 2 else -1
            nrc_desc = KWP_NRC.get(nrc, f"Unknown (0x{nrc:02X})")
            err_msg = f"Negative response to {desc} (SID 0x{service_id:02X}): NRC 0x{nrc:02X} ({nrc_desc})"
            logger.error(f"[KWP NEGATIVE RESPONSE] {err_msg}")
            raise RuntimeError(err_msg)

        if not resp or resp[0] != ((req[0] + 0x40) & 0xFF):
            raise RuntimeError(f"Unexpected positive KWP response: {resp.hex()}")
        if req[0] in (0x10, 0x27, 0x1A, 0x31, 0x33, 0x11):
            if len(resp) < 2 or resp[1] != req[1]:
                raise RuntimeError("KWP subfunction/routine echo mismatch")
        if req[0] == 0x27:
            if req[1] & 1 and len(resp) != 6:
                raise RuntimeError("Security seed must be four bytes")
            if not req[1] & 1 and resp != bytes([0x67, req[1], 0x34]):
                raise RuntimeError("Security key was not accepted (expected status 0x34)")
        if req[0] == 0x33 and len(resp) != 3:
            raise RuntimeError("Routine result must contain exactly one status byte")
        if req[0] == 0x31:
            expected = {RC_ERASE: b"\x71\xc4\x01", RC_CHECKSUM: b"\x71\xc5"}.get(req[1])
            if expected is None or resp != expected:
                raise RuntimeError("Unexpected routine-start response/status")
        if req[0] == 0x10:
            expected = {
                SESSION_EXTENDED: b"\x50\x89",
                SESSION_PROGRAMMING: b"\x50\x85\x01",
            }.get(req[1])
            if expected is None or resp != expected:
                raise RuntimeError("Unexpected session response/status")
        if req[0] in (0x36, 0x37, 0x20, 0x82) and len(resp) != 1:
            raise RuntimeError("Unexpected KWP response length")
        return resp

    def session(self, s):              return self.raw(bytes([0x10, s]))
    def sa_seed(self, sub):            return self.raw(bytes([0x27, sub]))[2:]
    def sa_key(self, sub, key):        return self.raw(bytes([0x27, sub]) + key)
    def read_ecu_ident(self, ident):   return self.raw(bytes([0x1A, ident]))
    def request_download(self, addr, size):
        a = struct.pack(">I", addr)[1:]
        s = struct.pack(">I", size)[1:]
        r = self.raw(bytes([0x34]) + a + b"\x00" + s)
        if len(r) != 2 or r[1] < 5:
            raise RuntimeError("Invalid RequestDownload block limit")
        return r[1]
    def transfer(self, data):          return self.raw(bytes([0x36]) + data)
    def transfer_exit(self):           return self.raw(bytes([0x37]))
    def routine(self, rid, data=b""):  return self.raw(bytes([0x31, rid]) + data)
    def routine_result(self, rid):     return self.raw(bytes([0x33, rid]))
    def ecu_reset(self):               return self.raw(bytes([0x11, 0x01]))
    def tester_present(self):
        try:
            self.tp.can_send(b"\xa3")
            self.tp.can_recv()
        except Exception:
            pass


def a3(addr):
    """24-bit big-endian address field"""
    return struct.pack(">I", addr)[1:]


def reconnect(dev, module: int, tries: int = 15, heartbeat: bool = False) -> TP20Transport:
    """Reconnect TP2.0 channel after an ECU reset into loader or application."""
    logger.info(f"[*] Reconnecting TP2.0 to module 0x{module:02X} (up to {tries} attempts)...")
    for i in range(tries):
        time.sleep(1.0)
        dev.can_clear(0xFFFF)
        try:
            tp = TP20Transport(dev, module=module, timeout=0.8, debug=True, log_fn=logger.debug)
            tp.keepalive_after_response = heartbeat
            logger.info(f"  [+] Reconnected on attempt {i+1}!")
            return tp
        except Exception as e:
            logger.debug(f"  [reconnect attempt {i+1}/{tries}] {e}")
    raise RuntimeError(f"Could not reopen TP2.0 channel to module 0x{module:02X} after {tries} attempts")


def parse_flash_date(raw: bytes) -> str:
    """Decode 4-byte flash date stamp (e.g. bytes([0x20, 0x26, 0x01, 0x01])) to YYYY-MM-DD."""
    if len(raw) < 4:
        return raw.hex()
    if raw[:4] in (b'\x00\x00\x00\x00', b'\xff\xff\xff\xff'):
        return "None (unprogrammed)"
    # Check if bytes are valid BCD digits (0-9 per nibble)
    if all((b >> 4) <= 9 and (b & 0x0F) <= 9 for b in raw[:4]):
        yyyy = f"{raw[0]:02x}{raw[1]:02x}"
        mm = f"{raw[2]:02x}"
        dd = f"{raw[3]:02x}"
        return f"{yyyy}-{mm}-{dd}"
    return raw[:4].hex()


def check_flash_counter(kwp: Kwp) -> dict:
    """
    Read flash counter, attempts, flash status, and last flash date
    from ECU via KWP2000 ReadECUIdentification (0x1A 0x9C STATUS_FLASH and 0x1A 0x9B ECU_IDENT).
    Returns a dictionary of parsed diagnostic info.
    """
    info = {
        "flash_status": None,
        "flash_counter": None,
        "flash_attempts": None,
        "last_flash_date": None,
        "last_tool_id": None,
        "factory_date": None,
        "part_number": None,
        "sw_version": None,
        "system_desc": None,
    }

    # 1. Query STATUS_FLASH (0x1A 0x9C)
    logger.info("\n[*] Reading Flash Status & Programming Counter (0x1A 0x9C / STATUS_FLASH)...")
    try:
        res_9c = kwp.read_ecu_ident(IDENT_STATUS_FLASH)
        payload_9c = res_9c[2:] if len(res_9c) >= 2 else res_9c
        info["raw_9c"] = payload_9c.hex()
        logger.info(f"  - Flash Status Raw (0x9C): 0x{payload_9c.hex()}")

        if len(payload_9c) >= 3:
            status_byte = payload_9c[0]
            success_count = payload_9c[1]
            attempt_count = payload_9c[2]
            info["flash_status"] = status_byte
            info["flash_counter"] = success_count
            info["flash_attempts"] = attempt_count

            status_desc = "Flash Valid / Normal" if status_byte in (0, 1) else f"Status 0x{status_byte:02X}"
            logger.info(f"  - Flash Status:             0x{status_byte:02X} ({status_desc})")
            logger.info(f"  - Flash Counter:            {success_count} successful programming cycles")
            logger.info(f"  - Flash Attempts:           {attempt_count} programming attempts")

        if len(payload_9c) >= 10:
            date_raw = payload_9c[4:8]
            tool_raw = payload_9c[8:10]
            date_str = parse_flash_date(date_raw)
            tool_id = int.from_bytes(tool_raw, "big")
            info["last_flash_date"] = date_str
            info["last_tool_id"] = tool_id
            logger.info(f"  - Last Flash Date:          {date_str}")
            logger.info(f"  - Last Flash Tool / Equip:  {tool_id:04d} (0x{tool_raw.hex()})")

        if len(payload_9c) >= 18:
            prod_date = payload_9c[10:18].decode("ascii", errors="replace").strip("\x00 ").replace(" ", "")
            info["factory_date"] = prod_date
            note = " (cleared to 00.00.00 by post-flash handshake; see DID 0x50 below)" if prod_date == "00.00.00" else ""
            logger.info(f"  - Flash Production Date:    {prod_date}{note}")
    except Exception as e:
        logger.warning(f"  - STATUS_FLASH (0x9C) query failed: {e}")

    # 2. Inspect ECU_IDENT (0x1A 0x9B) for identification, SW version, and cross-verification
    logger.info("\n[*] Reading ECU Identification & Flash Stamp (0x1A 0x9B / ECU_IDENT)...")
    try:
        res_9b = kwp.read_ecu_ident(IDENT_ECU_IDENT)
        payload_9b = res_9b[2:] if len(res_9b) >= 2 else res_9b
        info["raw_9b"] = payload_9b.hex()

        part_no = payload_9b[:12].decode("ascii", errors="replace").strip() if len(payload_9b) >= 12 else ""
        sw_ver = payload_9b[12:16].decode("ascii", errors="replace").strip() if len(payload_9b) >= 16 else ""
        sys_desc = payload_9b[26:].decode("ascii", errors="replace").strip() if len(payload_9b) >= 26 else ""
        info["part_number"] = part_no
        info["sw_version"] = sw_ver
        info["system_desc"] = sys_desc

        logger.info(f"  - ECU Part Number:          {part_no}")
        logger.info(f"  - Software Version:         {sw_ver}")
        if sys_desc:
            logger.info(f"  - System Designation:       {sys_desc}")

        if len(payload_9b) >= 20:
            # Bytes 16..19 contain the legacy factory/dealer flash block from EEPROM 0x140.
            # Unlike 0x9C (which the CAN bootloader increments on each flash at EEPROM 0x2A1),
            # EEPROM 0x140 is only written by official dealer/ODIS flash sessions.
            cnt_raw = payload_9b[16:20]
            cnt_attempts = int.from_bytes(cnt_raw[0:2], "little") if cnt_raw[1] == 0 else int.from_bytes(cnt_raw[0:2], "big")
            cnt_success = int.from_bytes(cnt_raw[2:4], "big")
            info["ident_attempts"] = cnt_attempts
            info["ident_success"] = cnt_success
            logger.info(f"  - Dealer/Factory Block (0x9B): {cnt_success} successful / {cnt_attempts} attempts (static EEPROM 0x140, raw: {cnt_raw.hex()})")

        if len(payload_9b) >= 26:
            stamp_date = parse_flash_date(payload_9b[20:24])
            stamp_tool = int.from_bytes(payload_9b[24:26], "big")
            info["ident_date"] = stamp_date
            info["ident_tool"] = stamp_tool
            logger.info(f"  - Last Flash Date (0x9B):   {stamp_date} (Tool ID: {stamp_tool:04d})")

    except Exception as e:
        logger.warning(f"  - ECU_IDENT (0x9B) query failed: {e}")

    # 3. Inspect Permanent Factory Hardware Production Stamp (DID 0x50)
    try:
        res_50 = kwp.raw(bytes([0x21, 0x50]))
        if len(res_50) >= 4 and res_50[0] == 0x61 and res_50[1] == 0x50:
            payload_50 = res_50[4:] if res_50[2] == 0x5F else res_50[2:]
            hw_tracking = payload_50.decode("ascii", errors="replace").strip()
            info["hw_tracking"] = hw_tracking
            logger.info(f"  - Factory HW Tracking (DID 0x50): {hw_tracking}")
    except Exception as e:
        logger.debug(f"  - DID 0x50 query: {e}")

    return info


def run_ident_only(dev, module: int):
    """Safe read-only check of ECU communication, identification, flash counters, and SecurityAccess."""
    logger.info("======================================================================")
    logger.info(" ECU Identification, Flash Status & Security Access Test")
    logger.info("======================================================================")
    tp = reconnect(dev, module=module, tries=5, heartbeat=True)
    kwp = Kwp(tp, debug=True)

    is_loader = False
    if tp.tx_addr != 0x764:
        is_loader = True
        logger.info(f"  - Active Mode: Resident Bootloader (ECU listen ID 0x{tp.tx_addr:03X} != 0x764)")

    # Read flash counters, status, dates, and ECU identification
    flash_info = check_flash_counter(kwp)

    if flash_info.get("system_desc") and "B_111545" in flash_info["system_desc"]:
        is_loader = True

    time.sleep(0.05)

    if is_loader:
        logger.info("\n[*] ECU is in resident Bootloader mode (B_111545). Testing Loader SecurityAccess...")
        seed = kwp.sa_seed(SA_LDR_REQUEST_SEED)
        logger.info(f"  - Loader Seed: {seed.hex()} (expected: {LOADER_SEED.hex()})")
        kwp.sa_key(SA_LDR_SEND_KEY, LOADER_KEY)
        logger.info("  [+] Loader SecurityAccess UNLOCKED SUCCESSFULLY!")
    else:
        logger.info("\n[*] Entering Diagnostic Session 0x89 (Extended)...")
        kwp.session(SESSION_EXTENDED)
        time.sleep(0.05)

        logger.info("[*] Requesting SecurityAccess Level 2 Seed (0x27 0x01)...")
        seed = kwp.sa_seed(SA_APP_REQUEST_SEED)
        seed_int = int.from_bytes(seed, "big")
        logger.info(f"  - Received Seed: 0x{seed.hex()} (integer: 0x{seed_int:08X})")

        if seed_int == 0:
            logger.info("  - ECU indicates SecurityAccess is ALREADY UNLOCKED (seed is 0).")
        else:
            calc_key = ((seed_int + APP_KEY_CONST) & 0xFFFFFFFF).to_bytes(4, "big")
            logger.info(f"  - Calculated Key: 0x{calc_key.hex()} (Seed + 0x{APP_KEY_CONST:04X})")
            time.sleep(0.05)
            logger.info("[*] Submitting SecurityAccess Key (0x27 0x02)...")
            kwp.sa_key(SA_APP_SEND_KEY, calc_key)
            logger.info("  [+] SecurityAccess UNLOCKED SUCCESSFULLY!")

    time.sleep(0.05)
    tp.disconnect()
    logger.info("\n[*** SUCCESS ***] ECU communication, TP2.0 channel, and SecurityAccess verified!")

def prepare_image(args):
    """Validate and patch selected sectors before acquiring the adapter."""
    img = bytearray(Path(args.input).read_bytes())
    validate_image(img)
    result = patch_firmware(img, harden_traps=True,
                           simulator_mode=args.simulator_mode,
                           start=args.start, end=args.end)
    logger.info("[*] Selected-sector checksums updated; anti-brick sector %s.",
                "included" if result['routine_installed'] else "outside selected range")
    logger.info("[*] Prepared 320 KiB image SHA-256: %s", sha256(img))
    return bytes(img)


def run_flash(args, dev, img):
    foff = args.start

    size = args.end - args.start + 1
    region = img[foff:foff + size]
    if len(region) != size:
        sys.exit(f"[-] Sliced region [{foff:#x}..{foff+size:#x}) exceeds file size ({len(img):#x} bytes)")

    checksum = calculate_c5_sum(region)

    logger.info("======================================================================")
    logger.info(" Haldex Gen4 OBD Flasher")
    logger.info("======================================================================")
    logger.info(f"  Log File:      {log_filename or 'none (pass --log to write one)'}")
    logger.info(f"  CAN adapter:   {dev.description if dev else 'None (dry run)'}")
    logger.info(f"  Target Module: 0x{args.module:02X} (AWD)")
    logger.info(f"  ECU Flash:     0x{args.start:06X}..0x{args.end:06X}  ({size} bytes)")
    logger.info(f"  Source Offset: 0x{foff:06X} (from '{args.input}')")
    logger.info(f"  Additive Sum:  0x{checksum:04X}")
    logger.info("======================================================================")

    if args.dry_run:
        logger.info("[*] Dry run requested. Validated slice and checksum. No CAN traffic sent.")
        return

    confirm = input("\n[?] Type 'YES' to begin flashing the ECU: ").strip()
    if confirm != "YES":
        logger.info("Aborted by user.")
        return
        
    # Step 1: Connect TP2.0 in App mode (or detect active Bootloader)
    logger.info("\n[1] TP2.0 connect...")
    tp = reconnect(dev, module=args.module, tries=5, heartbeat=True)
    kwp = Kwp(tp, debug=True)

    time.sleep(0.1)
    in_loader = False
    if tp.tx_addr != 0x764:
        in_loader = True
        logger.info(f"    [+] ECU is in active Bootloader mode (listen ID: 0x{tp.tx_addr:03X} != 0x764) — skipping app prologue.")
    else:
        try:
            ident_res = kwp.read_ecu_ident(0x9B)
            if b"B_111545" in ident_res:
                in_loader = True
                logger.info("    [+] ECU identified as active Bootloader (B_111545) — skipping app prologue.")
        except Exception as e:
            logger.debug(f"    (ident probe: {e})")

    if not in_loader:
        logger.info("    Entering Diagnostic Session 0x89 (Extended)...")
        kwp.session(SESSION_EXTENDED)

        # Step 2: App SecurityAccess level 2
        logger.info(f"[2] App SecurityAccess level 2 (key = seed + 0x{APP_KEY_CONST:04X})...")
        seed = kwp.sa_seed(SA_APP_REQUEST_SEED)
        seed_int = int.from_bytes(seed, "big")
        if seed_int != 0:
            key = ((seed_int + APP_KEY_CONST) & 0xFFFFFFFF).to_bytes(4, "big")
            logger.info(f"    Seed {seed.hex()} -> Submitting key {key.hex()}")
            kwp.sa_key(SA_APP_SEND_KEY, key)
            logger.info("    [+] App SecurityAccess unlocked!")
        else:
            logger.info("    ECU already unlocked (seed is 0).")

        # Step 3: Programming Session 0x85 -> Soft Reset into resident Bootloader
        logger.info("[3] Entering Programming Session (0x85) -> ECU soft resets into Bootloader...")
        for attempt in range(3):
            try:
                # The application may replace/close TP immediately after the
                # programming-session response; do not heartbeat this edge.
                tp.keepalive_after_response = False
                kwp.session(SESSION_PROGRAMMING)
                break
            except Exception as e:
                err_str = str(e)
                if "NRC 0x11" in err_str and attempt < 2 and dev:
                    logger.warning("    [!] ECU rejected 0x85 (NRC 0x11: Moving lockout). Sending standstill frames...")
                    try:
                        for _ in range(10):
                            dev.can_send(0x4A0, b'\x00' * 8)
                            time.sleep(0.01)
                    except Exception:
                        pass
                    time.sleep(0.05)
                    continue
                else:
                    logger.info(f"    (Expected link teardown on reset: {e})")
                    break

        # Step 4: Reconnect to Loader
        logger.info("[4] Reconnecting TP2.0 channel to resident Bootloader...")
        tp = reconnect(dev, module=args.module, tries=15, heartbeat=True)
        kwp = Kwp(tp, debug=True)
        if tp.tx_addr == 0x764:
            raise RuntimeError(
                "ECU is still in Application mode (listen ID 0x764)! "
                "Programming Session (0x85) soft-reset did not take effect. "
                "The ECU may have rejected reset due to non-zero wheel speed. "
                "Ensure the ECU is stationary before retrying."
            )
        logger.info("    [+] Bootloader TP2.0 channel established!")
    else:
        logger.info("    [+] ECU is already running in resident Bootloader mode!")

    # Step 5: Loader SecurityAccess (Fixed Seed/Key)
    logger.info("[5] Loader SecurityAccess (Fixed challenge/response)...")
    ldr_seed = kwp.sa_seed(SA_LDR_REQUEST_SEED)
    logger.info(f"    Loader reported seed: {ldr_seed.hex()} (expected: {LOADER_SEED.hex()})")
    if ldr_seed != LOADER_SEED:
        raise RuntimeError(f"Unexpected loader challenge: {ldr_seed.hex()}")
    kwp.sa_key(SA_LDR_SEND_KEY, LOADER_KEY)
    logger.info("    [+] Loader unlocked successfully!")

    # Step 6: RequestDownload
    logger.info(f"[6] RequestDownload: Addr 0x{args.start:06X}, Size {size} bytes...")
    blk = kwp.request_download(args.start, size)
    logger.info(f"    Loader accepted download! Block size limit: {blk}")
    # CRITICAL: Clamp chunk size to a 4-byte (32-bit word) boundary!
    # If chunk size is odd (e.g. 145), the ECU adds a 0xFF padding byte per chunk,
    # which causes flash write address drift and checksum corruption!
    # The loader limit includes the TransferData service byte.
    max_chunk = min(CHUNK_SIZE, blk - 1)
    chunk = (max_chunk // 4) * 4
    if chunk < 4:
        raise RuntimeError("Loader transfer size is too small")
    logger.info(f"    Configured 4-byte aligned chunk size: {chunk} bytes")

    # Step 7: Erase routine 0xC4
    logger.info("[7] Executing Erase Routine (0xC4)...")
    kwp.routine(RC_ERASE, a3(args.start) + a3(args.end) + ERASE_STAMP)

    logger.info("    Waiting for erase to complete (polling 0xC4)...")
    r = b""
    for i in range(30):
        time.sleep(0.5)
        try:
            r = kwp.routine_result(RC_ERASE)
            if r[-1:] == b"\x00":
                break
        except TimeoutError as e:
            logger.debug(f"    (erase poll {i}: {e})")
            tp = reconnect(dev, module=args.module, tries=5, heartbeat=True)
            kwp = Kwp(tp, debug=True)
        except RuntimeError as e:
            if "NRC 0x23" not in str(e):
                raise
            logger.debug(f"    (erase poll {i}: {e})")

    logger.info(f"    Erase response: {r.hex()}")
    if r[-1:] != b"\x00":
        raise RuntimeError(f"Flash erase routine failed! Response: {r.hex()}")
    logger.info("    [+] Sector erase confirmed!")

    # Step 8: TransferData
    logger.info(f"[8] TransferData: Writing {size} bytes in {chunk}-byte chunks...")
    buf = region
    total = len(buf)
    sent = 0
    t0 = time.time()
    while buf:
        curr = buf[:chunk]
        kwp.transfer(curr)
        buf = buf[chunk:]
        sent += len(curr)
        pct = (sent / total) * 100.0
        elapsed = time.time() - t0
        speed = (sent / elapsed) if elapsed > 0 else 0
        print(f"\r    [{pct:5.1f}%] {sent}/{total} bytes ({speed:.1f} B/s)", end="", flush=True)
    print()
    logger.info("    [+] Data transfer completed!")

    # Step 9: RequestTransferExit
    logger.info("[9] RequestTransferExit (0x37)...")
    kwp.transfer_exit()
    logger.info("    [+] Transfer exited.")

    # Step 10: Checksum Verification (Routine 0xC5)
    logger.info(f"[10] Verifying Checksum Routine (0xC5, sum=0x{checksum:04X})...")
    time.sleep(0.1)
    kwp.routine(RC_CHECKSUM, a3(args.start) + a3(args.end) + struct.pack(">H", checksum))
    time.sleep(0.2)
    r = kwp.routine_result(RC_CHECKSUM)
    logger.info(f"     Checksum result: {r.hex()}")
    if r[-1:] != b"\x00":
        raise RuntimeError(f"Checksum verification failed! Result: {r.hex()}")
    logger.info("     [+] Checksum VERIFIED!")

    # Step 11: Finalize Flash Session & Reboot to Application
    logger.info("[11] Finalizing Flash Session & Rebooting ECU to Application...")
    try:
        # The commit/reset replaces the TP channel after its valid response.
        tp.keepalive_after_response = False
        # StopDiagnosticSession (0x20) verifies state 7 (checksum passed) and transitions out
        logger.info("    Sending StopDiagnosticSession (0x20)...")
        kwp.raw(bytes([0x20]))
        time.sleep(0.05)
        # StopCommunication (0x82) writes 0x02 to EEPROM 0x2A8 (committing the flash) and triggers __software_reset()
        logger.info("    Sending StopCommunication (0x82) -> commits EEPROM 0x2A8 and soft-resets...")
        kwp.raw(bytes([0x82]))
    except Exception as e:
        logger.info(f"    (Expected link teardown on reboot: {e})")

    tp.disconnect()
    logger.info("\n[*** FLASH TRANSFER & CHECKSUM COMPLETE ***]")
    logger.info("[*] Run a fresh --ident-only after releasing the adapter to check application boot.")


def run_recovery(args, dev, img):
    """High-speed CAN bootloader intercept and recovery for an ECU trapped in a reboot loop."""
    logger.info("======================================================================")
    logger.info(" Haldex Gen4 CAN Fast Bootloader Intercept & Recovery")
    logger.info("======================================================================")
    logger.info(f"  Target Module: 0x{args.module:02X} (AWD)")
    logger.info(f"  Flash Range:   0x{args.start:06X}..0x{args.end:06X}")
    logger.info(f"  Input Binary:  {args.input}")
    logger.info("======================================================================")

    foff = args.start

    size = args.end - args.start + 1
    data = img[foff:foff + size]
    if len(data) != size:
        sys.exit(f"[-] Sliced region [{foff:#x}..{foff+size:#x}) exceeds file size ({len(img):#x} bytes)")

    checksum = calculate_c5_sum(data)
    logger.info(f"  Flash Slice: {size} bytes, Additive Sum: 0x{checksum:04X}")

    print("\n" + "=" * 70)
    print(" CAN BOOTLOADER INTERCEPT INSTRUCTIONS:")
    print(" 1. Turn OFF 12V bench power to the Haldex ECU now.")
    print(" 2. Press ENTER in this terminal when power is OFF.")
    print(" 3. The tool will begin high-speed broadcast polling (0x200 every 10ms).")
    print(" 4. As soon as you see the prompt, TURN ON 12V BENCH POWER!")
    print("=" * 70 + "\n")

    input("Press ENTER when ECU 12V power is switched OFF: ")

    print("\n[*] INTERCEPT ARMED! Rapidly broadcasting 0x200...")
    print("[*] >>> TURN ON 12V POWER TO THE HALDEX ECU NOW! <<<\n", flush=True)

    dev.can_clear(0xFFFF)
    tp = TP20Transport(dev, module=args.module, timeout=30.0, debug=True, log_fn=logger.debug, intercept=True)
    kwp = Kwp(tp, debug=True)

    logger.info("\n[+] BOOTLOADER CAUGHT! Beginning immediate flash sequence (zero delay)...")

    # Step 1: Immediate Loader SecurityAccess
    logger.info("[1] Loader SecurityAccess...")
    ldr_seed = kwp.sa_seed(SA_LDR_REQUEST_SEED)
    logger.info(f"    Seed: {ldr_seed.hex()}")
    if ldr_seed != LOADER_SEED:
        raise RuntimeError(f"Unexpected loader challenge: {ldr_seed.hex()}")
    kwp.sa_key(SA_LDR_SEND_KEY, LOADER_KEY)
    logger.info("    [+] Loader unlocked successfully!")

    # Step 2: Immediate RequestDownload
    logger.info(f"[2] RequestDownload: Addr 0x{args.start:06X}, Size {size} bytes...")
    blk = kwp.request_download(args.start, size)
    max_chunk = min(CHUNK_SIZE, blk - 1)
    chunk = (max_chunk // 4) * 4
    if chunk < 4:
        raise RuntimeError("Loader transfer size is too small")
    logger.info(f"    Chunk size: {chunk} bytes")

    # Step 3: Immediate Erase Routine 0xC4
    logger.info("[3] Executing Erase Routine (0xC4)...")
    kwp.routine(RC_ERASE, a3(args.start) + a3(args.end) + ERASE_STAMP)
    logger.info("    Waiting for erase to complete (polling 0xC4)...")
    r = b""
    for i in range(30):
        time.sleep(0.5)
        try:
            r = kwp.routine_result(RC_ERASE)
            if r[-1:] == b"\x00":
                break
        except TimeoutError as e:
            logger.debug(f"    (erase poll {i}: {e})")
            tp = reconnect(dev, module=args.module, tries=5, heartbeat=True)
            kwp = Kwp(tp, debug=True)
        except RuntimeError as e:
            if "NRC 0x23" not in str(e):
                raise
            logger.debug(f"    (erase poll {i}: {e})")
    if r[-1:] != b"\x00":
        raise RuntimeError(f"Flash erase routine failed! Response: {r.hex()}")
    logger.info("    [+] Sector erased! Bootloader is now permanently locked into flash mode.")

    # Step 4: TransferData streaming
    logger.info("[4] Streaming Flash Data...")
    off = 0
    total = len(data)
    t0 = time.time()
    while off < total:
        cdata = data[off:off + chunk]
        kwp.transfer(cdata)
        off += len(cdata)
        pct = (off / total) * 100
        bps = off / max(time.time() - t0, 0.001)
        print(f"\r    Flashing: {pct:5.1f}% [{off}/{total} bytes] @ {bps:.0f} B/s", end="", flush=True)
    print()
    logger.info("    [+] Flash data written successfully!")

    # Step 5: TransferExit
    logger.info("[5] Exiting transfer...")
    kwp.transfer_exit()

    # Step 6: Verify Checksum Routine 0xC5
    logger.info(f"[6] Verifying Checksum Routine (0xC5, sum=0x{checksum:04X})...")
    time.sleep(0.1)
    kwp.routine(RC_CHECKSUM, a3(args.start) + a3(args.end) + struct.pack(">H", checksum))
    time.sleep(0.2)
    r = kwp.routine_result(RC_CHECKSUM)
    logger.info(f"     Checksum result: {r.hex()}")
    if r[-1:] != b"\x00":
        raise RuntimeError(f"Checksum verification failed! Result: {r.hex()}")
    logger.info("     [+] Checksum VERIFIED!")

    # Step 7: Finalize
    logger.info("[7] Finalizing Flash Session & Rebooting ECU...")
    try:
        tp.keepalive_after_response = False
        logger.info("    Sending StopDiagnosticSession (0x20)...")
        kwp.raw(bytes([0x20]))
        time.sleep(0.05)
        logger.info("    Sending StopCommunication (0x82)...")
        kwp.raw(bytes([0x82]))
    except Exception as e:
        logger.info(f"    (Expected link teardown on reboot: {e})")

    tp.disconnect()
    logger.info("\n[*** CAN RECOVERY FLASH COMPLETE & VERIFIED ***]")
    logger.info("[*] Cycle 12V bench power, then run --ident-only to verify the application!")


def main(argv=None):
    ap = ArgumentParser(description="Haldex Gen4 flasher and application readout")
    from flasher.runner import add_adapter_arguments, open_adapter
    add_adapter_arguments(ap)
    ap.add_argument("--dll", default=None, help="Path to J2534 DLL (defaults to Scanmatik smj2534.dll)")
    ap.add_argument("--baud", type=int, default=500000, help="CAN bus baudrate (default 500000)")
    ap.add_argument("--module", type=lambda x: int(x, 0), default=AWD_MODULE_ADDR, help="AWD TP2.0 address (default 0x0A)")
    ap.add_argument("--input", default=None, help="Flash binary image (exactly 320 KiB CPU image)")
    ap.add_argument("--start", type=lambda x: int(x, 0), default=DEFAULT_START, help="First sector start (default 0x18000)")
    ap.add_argument("--end", type=lambda x: int(x, 0), default=DEFAULT_END, help="Last sector inclusive end (default 0x4ffff)")
    ap.add_argument("--readout", action="store_true", help="Read original application flash via native TP2/KWP upload; no erase or flash")
    ap.add_argument("--out", default=None, help="Readout capture directory (must not already exist)")
    ap.add_argument("--reference", action="append", default=[], help="Compare readout against a 320 KiB CPU image; may be repeated")
    ap.add_argument("--readout-passes", type=int, choices=(1, 2), default=1, help="Readout passes (default 1 with sector checksums; optional 2 must match)")
    ap.add_argument("--readout-window", type=lambda x: int(x, 0), default=4096, help="Bytes per readout transaction (default 4096)")
    ap.add_argument("--log", action="store_true", help="Write a DEBUG-level session log to flasher/logs/. Off by default; console output is unaffected. Worth passing for any flash you might need to diagnose afterwards.")
    ap.add_argument("--verbose", "--debug", action="store_true", help="Print all raw CAN frames to console")
    ap.add_argument("--dry-run", action="store_true", help="Print plan and checksum, no CAN traffic")
    ap.add_argument("--ident-only", action="store_true", help="Safe bench test: read ECU info and test SecurityAccess without flashing")
    ap.add_argument("--recovery", action="store_true", help="High-speed CAN bootloader intercept mode for ECUs trapped in a reset loop")
    ap.add_argument("--simulator-mode", action="store_true", help="Apply bench simulator bypass patches (defeat Notlauf limp mode, pump/valve faults, clutch open)")
    args = ap.parse_args(argv)
    try:
        selected_blocks(args.start, args.end)
    except ValueError as exc:
        ap.error(str(exc))

    # Dispatch before opening a CAN adapter or invoking any flash path.
    # The reader owns its sole device and always records raw diagnostics.
    if args.readout:
        if any((args.input, args.ident_only, args.recovery, args.simulator_mode)):
            ap.error('--readout cannot be combined with flash, recovery, or identification-only options')
        start, end = args.start, args.end
        args.start = start
        args.length = end-start+1
        args.passes = args.readout_passes
        args.window = args.readout_window
        args.run = not args.dry_run
        return run_readout(args)

    if args.out or args.reference:
        ap.error('--out and --reference require --readout')

    # Before anything else, so the file captures the whole session.
    if args.log:
        enable_file_logging()

    if args.verbose:
        console_handler.setLevel(logging.DEBUG)

    if not args.ident_only and not args.input:
        sys.exit("[-] Error: --input <binary> is required unless --ident-only is specified.")

    img = prepare_image(args) if args.input else None

    if args.dry_run:
        if args.ident_only:
            logger.info("[*] Identification dry run. No CAN traffic sent.")
        else:
            run_flash(args, None, img)
        return 0

    logger.info(f"[*] Opening {args.adapter} adapter")
    dev = open_adapter(args)
    try:
        if args.ident_only:
            run_ident_only(dev, module=args.module)
        elif args.recovery:
            run_recovery(args, dev, img)
        else:
            run_flash(args, dev, img)
    finally:
        logger.info("[*] Closing CAN adapter...")
        try:
            dev.disconnect()
        except Exception:
            pass
        try:
            dev.close()
        except Exception:
            pass
        if log_filename:
            logger.info(f"[*] Done. Full session log written to: {log_filename}")
        else:
            logger.info("[*] Done.")



# ---- Native application upload / padded image capture --------------------
APP_START, APP_END = 0x18000, 0x50000  # end exclusive
IMAGE_SIZE = 0x50000
UPLOAD_KEY_ADD = 0x762B
MAX_BLOCK = 200



def sha256(data):
    return hashlib.sha256(data).hexdigest().upper()


def validate_range(start, length):
    if length <= 0 or start < APP_START or start + length > APP_END:
        raise ValueError('Only nonempty application ranges 0x018000..0x04FFFF are supported')


def upload_request(start, length):
    validate_range(start, length)
    return b'\x35' + start.to_bytes(3, 'big') + b'\x00' + length.to_bytes(3, 'big')


class ProtocolError(RuntimeError):
    pass


class ApplicationReader:
    def __init__(self, transport, record=lambda event: None):
        self.transport = transport
        self.record = record
        self.special_session = False
        self.upload_active = False

    def exchange(self, request, prefix):
        # An allowlist guards against accidentally reusing flash/programming code.
        allowed = (request in (b'\x10\x89', b'\x10\x84', b'\x27\x03',
                               b'\x36', b'\x37', b'\x20', b'\x1a\x9b')
                   or (request[:2] == b'\x27\x04' and len(request) == 6)
                   or (request[:1] == b'\x35' and len(request) == 8))
        if not allowed:
            raise ValueError('Request is outside readout allowlist: ' + request.hex())
        if request[:1] == b'\x35':
            if request[4] != 0:
                raise ValueError('Only uncompressed upload is supported')
            validate_range(int.from_bytes(request[1:4], 'big'),
                           int.from_bytes(request[5:8], 'big'))
        self.record({'event': 'request', 'hex': request.hex()})
        self.transport.send(request)
        response = self.transport.recv()
        self.record({'event': 'response', 'hex': response.hex()})
        if response[:1] == b'\x7f':
            raise ProtocolError('Negative response: ' + response.hex(' '))
        if not response.startswith(prefix):
            raise ProtocolError(f'Expected {prefix.hex()}, received {response.hex()}')
        return response[len(prefix):]

    def identify(self):
        return self.exchange(b'\x1a\x9b', b'\x5a\x9b')

    def enter(self):
        self.exchange(b'\x10\x89', b'\x50\x89')
        seed_bytes = self.exchange(b'\x27\x03', b'\x67\x03')
        if len(seed_bytes) != 4:
            raise ProtocolError('Expected exactly four seed bytes; refusing to guess')
        seed = int.from_bytes(seed_bytes, 'big')
        key = ((seed + UPLOAD_KEY_ADD) & 0xffffffff).to_bytes(4, 'big')
        # One calculated key attempt only. No brute force or automatic retries.
        self.exchange(b'\x27\x04' + key, b'\x67\x04')
        # Set before sending so an uncertain reply still causes cleanup.
        self.special_session = True
        self.exchange(b'\x10\x84', b'\x50\x84')

    def read_window(self, start, length):
        request = upload_request(start, length)
        self.upload_active = True
        answer = self.exchange(request, b'\x75')
        expected_limit = min(MAX_BLOCK, length)
        if answer != bytes([expected_limit]):
            raise ProtocolError(f'Unexpected upload block limit {answer.hex()}; expected {expected_limit}')
        data = bytearray()
        while len(data) < length:
            # Request has NO data and NO sequence byte on this KWP implementation.
            # Never retry an ambiguous 36: the ECU may already have advanced.
            block = self.exchange(b'\x36', b'\x76')
            expected = min(MAX_BLOCK, length - len(data))
            if len(block) != expected:
                raise ProtocolError(f'Upload at 0x{start+len(data):06X}: expected {expected} bytes, got {len(block)}')
            data.extend(block)
        self.exchange(b'\x37', b'\x77')
        self.upload_active = False
        return bytes(data)

    def leave(self):
        errors = []
        if self.upload_active:
            try:
                self.exchange(b'\x37', b'\x77')
                self.upload_active = False
            except Exception as exc:
                errors.append('transfer exit: ' + str(exc))
        if self.special_session:
            try:
                self.exchange(b'\x20', b'\x60')
                self.exchange(b'\x10\x89', b'\x50\x89')
                self.special_session = False
            except Exception as exc:
                errors.append('session exit: ' + str(exc))
        return errors


def compare_reference(data, start, path):
    reference = Path(path).read_bytes()
    validate_image(reference)
    expected = reference[start:start+len(data)]
    differences = [i for i, (a, b) in enumerate(zip(data, expected)) if a != b]
    sectors = []
    for low, high in APP_SECTORS:
        lo, hi = max(low, start), min(high, start+len(data))
        if lo < hi:
            actual_slice = data[lo-start:hi-start]
            reference_slice = reference[lo:hi]
            sectors.append({'start': lo, 'end_exclusive': hi,
                            'whole_sector': (lo, hi) == (low, high),
                            'equal': actual_slice == reference_slice,
                            'captured_sha256': sha256(actual_slice),
                            'reference_sha256': sha256(reference_slice)})
    return {'path': str(Path(path).resolve()), 'reference_sha256': sha256(reference),
            'equal': not differences, 'different_bytes': len(differences),
            'first_difference_addresses': [start+i for i in differences[:32]],
            'sectors': sectors}


def capture(reader, output, start, length, passes, window, record, progress,
            recover=None, max_window_retries=2):
    selected_blocks(start, start+length-1)
    if passes not in (1, 2) or not 1 <= window <= 65536:
        raise ValueError('passes must be 1 or 2; window must be 1..65536')
    output = Path(output)
    results = []
    for pass_index in range(1, passes+1):
        path = output / f'pass{pass_index}.bin'
        with path.open('xb') as stream:
            # CPU addresses are file offsets, matching the flasher's 320 KiB input.
            # Unread bytes are padding, not captured flash contents.
            stream.write(b'\xff' * IMAGE_SIZE)
            stream.flush()
            for offset in range(0, length, window):
                size = min(window, length-offset)
                for attempt in range(max_window_retries+1):
                    try:
                        block = reader.read_window(start+offset, size)
                        break
                    except (TimeoutError, ConnectionError, RuntimeError) as exc:
                        # NRCs, changed protocol, and invalid lengths are not
                        # transient transport failures; don't hide them.
                        if isinstance(exc, ProtocolError) or recover is None or attempt == max_window_retries:
                            raise
                        record({'event': 'window_restart', 'pass': pass_index,
                                'address': start+offset, 'length': size,
                                'attempt': attempt+1, 'error': str(exc)})
                        recover(exc)
                if len(block) != size:
                    raise ProtocolError('Incomplete window')
                stream.seek(start+offset)
                stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
                record({'event': 'window_saved', 'pass': pass_index,
                        'address': start+offset, 'length': size, 'sha256': sha256(block)})
                progress(pass_index, offset+size, length)
        data = path.read_bytes()
        results.append({'path': path.name, 'length': len(data), 'sha256': sha256(data),
                        'captured_start': start, 'captured_end_exclusive': start+length,
                        'captured_length': length,
                        'captured_sha256': sha256(data[start:start+length])})
    if passes == 2 and (output/'pass1.bin').read_bytes() != (output/'pass2.bin').read_bytes():
        raise ProtocolError('Independent captures differ; do not treat this as a verified dump')
    return results


def run_readout(args):
    """Capture with the same adapter owner as flash/ident/recovery."""
    from flasher.runner import open_adapter
    selected_blocks(args.start, args.start+args.length-1)
    if not 1 <= args.window <= 65536:
        raise ValueError('--readout-window must be 1..65536')
    # Validate comparison files before touching hardware.
    for path in args.reference:
        compare_reference(b'', args.start, path)
    plan = {'start': args.start, 'end_exclusive': args.start+args.length,
            'length': args.length, 'passes': args.passes, 'window': args.window,
            'image_size': IMAGE_SIZE, 'padding_byte': 255,
            'output_format': '320 KiB CPU image; file offset equals CPU address; unread bytes padded 0xFF',
            'protocol': 'TP2.0 / KWP: 10 89; 27 03/04; 10 84; 35/36/37; 20; 10 89',
            'key_add': hex(UPLOAD_KEY_ADD), 'firmware_writes': False,
            'scope': 'Application only; no bootloader, EEPROM, or memory-gap bytes',
            'hardware_access': args.run, 'adapter': args.adapter, 'bus': args.bus}
    print(json.dumps(plan, indent=2), flush=True)
    if not args.run:
        return 0
    output = Path(args.out) if args.out else Path('data/raw/readout') / ('app_dump_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    output.mkdir(parents=True, exist_ok=False)
    report = {**plan, 'status': 'incomplete', 'started_utc': datetime.now(timezone.utc).isoformat()}
    report_path = output / 'report.json'
    report_path.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    device = transport = reader = None
    error = None
    with (output/'diagnostic.jsonl').open('x', encoding='utf-8') as log:
        def record(event):
            log.write(json.dumps({'monotonic': time.monotonic(), **event})+'\n')
            log.flush()
            if event['event'] == 'window_saved':
                # Retain actual coverage even if a later window/pass fails.
                coverage = report.setdefault('saved_ranges', {})
                coverage[f"pass{event['pass']}.bin"] = {
                    'start': args.start,
                    'end_exclusive': event['address']+event['length'],
                }
        try:
            device = open_adapter(args)
            device.can_clear(0xffff)
            transport = TP20Transport(device, module=args.module, timeout=2.0, debug=True,
                                      log_fn=lambda message: record({'event': 'tp20', 'message': message}))
            transport.keepalive_after_response = True
            report['ecu_listen_id'] = transport.tx_addr
            if transport.tx_addr != 0x764:
                raise ProtocolError(f'Expected application at 0x764; received 0x{transport.tx_addr:03X}. No loader commands sent.')
            reader = ApplicationReader(transport, record)
            report['ident_before_hex'] = reader.identify().hex()
            reader.enter()
            report['transport_reconnections'] = 0
            def recover(exc):
                nonlocal transport
                # Single hardware owner, same device. Start a new channel and
                # re-issue 35 at the ORIGINAL window address; never repeat 36.
                report['transport_reconnections'] += 1
                if report['transport_reconnections'] > 8:
                    raise ProtocolError('Readout exceeded eight total transport reconnections')
                print('Reconnecting after transport interruption; restarting unfinished window.', flush=True)
                transport.disconnect()
                time.sleep(0.3)
                device.can_clear(0xffff)
                transport = TP20Transport(device, module=args.module, timeout=2.0, debug=True,
                                          log_fn=lambda message: record({'event': 'tp20', 'message': message}))
                transport.keepalive_after_response = True
                if transport.tx_addr != 0x764:
                    raise ProtocolError('Reconnected ECU is not the application')
                reader.transport = transport
                # End any previous application upload/control session first.
                reader.upload_active = True
                reader.special_session = True
                errors = reader.leave()
                if errors:
                    raise ProtocolError('Reconnect cleanup failed: '+'; '.join(errors))
                if reader.identify().hex() != report['ident_before_hex']:
                    raise ProtocolError('ECU identification changed after reconnect')
                reader.enter()
            last_progress = [0.0]
            def progress(pass_index, done, total):
                if time.monotonic()-last_progress[0] >= 10 or done == total:
                    print(f'Pass {pass_index}/{args.passes}: {done}/{total} bytes', flush=True)
                    last_progress[0] = time.monotonic()
            report['captures'] = capture(reader, output, args.start, args.length,
                                         args.passes, args.window, record, progress, recover=recover)
            report['two_pass_equal'] = args.passes == 2
            data = (output/'pass1.bin').read_bytes()[args.start:args.start+args.length]
            report['application_checksums'] = application_checksums(data, args.start)
            bad = [row for row in report['application_checksums'] if row['valid'] is False]
            if bad:
                raise ProtocolError('Application sector checksum mismatch: '+', '.join(hex(row['start']) for row in bad))
            checksums_complete = bool(report['application_checksums']) and all(
                row['complete'] for row in report['application_checksums'])
            report['comparisons'] = [compare_reference(data, args.start, path) for path in args.reference]
            report['status'] = 'captured_checksums_valid' if checksums_complete else 'captured_partial'
        except Exception as exc:
            error = str(exc)
            report['error'] = error
            report['status'] = 'failed'
            record({'event': 'failure', 'error': error})
        finally:
            if reader:
                report['cleanup_errors'] = reader.leave()
                try:
                    report['ident_after_hex'] = reader.identify().hex()
                except Exception as exc:
                    report['cleanup_errors'].append('fresh identification: '+str(exc))
                if report['cleanup_errors']:
                    error = error or '; '.join(report['cleanup_errors'])
                    report['status'] = 'requires_attention'
            for component, method in ((transport, 'disconnect'), (device, 'disconnect'), (device, 'close')):
                if component is not None:
                    try:
                        getattr(component, method)()
                    except Exception as exc:
                        error = error or str(exc)
                        report.setdefault('cleanup_errors', []).append(method+': '+str(exc))
                        report['status'] = 'requires_attention' 
            report['finished_utc'] = datetime.now(timezone.utc).isoformat()
            report_path.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(f'Report: {report_path.resolve()}', flush=True)
    if error:
        print('Readout failed: '+error, flush=True)
        return 1
    print(report['status'], flush=True)
    for comparison in report.get('comparisons', []):
        print(f"Reference {comparison['path']}: {comparison['different_bytes']} differing bytes", flush=True)
    return 0



if __name__ == "__main__":
    sys.exit(main())
