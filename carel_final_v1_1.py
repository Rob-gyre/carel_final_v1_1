#!/usr/bin/env python3
"""
CAREL PJEZ Controller - Final Read/Write Script

Token mappings confirmed by manual testing.

Usage:
  python3 carel_final.py --port /dev/ttyACM0 read
  python3 carel_final.py --port /dev/ttyACM0 write St 5.0
  python3 carel_final.py --port /dev/ttyACM0 write c2 3
"""

import argparse
import sys
import time

try:
    import serial
except ImportError:
    print("ERROR: pip install pyserial")
    sys.exit(1)

STX, ETX, ENQ, ACK, NULL = 0x02, 0x03, 0x05, 0x06, 0x00

# Confirmed parameter mappings from manual testing
# Format: "mnemonic": (token, scale, signed, description)
# Token format: Table + 2 chars, e.g., "S81", "U<1", "BM1"
# For WRITE: we need to convert token to EZ index
# Token char to EZ: '0'=0, '1'=1, ... '9'=9, ':'=10, ';'=11, '<'=12, '='=13, '>'=14, '?'=15, '@'=16, 'A'=17, ...

PARAMS = {
    # S-table (analog) - x10 scaling, signed
    "Pb1": ("S11", 10, True, "Probe 1 / room-control temp °C"),
    "Pb2": ("S21", 10, True, "Probe 2 / evaporator temp °C"),
    "Pb3": ("S31", 10, True, "Probe 3 / auxiliary temp °C"),
    "St": ("S81", 10, True, "Setpoint °C"),
    "rd": ("S91", 10, True, "Differential °C"),
    "LSE": ("S:1", 10, True, "Minimum setpoint allowed °C"),
    "HSE": ("S;1", 10, True, "Maximum setpoint allowed °C"),
    "/C1": ("S51", 10, True, "Probe 1 calibration °C"),
    "/C2": ("S61", 10, True, "Probe 2 calibration °C"),
    "F1": ("SC1", 10, True, "Fan stop temp °C"),
    "AL": ("S?1", 10, True, "Low alarm °C"),
    "AH": ("S@1", 10, True, "High alarm °C"),
    "dt": ("S=1", 10, True, "Defrost end temp °C"),
    
    # U-table (integer) - no scaling
    "c1": ("U<1", 1, False, "Min between starts (min)"),
    "c2": ("U=1", 1, False, "Min OFF time (min)"),
    "c3": ("U>1", 1, False, "Min ON time (min)"),
    "d0": ("UB1", 1, False, "Defrost type"),
    "d1": ("UC1", 1, False, "Defrost interval (hours)"),
    "dP": ("UD1", 1, False, "Max defrost time (min)"),
    "d5": ("UE1", 1, False, "Defrost delay (min)"),
    "dd": ("UF1", 1, False, "Drain time (min)"),
    "d8": ("UG1", 1, False, "Defrost priority"),
    "Fd": ("UL1", 1, False, "Fan delay after dripping (min)"),
    
    # B-table (bit) - 0 or 1
    "d4": ("BL1", 1, False, "Defrost at power-on"),
    "d6": ("BM1", 1, False, "Display during defrost"),
    "d9": ("BN1", 1, False, "Defrost priority over compressor protection"),
    "dC": ("BO1", 1, False, "Defrost time base"),
    "F0": ("BQ1", 1, False, "Enable evaporator fan control"),
    "F2": ("BR1", 1, False, "Fans cycle with compressor"),
    "F3": ("BS1", 1, False, "Fans in defrost"),
}

# Reverse mapping: token -> mnemonic
TOKEN_TO_PARAM = {v[0]: (k, v[1], v[2], v[3]) for k, v in PARAMS.items()}

STATUS = {
    "B21": "Compressor ON",
    "B31": "Defrost active",
    "B41": "Fan ON",
    "B71": "Alarm active",
    "B:1": "Probe fault",
}

def token_char_to_index(ch):
    """Convert token character to index number."""
    return ord(ch) - 0x30


def index_to_token_char(idx):
    """Convert index to token character."""
    return chr(0x30 + idx)


def token_to_ez(token):
    """
    Convert broadcast token to EZ index for writing.
    Token "S81" -> table S, char '8' -> EZ 8
    Token "U<1" -> table U, char '<' -> EZ 12
    """
    if len(token) < 3:
        return None, None
    table = token[0]
    char = token[1]
    ez = token_char_to_index(char)
    return table, ez


