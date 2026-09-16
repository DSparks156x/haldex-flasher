import struct
from enum import IntEnum
try:
    from .tp20 import TP20Transport
except ImportError:
    from tp20 import TP20Transport

class NegativeResponseError(Exception):
    def __init__(self, message, service_id, error_code):
        super().__init__(message)
        self.message = message
        self.service_id = service_id
        self.error_code = error_code

    def __str__(self):
        return self.message

class InvalidServiceIdError(Exception):
    pass

class InvalidSubFunctionError(Exception):
    pass

class SERVICE_TYPE(IntEnum):
    DIAGNOSTIC_SESSION_CONTROL = 0x10
    ECU_RESET = 0x11
    READ_FREEZE_FRAME_DATA = 0x12
    READ_DIAGNOSTIC_TROUBLE_CODES = 0x13
    CLEAR_DIAGNOSTIC_INFORMATION = 0x14
    READ_STATUS_OF_DIAGNOSTIC_TROUBLE_CODES = 0x17
    READ_DIAGNOSTIC_TROUBLE_CODES_BY_STATUS = 0x18
    READ_ECU_IDENTIFICATION = 0x1A
    STOP_DIAGNOSTIC_SESSION = 0x20
    READ_DATA_BY_LOCAL_IDENTIFIER = 0x21
    READ_DATA_BY_COMMON_IDENTIFIER = 0x22
    READ_MEMORY_BY_ADDRESS = 0x23
    SECURITY_ACCESS = 0x27
    REQUEST_DOWNLOAD = 0x34
    REQUEST_UPLOAD = 0x35
    TRANSFER_DATA = 0x36
    REQUEST_TRANSFER_EXIT = 0x37
    WRITE_MEMORY_BY_ADDRESS = 0x3D
    TESTER_PRESENT = 0x3E

class ECU_IDENTIFICATION_TYPE(IntEnum):
    IDENT_ORIGINAL = 0x80
    IDENT_SCALING = 0x81
    IDENT_CURRENT = 0x82
    ECU_IDENT = 0x9B
    STATUS_FLASH = 0x9C
    SYSTEM_NAME = 0x97
    VIN = 0x90

class SESSION_TYPE(IntEnum):
    DEFAULT = 0x81
    PROGRAMMING = 0x85
    ENGINEERING_MODE = 0x86
    DIAGNOSTIC = 0x89
    EXTENDED = 0x92

class ACCESS_TYPE(IntEnum):
    PROGRAMMING_REQUEST_SEED = 1
    PROGRAMMING_SEND_KEY = 2
    REQUEST_SEED = 3
    SEND_KEY = 4
    MANUFACTURE_REQUEST_SEED = 0x9
    MANUFACTURE_SEND_KEY = 0xA

_negative_response_codes = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported-invalidFormat",
    0x21: "busy-RepeatRequest",
    0x22: "conditionsNotCorrect or requestSequenceError",
    0x23: "routineNotComplete",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x40: "downloadNotAccepted",
    0x41: "improperDownloadType",
    0x42: "cantDownloadToSpecifiedAddress",
    0x43: "cantDownloadNumberOfBytesRequested",
    0x50: "uploadNotAccepted",
    0x51: "improperUploadType",
    0x52: "cantUploadFromSpecifiedAddress",
    0x53: "cantUploadNumberOfBytesRequested",
    0x71: "transferSuspended",
    0x72: "transferAborted",
    0x74: "illegalAddressInBlockTransfer",
    0x75: "illegalByteCountInBlockTransfer",
    0x76: "illegalBlockTransferType",
    0x77: "blockTransferDataChecksumError",
    0x78: "reqCorrectlyRcvd-RspPending",
    0x79: "incorrectByteCountDuringBlockTransfer",
}

class KWP2000Client:
    def __init__(self, transport: TP20Transport, debug: bool = False):
        self.transport = transport
        self.debug = debug

    def _kwp(self, service_type: SERVICE_TYPE, subfunction: int = None, data: bytes = None) -> bytes:
        req = bytes([service_type])
        if subfunction is not None:
            req += bytes([subfunction])
        if data is not None:
            req += data

        if self.debug:
            print(f"[KWP TX] {req.hex()}")

        self.transport.send(req)
        resp = self.transport.recv()

        if self.debug:
            print(f"[KWP RX] {resp.hex()}")

        resp_sid = resp[0] if len(resp) > 0 else None

        if resp_sid == 0x7F:
            service_id = resp[1] if len(resp) > 1 else -1
            try:
                service_desc = SERVICE_TYPE(service_id).name
            except Exception:
                service_desc = f"SID_0x{service_id:02X}"
            error_code = resp[2] if len(resp) > 2 else -1
            error_desc = _negative_response_codes.get(error_code, f"ERR_0x{error_code:02X}")
            raise NegativeResponseError(f"{service_desc} - {error_desc}", service_id, error_code)

        if service_type + 0x40 != resp_sid:
            raise InvalidServiceIdError(f"Invalid response service ID: 0x{resp_sid:02X}")

        if subfunction is not None:
            resp_sfn = resp[1] if len(resp) > 1 else None
            if subfunction != resp_sfn:
                raise InvalidSubFunctionError(f"Invalid response subfunction: 0x{resp_sfn:02X}")

        return resp[(1 if subfunction is None else 2) :]

    def diagnostic_session_control(self, session_type: SESSION_TYPE):
        return self._kwp(SERVICE_TYPE.DIAGNOSTIC_SESSION_CONTROL, subfunction=session_type)

    def security_access(self, access_type: ACCESS_TYPE, security_key: bytes = b""):
        request_seed = (access_type % 2 != 0)
        if request_seed and len(security_key) != 0:
            raise ValueError("security_key not allowed when requesting seed")
        if not request_seed and len(security_key) == 0:
            raise ValueError("security_key is missing when sending key")
        return self._kwp(SERVICE_TYPE.SECURITY_ACCESS, subfunction=access_type, data=security_key)

    def read_ecu_identification(self, data_identifier_type: ECU_IDENTIFICATION_TYPE):
        return self._kwp(SERVICE_TYPE.READ_ECU_IDENTIFICATION, data_identifier_type)

    def read_memory_by_address(self, address: int, size: int) -> bytes:
        # Address: 3 bytes (Big Endian) + Size: 1 or 2 bytes
        addr_bytes = struct.pack(">I", address)[1:]  # 3 bytes
        size_bytes = struct.pack(">B", size) if size <= 255 else struct.pack(">H", size)
        return self._kwp(SERVICE_TYPE.READ_MEMORY_BY_ADDRESS, subfunction=None, data=addr_bytes + size_bytes)

    def tester_present(self, subfunction: int = 0x00) -> bytes:
        return self._kwp(SERVICE_TYPE.TESTER_PRESENT, subfunction=subfunction)
