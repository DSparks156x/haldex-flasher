# Haldex Flasher
This is a project to flash and readout a Gen4 VW Haldex Controller, or probably most other modules with little adaptation. It is somewhat probably derived from pq-flasher, that EPS RE project. Testing was mostly done on a clone SM2 pro, hence the 32 bit shit, panda is untested and socketcan should work.  

It currently does not work in a car over OBD due to TP2 timing/ack issues that the gateway gets mad at, but it works directly connected to a controller. Working on fixing.

It also contains haldex_patcher.py, a script for patching a Haldex Gen4 (or at least a 0BR 3016 specifically) binary and correcting its checksums. The flasher automatically runs the patcher to correct checksums and apply anti-brick patches, though they only apply to whatever sector you flash.

The anti-brick patches trigger the haldex to drop back into bootloader ready to reflash on checksum and some other errors, rather than ending up in an unflashable bootloop. If you do not apply them, a bad checksum or some other errors will put the haldex in a bootloop, as the stock error handling routine for many things basically just freezes and lets the watchdog reset, within about 40ms. You cannot start a flash section in 40ms, and will need to open the controller and recover via BSL. See BSL folder for that. 

I have flashed this thing over 180 times, I have only had to recover via BSL once. 

Do what you want with this stuff, more to be released about the actual editing of this binary eventually. maybe. This took a lot of human time. 

The rest of this readme is AI written. It looks fine, but i didn't read it all that thoroughly. 

There is one flash/readout implementation for every CAN adapter:

- `runner.py`: selects and owns J2534, Panda, or SocketCAN; normalizes CAN frames.
- `haldex_flash.py`: shared flash, recovery, identification, and application dump flows.
- `haldex_patcher.py`: strict 320 KiB image validation, anti-brick/simulator patches, and both checksum layers.
- `tp20.py`, `kwp2000.py`, `j2534.py`: protocol and driver modules.


## Commands

On this Windows host, use the project interpreter for J2534:

```powershell
$python = 'C:\Users\raccoon\AppData\Local\Programs\Python\Python311-32\python.exe'
& $python flasher/runner.py --help
& $python flasher/runner.py --adapter j2534 --readout --dry-run
& $python flasher/runner.py --adapter j2534 --readout --start 0x30000 --end 0x3ffff --out data/raw/readout/calibration_original
& $python flasher/runner.py --adapter j2534 --input artifacts/candidate_320k.bin --start 0x18000 --end 0x4ffff --dry-run
```

`--dry-run` does not load a hardware driver, access CAN, or relaunch Python.
Real J2534 access requires 32-bit Python. `run_j2534.bat` selects the standard
per-user Python311-32 installation; set `PYTHON32` to override its path.
`--dll` overrides the J2534 DLL. Panda and SocketCAN can use 64-bit Python.

Only adapter options differ; the same operations work with either backend:

```text
python -m flasher.runner --adapter panda --bus 0 --readout --out data/raw/readout/panda_original
python -m flasher.runner --adapter socketcan --channel can0 --readout --out data/raw/readout/socketcan_original
```

Panda requires the comma.ai Panda Python package and supports `--serial` and
`--bus 0|1|2`. The runner sets its bitrate from `--baud` (default 500000),
enables all-output safety for the session, and returns it to silent mode on
close. SocketCAN requires Linux and `python-can`; configure and bring up the
named interface at the ECU bitrate before running. The runner does not change
Linux interface configuration; `--baud` controls J2534/Panda only.

## Image and readout behavior

Both flash and readout default to the full application `0x18000..0x4ffff`.
The same `--start`/`--end` flags select contiguous whole sectors for either
operation. Allowed sectors are `0x18000..0x1ffff`, `0x20000..0x2ffff`,
`0x30000..0x3ffff`, and `0x40000..0x4ffff`. Partial-sector selections are
rejected before hardware is opened. Images and comparison references must be
exactly 327680 bytes; packed 256 KiB images and file-offset overrides are no
longer supported. Readout uses one pass by default. Each
`passN.bin` is exactly 327680 bytes. CPU addresses are file offsets, the selected
range contains captured bytes, and all unread bytes are `0xff`. The report
records saved ranges even on failure; padding must never be mistaken for
captured bootloader or gap contents. Checksums and `--reference` comparisons
cover captured bytes only. Existing capture directories are refused.

For flashing, explicitly select the minimum whole sectors covering every
changed byte and its checksum. The patcher updates only selected sectors:
checksums in each selected sector, and anti-brick hooks/routine/traps when
`0x20000..0x2ffff` is selected. It changes no bytes outside the selected range. Flash preparation
uses the patcher for both ordinary and recovery paths and stops if patching
fails. `--simulator-mode` remains bench-only. The final StopCommunication ACK
commits and resets; release the adapter and run a fresh `--ident-only` to check
application boot. Obsolete flasher flags and command paths are removed.

Standalone patching and checksum commands are now in one tool:

```text
python -m flasher.haldex_patcher input.bin patched.bin
python -m flasher.haldex_patcher input.bin --verify
python -m flasher.haldex_patcher input.bin --fix 0x30000 --out corrected.bin
```

`--verify` and `--fix` are checksum-only operations and install no patches.
Verification exits nonzero on a mismatch. `--fix` without `--out` updates the
input, so use a derived output path when preserving original evidence.

## Offline regression checks

```text
python -m unittest discover -s tests -p test_app_flash_dump.py
python -m unittest discover -s tests -p test_tp20_upload_receive.py
python -m unittest discover -s tests -p test_flasher_readout_cli.py
python -m unittest discover -s tests -p test_flasher_shared.py
```

These tests exercise simulated protocol exchanges, adapter contracts, cleanup,
padding and checksum scope. They do not establish live Panda/SocketCAN timing
or constitute a post-refactor bench flash. Only the coordinating session may
own hardware, as specified in [AGENTS.md](AGENTS.md).