def carel_hex(s):
    """Decode CAREL hex string."""
    r = 0
    for c in s:
        if '0' <= c <= '9': v = ord(c) - 0x30
        elif 'A' <= c <= 'F': v = ord(c) - 0x37
        elif 'a' <= c <= 'f': v = ord(c) - 0x57
        elif ':' <= c <= '?': v = ord(c) - 0x30
        else: return 0
        r = (r << 4) | (v & 0x0F)
    return r


def bcc(core):
    """Calculate BCC checksum."""
    b = sum(core) & 0xFF
    return bytes([0x30 + ((b >> 4) & 0x0F), 0x30 + (b & 0x0F)])


def build_frame(body):
    """Build protocol frame."""
    core = bytes([STX]) + body.encode("ascii") + bytes([ETX])
    return core + bcc(core)

class PJEZ:
    def __init__(self, port, unit=1, verbose=False):
        self.unit = unit
        self.verbose = verbose
        self.slot = chr(0x30 + unit)
        self.ser = serial.Serial(port=port, baudrate=19200, bytesize=8,
                                  parity='N', stopbits=2, timeout=0.5)
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.ser.close()
    
    def log(self, msg):
        if self.verbose:
            print(f"[DEBUG] {msg}")
    
    def send(self, body, timeout=0.8):
        """Send frame and wait for ACK."""
        frame = build_frame(body)
        self.log(f"TX: {body} | {frame.hex(' ')}")
        self.ser.reset_input_buffer()
        self.ser.write(frame)

        deadline = time.time() + timeout
        while time.time() < deadline:
            b = self.ser.read(1)
            if b and b[0] == ACK:
                self.log("RX: ACK")
                return True
        self.log("RX: timeout")
        return False
    
    def unlock(self, password=0x16):
        """Unlock controller for writing."""
        if not self.send(f"{self.slot}U71{password:04X}"):
            return False
        time.sleep(0.05)
        return self.send(f"{self.slot}A00001")
    
    def write_raw(self, ez, raw, cmd="A"):
        """Write raw value to EZ index."""
        idx_char = index_to_token_char(ez)
        if cmd == "D":
            body = f"{self.slot}D{idx_char}{'1' if raw else '0'}"
        else:
            body = f"{self.slot}{cmd}{idx_char}{raw & 0xFFFF:04X}"
        return self.send(body)
    
    def read_frame(self, timeout=0.8):
        """Read a broadcast frame."""
        start = time.time()
        while time.time() - start < timeout:
            b = self.ser.read(1)
            if not b:
                continue
            if b[0] == NULL:
                return bytes([NULL])
            if b[0] == STX:
                buf = bytearray([STX])
                while time.time() - start < timeout:
                    c = self.ser.read(1)
                    if c:
                        buf.append(c[0])
                        if c[0] == ETX:
                            break
                for _ in range(2):
                    c = self.ser.read(1)
                    if c:
                        buf.append(c[0])
                return bytes(buf)
        return None
    
    def poll_all(self, cycles=120, max_null=50):
        """Start CAREL table dump, then poll queued tokens."""
        values = {}
        null_count = 0
        needed = {PARAMS[m][0] for m in PARAMS} | set(STATUS.keys())

        if not self.send(f"{self.slot}F1"):
            self.log("Read dump start failed")
            return values
        time.sleep(0.05)

        for _ in range(cycles):
            self.ser.write(bytes([ENQ, 0x30 + self.unit]))
            frame = self.read_frame()

            if frame is None:
                self.log("ENQ RX: timeout")
                continue
            self.log(f"ENQ RX: {frame.hex(' ')}")
            if frame == bytes([NULL]):
                if needed.issubset(values.keys()):
                    break
                null_count += 1
                if null_count >= max_null:
                    break
                continue

            null_count = 0
            self.ser.write(bytes([ACK]))

            if len(frame) >= 6 and frame[0] == STX:
                try:
                    etx = frame.index(ETX)
                    body = frame[1:etx].decode("ascii", errors="ignore")
                    if len(body) >= 5 and body[1] in "SUB":
                        token = f"{body[1]}{body[2:4]}"
                        raw = carel_hex(body[4:]) & 0xFFFF
                        values[token] = raw
                except:
                    pass
            time.sleep(0.02)

        return values
    
    def read_param(self, mnemonic):
        """Read a single parameter by mnemonic."""
        if mnemonic not in PARAMS:
            return None

        token, scale, signed, desc = PARAMS[mnemonic]
        values = self.poll_all(cycles=80)

        if token not in values:
            return None

        raw = values[token]
        if signed and raw >= 0x8000:
            raw = raw - 0x10000

        return raw / scale if scale > 1 else raw
    
    def write_param(self, mnemonic, value, password=0x16):
        """Write a parameter by mnemonic."""
        if mnemonic not in PARAMS:
            print(f"Unknown parameter: {mnemonic}")
            print(f"Available: {', '.join(sorted(PARAMS.keys()))}")
            return False

        token, scale, signed, desc = PARAMS[mnemonic]
        table, ez = token_to_ez(token)

        # Calculate raw value
        if table == "B":
            raw = 1 if value else 0
        else:
            raw = int(round(value * scale))
            if signed and raw < 0:
                raw = raw & 0xFFFF
            raw = raw & 0xFFFF

        print(f"Writing {mnemonic} ({desc})")
        print(f"  Value: {value}")
        print(f"  Token: {token}, EZ: {ez}, Raw: 0x{raw:04X} ({raw})")

        if not self.unlock(password):
            print("  ERROR: Unlock failed")
            return False
        time.sleep(0.1)

        # Use appropriate command based on table
        cmd = "D" if table == "B" else "A"

        if not self.write_raw(ez, raw, cmd):
            print("  ERROR: Write failed")
            return False

        print("  SUCCESS")
        return True

