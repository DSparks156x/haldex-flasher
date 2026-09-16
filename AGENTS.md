# Flasher and J2534 ownership

This directory contains real ECU transport, flashing and diagnostics. The
coordinating session is the sole hardware owner. Do not assign a flash, KWP
poller or live logger to a subagent, and do not overlap any of them. Offline
builds, checksum analysis and flasher `--dry-run` do not access CAN.

Before a bench flash, record the exact image path and full SHA-256, establish
the currently flashed image, compare complete sectors, and choose the minimum
whole-sector range that covers every changed byte including its Layer-1
checksum word. If the ECU's previous image is uncertain, flash the full
required range rather than guessing. Keep the flasher log and checksum result.

StopCommunication's ACK commits and soft-resets the ECU; it is not proof of
application boot. Release the J2534 device, then obtain a fresh diagnostic or
measuring-block read before claiming that the ECU is running. Do not infer
dynamic tune validation from an idle BeamNG capture. Bench recovery hardware
is not a reason to use parallel hardware agents.

The vehicle artifact must retain stock fault handling. The
`--simulator-mode` overlay is only for the bench simulator. The
current source hashes and build path are in root `AGENTS.md` and
`docs/KNOWN_GOOD_FILES.md`.
