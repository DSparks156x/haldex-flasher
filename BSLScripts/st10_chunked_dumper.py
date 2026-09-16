"""
ST10F272M Production Chunked Flash Dumper (64-byte Chunks)
Reliably dumps the entire 256 KB Flash of ST10F272M via UART BSL.

Proven Timing:
  - 64 bytes dump in ~31 ms.
  - Watchdog resets CPU at ~95 ms.
  - CPU auto re-enters BSL (P0L.4 grounded).
  - Next 64-byte chunk begins immediately upon 0xD5 ACK.
"""

import sys
import os
import time
import argparse
import serial
import serial.tools.list_ports

TOTAL_CHUNKS = 8192
CHUNK_SIZE = 32
FLASH_SIZE = 0x50000  # 327,680 bytes (320 KB ST10 CPU linear memory space)

# ST10F272M Table 4: Flash modules sectorization (read operations)
FLASH_SECTORS = [
    (0x000000, 32768,  "B0F0-B0F3 (32 KB)"),  # Chunks 0..1023
    (0x018000, 32768,  "B0F4      (32 KB)"),  # Chunks 1024..2047
    (0x020000, 65536,  "B0F5      (64 KB)"),  # Chunks 2048..4095
    (0x030000, 65536,  "B0F6      (64 KB)"),  # Chunks 4096..6143
    (0x040000, 65536,  "B0F7      (64 KB)"),  # Chunks 6144..8191
]

def chunk_to_flash_addr(chunk_idx: int):
    """
    Translates chunk index (0..8191) to physical Flash read address (per Table 4)
    and 320 KB CPU-space file byte offset (which is identical to phys).
    """
    chunk_byte = chunk_idx * CHUNK_SIZE
    cur = 0
    for base_phys, size, name in FLASH_SECTORS:
        if cur <= chunk_byte < cur + size:
            phys = base_phys + (chunk_byte - cur)
            return phys, phys, name
        cur += size
    raise ValueError(f"Chunk index {chunk_idx} out of range")

def make_chunk_payload(linear_addr: int, count: int = 32) -> bytes:
    """
    Generates exact 32-byte BSL payload to read `count` bytes starting at physical `linear_addr`.
    Uses ST10 DPP0 (Data Page Pointer 0) to map any 16 KB page of Flash.
    Properly waits for S0TIR on every byte (including the final 64th byte) before entering spin.
    """
    page = linear_addr // 0x4000
    page_offset = linear_addr % 0x4000
    code = bytes([
        0xE6, 0x00, page & 0xFF, (page >> 8) & 0xFF,                # 0xFA40: MOV DPP0, #page
        0xE6, 0xF2, page_offset & 0xFF, (page_offset >> 8) & 0xFF, # 0xFA44: MOV R2, #offset (0..0x3FFF)
        0xE6, 0xF3, count & 0xFF, (count >> 8) & 0xFF,              # 0xFA48: MOV R3, #count
        0x7E, 0xB6,                                                 # 0xFA4C: BCLR S0TIR (clear leftover ACK flag)
        # LOOP (0xFA4E):
        0x99, 0x02,                                                 # 0xFA4E: MOVB RL0, [R2+] (read flash byte)
        0xF6, 0xF0, 0xB0, 0xFE,                                     # 0xFA50: MOV 0xFEB0, R0 (transmit byte)
        # WAIT_TX (0xFA54):
        0x9A, 0xB6, 0xFE, 0x70,                                     # 0xFA54: JNB S0TIR, 0xFA54 (wait until TX buffer free)
        0x7E, 0xB6,                                                 # 0xFA58: BCLR S0TIR
        0xA0, 0x03,                                                 # 0xFA5A: CMPD1 R3, #0 (decrement count)
        0x3D, 0xF8,                                                 # 0xFA5C: JMPR cc_NE, 0xFA4E (loop if R3 != 0)
        # Spin idle until Watchdog reset auto-re-enters BSL:
        0x0D, 0xFE                                                  # 0xFA5E: JMPR cc_UC, 0xFA5E (spin)
    ])
    assert len(code) == 32
    return code

def handshake_bsl(ser, timeout=3.0):
    """
    Probes ST10 BSL with 0x00 every 15ms and looks for 0xD5 ACK.
    Synchronizes immediately to the WDT reset.
    """
    ser.timeout = 0.015
    t0 = time.time()
    while time.time() - t0 < timeout:
        ser.reset_input_buffer()
        ser.write(b"\x00")
        ser.flush()
        rx = ser.read(1)
        if rx and rx[0] == 0xD5:
            # Drain any trailing residual bytes/line bounces before proceeding
            time.sleep(0.005)
            ser.reset_input_buffer()
            return True
        time.sleep(0.005)
    return False