def cmd_read(args):
    """Read all known parameters."""
    with PJEZ(args.port, args.unit, args.verbose) as pjez:
        print("Reading parameters...\n")
        values = pjez.poll_all()

        if not values:
            print("ERROR: No data received")
            return 1

        print("=" * 60)
        print(" CAREL PJEZ Status")
        print("=" * 60)

        # Group by category
        categories = {
            "PROBES": ["Pb1", "Pb2", "Pb3"],
            "SETPOINT & REGULATION": ["St", "rd", "LSE", "HSE"],
            "PROBE CALIBRATION": ["/C1", "/C2"],
            "ALARMS": ["AL", "AH"],
            "COMPRESSOR TIMING": ["c1", "c2", "c3"],
            "DEFROST": ["d0", "d1", "dt", "dP", "d4", "d5", "d6", "d8", "d9", "dd", "dC"],
            "FANS": ["F0", "F1", "F2", "F3", "Fd"],
        }

        for cat_name, params in categories.items():
            print(f"\n{cat_name}")
            print("-" * 50)
            for mnemonic in params:
                if mnemonic not in PARAMS:
                    continue
                token, scale, signed, desc = PARAMS[mnemonic]
                if token in values:
                    raw = values[token]
                    if signed and raw >= 0x8000:
                        raw = raw - 0x10000
                    val = raw / scale if scale > 1 else raw

                    if scale > 1:
                        print(f"  {mnemonic:4} = {val:>8.1f}  ({desc})")
                    else:
                        print(f"  {mnemonic:4} = {val:>8}  ({desc})")
                else:
                    print(f"  {mnemonic:4} = {'N/A':>8}  ({desc})")

        print("\nSTATUS")
        print("-" * 50)
        for token, desc in STATUS.items():
            if token in values:
                state = "ON" if (values[token] & 1) else "OFF"
                print(f"  {desc:24} = {state}")
            else:
                print(f"  {desc:24} = N/A")

        print("\n" + "=" * 60)
        return 0


def cmd_write(args):
    """Write a parameter."""
    with PJEZ(args.port, args.unit, args.verbose) as pjez:
        if pjez.write_param(args.param, args.value, args.password):
            return 0
        return 1

def cmd_list(args):
    """List all available parameters."""
    print("Available parameters:\n")
    print(f"{'Mnemonic':<8} {'Token':<6} {'Scale':<6} {'Description'}")
    print("-" * 60)
    for mnemonic in sorted(PARAMS.keys()):
        token, scale, signed, desc = PARAMS[mnemonic]
        scale_str = f"x{scale}" if scale > 1 else "int"
        if token.startswith("B"):
            scale_str = "bit"
        print(f"{mnemonic:<8} {token:<6} {scale_str:<6} {desc}")


def main():
    parser = argparse.ArgumentParser(description="CAREL PJEZ Controller")
    parser.add_argument("--port", required=True, help="Serial port")
    parser.add_argument("--unit", type=int, default=1)
    parser.add_argument("--password", type=lambda x: int(x, 0), default=0x16)
    parser.add_argument("-v", "--verbose", action="store_true")
    
    sub = parser.add_subparsers(dest="cmd", required=True)
    
    sub.add_parser("read", help="Read all parameters")
    sub.add_parser("list", help="List available parameters")
    
    wp = sub.add_parser("write", help="Write a parameter")
    wp.add_argument("param", help="Parameter mnemonic")
    wp.add_argument("value", type=float, help="Value to write")
    
    args = parser.parse_args()
    
    if args.cmd == "read":
        return cmd_read(args)
    elif args.cmd == "write":
        return cmd_write(args)
    elif args.cmd == "list":
        return cmd_list(args)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())













