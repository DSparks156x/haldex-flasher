#!/usr/bin/env python3
"""
Read (and optionally clear) the Haldex fault memory over KWP2000.

Why this exists: on a --simulator-mode image the fault flags in the 0x2C0
broadcast are LIES. Two of the five bench patches overwrite the *getters* that
feed that message:

  SIMULATOR_PATCHES[0]  Notlauf bypass            -> reports Notlauf   = 0 always
  SIMULATOR_PATCHES[1]  Fehler_Allrad_Kupplung    -> reports clutch OK always

so a bench unit can sit in a latched internal fault while broadcasting
limp=False / lamp=False / fault=False and commanding 0 Nm. The fault MEMORY is
not patched, so this is the way to see the truth.

  py -3.11-32 -m flasher.read_dtcs               # read
  py -3.11-32 -m flasher.read_dtcs --clear       # read, clear, read back
"""
import sys
import time
from argparse import ArgumentParser

from .j2534 import J2534Device
from .tp20 import TP20Transport
from .kwp2000 import KWP2000Client, SERVICE_TYPE, SESSION_TYPE

# KWP2000 status-of-DTC selectors worth trying, most useful first.
STATUS_VARIANTS = [
    (0x00, 'all identified'),
    (0x02, 'supported + identified'),
    (0xFF, 'all'),
]


def decode_status(b):
    bits = []
    if b & 0x80: bits.append('warningLamp')
    if b & 0x40: bits.append('mil')
    if b & 0x20: bits.append('storedSinceClear')
    if b & 0x10: bits.append('confirmed')
    if b & 0x08: bits.append('pending')
    if b & 0x04: bits.append('testFailed')
    if b & 0x02: bits.append('currentlyActive')
    if b & 0x01: bits.append('presentAtRequest')
    return ','.join(bits) if bits else '-'


def read_dtcs(kwp, verbose=False):
    """
    Try the 0x18 variants; return (status_byte, label, raw_response).

    Sent through the transport directly, NOT through KWP2000Client._kwp: the
    0x18 positive response is `58 <count> <3-byte records...>` with no
    subfunction echo, and _kwp's generic parser treats byte[1] as the echoed
    subfunction and raises InvalidSubFunctionError on a perfectly good answer.
    """
    for status, label in STATUS_VARIANTS:
        req = bytes([SERVICE_TYPE.READ_DIAGNOSTIC_TROUBLE_CODES_BY_STATUS,
                     status, 0xFF, 0x00])
        try:
            kwp.transport.send(req)
            resp = kwp.transport.recv()
        except Exception as e:
            if verbose:
                print('  [%s] %s: %s' % (req.hex(), type(e).__name__, e))
            continue
        if verbose:
            print('  [%s] -> %s' % (req.hex(), resp.hex()))
        if resp and resp[0] == 0x58:
            return status, label, resp
        if resp and resp[0] == 0x7F and verbose:
            print('       negative response, trying next variant')
    return None, None, None


def show(resp):
    """0x58 <count> then 3-byte records {dtc_hi, dtc_lo, status}."""
    if len(resp) < 2:
        print('  short response: %s' % resp.hex())
        return 0
    count = resp[1]
    body = resp[2:]
    n = len(body) // 3
    print('  ECU reports %d DTC(s); %d record(s) in the payload' % (count, n))
    if n == 0:
        print('  -> fault memory is CLEAR')
    for i in range(n):
        hi, lo, st = body[3 * i], body[3 * i + 1], body[3 * i + 2]
        code = (hi << 8) | lo
        print('   %2d.  0x%04X (%5d)   status 0x%02X  %s'
              % (i + 1, code, code, st, decode_status(st)))
    print('  raw: %s' % resp.hex())
    return n


def main():
    ap = ArgumentParser()
    ap.add_argument('--dll', default=None)
    ap.add_argument('--module', type=lambda x: int(x, 0), default=0x0A)
    ap.add_argument('--clear', action='store_true',
                    help='clear the fault memory (14 FF 00) and read back')
    ap.add_argument('--tries', type=int, default=10)
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    dev = J2534Device(args.dll) if args.dll else J2534Device()
    dev.open()
    dev.connect_can(baudrate=500000)
    tp = None
    try:
        # The module does not always answer the first channel-setup request,
        # especially just after a power cycle. The flasher retries too.
        # NOTE: TP20Transport.__init__ ALREADY opens the channel. Calling
        # open_channel() again sends a duplicate setup on a live channel, which
        # the ECU ignores -> guaranteed timeout. Same retry shape the flasher's
        # reconnect() uses: settle, flush the RX queue, then try.
        last = None
        for attempt in range(args.tries):
            time.sleep(1.0)
            dev.can_clear(0xFFFF)
            try:
                tp = TP20Transport(dev, module=args.module, timeout=0.8)
                if attempt:
                    print('connected on attempt %d' % (attempt + 1))
                break
            except Exception as e:
                last = e
                tp = None
        if tp is None:
            print('could not open a TP2.0 channel to module 0x%02X after %d tries: %s'
                  % (args.module, args.tries, last))
            return 3
        if tp.tx_addr != 0x764:
            print('ECU is answering on 0x%03X, not the application (0x764).' % tp.tx_addr)
            print('It is sitting in the resident bootloader -- power cycle it first.')
            return 2

        kwp = KWP2000Client(tp, debug=args.verbose)
        kwp.diagnostic_session_control(SESSION_TYPE.DIAGNOSTIC)

        print('\n=== fault memory BEFORE ===')
        status, label, resp = read_dtcs(kwp, args.verbose)
        if resp is None:
            print('  no 0x18 variant answered; the ECU may not support it in this session.')
            return 1
        print('  (via 18 %02X FF 00 -- %s)' % (status, label))
        before = show(resp)

        if args.clear:
            print('\n=== clearing (14 FF 00) ===')
            try:
                kwp.transport.send(bytes([SERVICE_TYPE.CLEAR_DIAGNOSTIC_INFORMATION,
                                          0xFF, 0x00]))
                r = kwp.transport.recv()
                print('  -> %s  %s' % (r.hex(),
                                       'accepted' if r and r[0] == 0x54
                                       else 'REJECTED'))
            except Exception as e:
                print('  %s: %s' % (type(e).__name__, e))
            time.sleep(0.5)
            print('\n=== fault memory AFTER ===')
            _s, _l, resp2 = read_dtcs(kwp, args.verbose)
            if resp2 is not None:
                after = show(resp2)
                if before and not after:
                    print('\ncleared. If the coupling still commands 0 Nm after this,')
                    print('the latch is not in the fault memory -- power cycle.')
        return 0
    finally:
        try:
            if tp:
                tp.disconnect()
        except Exception:
            pass
        dev.close()


if __name__ == '__main__':
    sys.exit(main())