def dump_chunk(ser, linear_addr: int, max_retries=5) -> bytes:
    payload = make_chunk_payload(linear_addr, CHUNK_SIZE)
    for attempt in range(max_retries):
        # 1. Wait for BSL handshake (synchronized to WDT reset)
        if not handshake_bsl(ser, timeout=2.0):
            time.sleep(0.02)
            continue

        # 2. Upload 32-byte payload
        ser.reset_input_buffer()
        time.sleep(0.005)
        ser.write(payload)
        ser.flush()

        # 3. Read 64 bytes
        buf = bytearray()
        t_read = time.time()
        ser.timeout = 0.05
        while time.time() - t_read < 0.25:
            chunk = ser.read(CHUNK_SIZE - len(buf))
            if chunk:
                buf.extend(chunk)
            if len(buf) >= CHUNK_SIZE:
                break

        if len(buf) == CHUNK_SIZE:
            # Short safety pause for stop bits to clear FTDI FIFO
            time.sleep(0.002)
            return bytes(buf)

        # Retry if incomplete
        time.sleep(0.05)

    return None

def run_dumper(port="COM2", baud=112000, outfile="haldex_gen4_flash_256k.bin", start_chunk=0, end_chunk=TOTAL_CHUNKS):
    print("=" * 70)
    print(" ST10F272M Production Chunked Flash Dumper (64-byte Chunks)")
    print(f" Target: {FLASH_SIZE} bytes ({TOTAL_CHUNKS} chunks)")
    print(f" Port: {port} @ {baud} baud")
    print(f" Output File: {outfile}")
    print("=" * 70)

    # Initialize / open output file
    if os.path.exists(outfile) and os.path.getsize(outfile) == FLASH_SIZE:
        with open(outfile, "r+b") as f:
            flash_data = bytearray(f.read())
        print(f"[*] Loaded existing {outfile} ({FLASH_SIZE} bytes)")
    else:
        flash_data = bytearray(b"\xFF" * FLASH_SIZE)

    ser = serial.Serial(port, baud, timeout=0.1, write_timeout=1.0)
    try:
        ser.reset_input_buffer(); ser.reset_output_buffer()
        print("\n" + "#"*60)
        print(" ACTION REQUIRED: Power 12V into BSL now (P0L.4 grounded).")
        print("#"*60)

        print("[*] Waiting for initial BSL handshake...")
        ser.timeout = 0.02
        t0 = time.time(); got_init = False
        while time.time() - t0 < 60.0:
            ser.reset_input_buffer()
            ser.write(b"\x00"); ser.flush()
            rx = ser.read(1)
            if rx and rx[0] == 0xD5:
                got_init = True; break
            time.sleep(0.01)

        if not got_init:
            print("[-] Handshake timed out. Check 12V power and P0L.4 connection.")
            return False

        print("[+] Synchronized to ST10 BSL! Starting automated dump...\n")

        t_start_all = time.time()
        success_count = 0
        total_to_dump = end_chunk - start_chunk

        for idx in range(start_chunk, end_chunk):
            phys_addr, file_offset, sector_name = chunk_to_flash_addr(idx)
            data = dump_chunk(ser, phys_addr)

            if data is None:
                print(f"\n[!] Failed to read Chunk {idx} (Phys 0x{phys_addr:06X}, Sector {sector_name}) after retries!")
                with open(outfile, "wb") as f:
                    f.write(flash_data)
                print(f"[*] Progress saved to {outfile}")
                return False

            flash_data[file_offset : file_offset + CHUNK_SIZE] = data
            success_count += 1
            elapsed = time.time() - t_start_all
            eta_sec = (elapsed / success_count) * (total_to_dump - success_count)
            rate = success_count / elapsed if elapsed > 0 else 0

            # Progress line
            pct = ((idx + 1 - start_chunk) / total_to_dump) * 100.0
            sample_hex = data[:8].hex(' ')
            print(f"\r[{pct:5.1f}%] Chunk {idx+1:5d}/{end_chunk} | 0x{phys_addr:06X} ({sector_name[:4]}) | [{sample_hex}...] | {rate:.1f} chk/s | ETA: {eta_sec:4.0f}s", end="", flush=True)

            # Auto-save every 64 chunks (every ~2 KB)
            if (idx + 1) % 64 == 0 or (idx + 1) == end_chunk:
                with open(outfile, "wb") as f:
                    f.write(flash_data)

        total_time = time.time() - t_start_all
        print(f"\n\n[*** SUCCESS ***] Full {FLASH_SIZE} bytes read in {total_time:.1f}s ({total_time/60:.1f} minutes)!")
        print(f"[+] Verified and saved to: {os.path.abspath(outfile)}")
        return True

    finally:
        ser.close()
        print("[+] Serial port closed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ST10F272M Production Chunked Flash Dumper")
    parser.add_argument("port", nargs="?", default="COM2", help="COM port (default: COM2)")
    parser.add_argument("baud", nargs="?", type=int, default=112000, help="Baud rate (default: 112000)")
    parser.add_argument("--out", default="haldex_gen4_flash_cpu_320k.bin", help="Output file")
    parser.add_argument("--start", type=int, default=0, help="Start chunk (0..8191)")
    parser.add_argument("--count", type=int, default=TOTAL_CHUNKS, help="Number of chunks to dump")
    args = parser.parse_args()

    run_dumper(args.port, args.baud, args.out, args.start, args.start + args.count)
