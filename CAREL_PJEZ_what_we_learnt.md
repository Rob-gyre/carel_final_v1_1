# CAREL PJEZ Read/Write Protocol - What We Learnt

## Purpose

This document records what we learnt while getting `carel_final.py` working with a CAREL PJEZ controller over the TTL/programming-key port.

The aim was to make the Raspberry Pi script reliably:

- write CAREL parameters, especially `St` setpoint
- read CAREL parameters
- read probe temperatures
- read live status bits such as compressor, fan, defrost and alarms
- understand whether the read method was a direct EEPROM read, Modbus read, or something else

The final result is that both reading and writing work, but the method is not Modbus and not a direct EEPROM/register read.

The working method is best described as:

> Empirical TTL serial protocol discovery, using CAREL table-dump polling over the programming-key port.


## High-Level Conclusion

The CAREL PJEZ controller was not using standard Modbus RTU on this connection.

It was also not using the generic CAREL RS485 Supervisor Protocol format often found online, which normally uses:

- two ASCII address digits, such as `01`
- XOR-style checksum
- read commands such as `R` with type/index payloads

Instead, the working controller used a CAREL TTL/programming-key style protocol:

- 19200 baud
- 8 data bits
- no parity
- 2 stop bits
- one ASCII unit/address character, such as `1`
- STX/ETX framed command body
- two ASCII checksum nibbles
- checksum based on byte sum including STX and ETX

Writing works using direct indexed write commands.

Reading works by starting a table dump with `F1`, then polling queued frames with `ENQ + unit`.


## Physical And Serial Layer

The Raspberry Pi communicated through a USB-to-serial adapter connected to the CAREL programming/key TTL connection.

The serial settings that worked were:

```text
Baud rate: 19200
Data bits: 8
Parity: none
Stop bits: 2
Format: 8N2
```

This was confirmed by live tests. When the controller was connected correctly, known commands returned valid responses. When the controller was unplugged, every known-good command returned no bytes.

Example of healthy communication:

```text
1?        -> returns fixed identity/version frame
1F1       -> returns ACK
1U710016  -> returns ACK
```

Example when the CAREL was unplugged:

```text
1?        -> b''
1F1       -> b''
1U710016  -> b''
```

That proved the script and serial port could be correct, while a missing physical connection could still make everything fail.


## Frame Format

The working frame format is:

```text
STX + unit/address + command body + ETX + checksum
```

Where:

```text
STX = 0x02
ETX = 0x03
unit/address = one ASCII character, usually "1"
checksum = two ASCII nibbles from byte sum
```

Example command body:

```text
1F1
```

Full packet:

```text
02 31 46 31 03 3A 3D
```

Breakdown:

```text
02       STX
31       ASCII "1" unit/address
46 31    ASCII "F1"
03       ETX
3A 3D    checksum
```


## Checksum Method

The checksum is calculated by:

1. Taking all bytes from `STX` through `ETX`, inclusive.
2. Adding them together.
3. Keeping only the lower 8 bits with `& 0xFF`.
4. Splitting that byte into high and low nibbles.
5. Encoding each nibble as `0x30 + nibble`.

The Python function is:

```python
def bcc(core):
    b = sum(core) & 0xFF
    return bytes([0x30 + ((b >> 4) & 0x0F), 0x30 + (b & 0x0F)])
```

The complete frame builder is:

```python
def build_frame(body):
    core = bytes([STX]) + body.encode("ascii") + bytes([ETX])
    return core + bcc(core)
```

This is important because some online CAREL Supervisor examples use a different checksum, often XOR and often excluding STX. That did not match this controller.


## Why Generic CAREL Supervisor Protocol Did Not Fit

Online information often described CAREL Supervisor Protocol as:

```text
STX + two digit address + command + payload + ETX + XOR checksum
```

Example shape:

```text
02 30 31 52 49 30 35 03 checksum
```

This was tested against the actual controller using several two-digit-address/XOR frames.

The controller did not reply.

That told us the live CAREL PJEZ connection was not using that protocol on this port. The working port was the TTL/programming-key style protocol, not the generic RS485 Supervisor frame style.


## Initial Read Problem

The original read logic tried to poll using:

```text
ENQ + unit
```

For unit 1:

```text
05 31
```

The controller often replied:

```text
00
```

This means NULL/no queued data.

At first this looked like "no data received" or unreliable reading. Later we learnt this was not a dead serial connection. It meant the CAREL was alive but had no token/value frames queued for the script to pull.

This was an important distinction:

```text
b''      = no reply at all
b'\x00'  = controller replied NULL, no queued data
```


## Testing Method Used

The investigation was done as black-box serial protocol testing.

We did not begin with a confirmed official byte-level CAREL TTL protocol document. Instead, we sent controlled candidate frames and recorded exactly what the controller returned.

The test process was:

1. Confirm Linux could see the serial adapter.
2. Confirm Python could open the serial port.
3. Send known or suspected CAREL frames.
4. Record raw hex responses.
5. Identify which replies proved useful behaviour.
6. Reject commands that only returned ACK but produced no data.
7. Avoid trusting ACK alone as proof.
8. Patch the script only once a command was proven by behaviour.

This was important because the CAREL replied `ACK` to many valid-looking frames even when they did not actually perform useful reads.


## Commands Tested

Many possible read commands were tested.

These included:

```text
R
G
E
Q
L
M
X
V
B
C
H
S
a
?
```

Different formats were also tested:

```text
1R8
1R08
1R0008
1RS81
1RS008
1G0008
1?8
1?A01
1B8
1C8
1A8
1F0
1F1
1FA
```

Most read-like commands returned:

```text
ACK 06
```

Then a follow-up `ENQ + unit` returned:

```text
00
```

That meant those commands were not useful direct-read commands.


## Important Lesson About ACK

One of the biggest lessons was that:

```text
ACK does not prove the command did what we wanted.
```

On this controller, many command bodies returned:

```text
06
```

But then produced no data.

So ACK mostly proved:

- the frame structure was acceptable
- the checksum was acceptable
- the controller heard something

It did not prove:

- the command was valid for reading
- the command selected the requested parameter
- the controller queued a response value

The only commands accepted as "working" were the ones that caused a visible and repeatable result.


## Identity / Version Frame

The command:

```text
1?
```

returned a fixed frame:

```text
02 31 56 DD 49 03 3B 32
```

This was useful because it proved the CAREL was alive and replying through the TTL link.

However, appending things to `?` did not make it read parameters.

For example:

```text
1?8
1?08
1?A1
1?A01
1?S81
```

all returned the same fixed `1V DD49` style frame.

That showed `?` was most likely a fixed identity/version/status request, not a parameter read command.


## Write Unlock Problem

The original script used this unlock sequence:

```python
def unlock(self, password=0x16):
    if not self.send(f"{self.slot}U71{password:04X}"):
        return False
    time.sleep(0.05)
    return self.send(f"{self.slot}Ad0001")
```

The problem was:

```text
Ad0001
```

This means:

```text
A + d + 0001
```

In this protocol, the character after `A` is treated as an index character. Lowercase `d` was not the correct unlock-enable index.

The corrected command is:

```text
A00001
```

So the corrected unlock is:

```python
def unlock(self, password=0x16):
    if not self.send(f"{self.slot}U71{password:04X}"):
        return False
    time.sleep(0.05)
    return self.send(f"{self.slot}A00001")
```

This was tested live and confirmed:

```text
1U710016  -> ACK 06
1A00001   -> ACK 06
```


## How Writing Works

Writing is direct indexed writing.

For setpoint `St`, the token is:

```text
S81
```

The write index comes from the second character of the token:

```text
S81 -> index character "8"
```

The write command body for setpoint is:

```text
1A8xxxx
```

where `xxxx` is the raw 16-bit value.

Examples:

```text
St = 4.0 C
4.0 * 10 = 40
raw = 0x0028
write body = 1A80028
```

```text
St = -19.0 C
-19.0 * 10 = -190
raw = 0xFF42
write body = 1A8FF42
```

Negative numbers are written using 16-bit two's complement.

The successful write sequence is:

```text
1U710016  -> unlock/password
1A00001   -> write enable
1A8xxxx   -> write value
```


## Read Breakthrough

The key read discovery was:

```text
1F1
```

When sent as a framed command, `1F1` returned:

```text
ACK 06
```

Then repeated `ENQ + unit` polling returned real token/value frames.

The working read sequence is:

```text
1F1 -> ACK
ENQ + unit -> token/value frame
ACK
ENQ + unit -> next token/value frame
ACK
repeat until 00/NULL
```

So reading is not direct single-parameter reading. It is table-dump polling.


## How The Read Frames Work

After `1F1`, the CAREL sends token/value frames such as:

```text
02 31 53 38 31 30 30 32 38 03 3B 3C
```

The body between STX and ETX is:

```text
1S810028
```

Breakdown:

```text
1       unit/address
S81     token
0028    raw value
```

`S81` is setpoint.

`0028` is raw decimal 40.

With scale 10:

```text
40 / 10 = 4.0 C
```

The script then stores:

```text
St = 4.0
```


## Token Mapping

The script maps CAREL tokens to readable names.

Examples:

```text
S81 -> St / setpoint
S91 -> rd / differential
S11 -> Pb1 / probe 1 room-control temperature
S21 -> Pb2 / probe 2 evaporator temperature
S31 -> Pb3 / probe 3 auxiliary temperature
UC1 -> dI / defrost interval
UD1 -> dP / max defrost duration
B21 -> compressor status
B31 -> defrost status
B41 -> fan status
```

The script applies:

- scaling, such as `/10`
- signed conversion for negative values
- boolean handling for status bits


## Probe Temperatures Added

The following probe mappings were added:

```python
"Pb1": ("S11", 10, True, "Probe 1 / room-control temp C"),
"Pb2": ("S21", 10, True, "Probe 2 / evaporator temp C"),
"Pb3": ("S31", 10, True, "Probe 3 / auxiliary temp C"),
```

Meaning:

```text
Pb1 = Probe 1, normally room/control temperature
Pb2 = Probe 2, normally evaporator/defrost temperature
Pb3 = Probe 3, auxiliary if fitted
```

This helped confirm whether the controller was seeing the room probe and evaporator probe separately.


## Status Bits Added

The status mappings added were:

```python
STATUS = {
    "B21": "Compressor ON",
    "B31": "Defrost active",
    "B41": "Fan ON",
    "B71": "Alarm active",
    "B:1": "Probe fault",
}
```

These allow the read output to show live controller states as:

```text
ON
OFF
N/A
```


## NULL Handling

The controller can return:

```text
00
```

This means NULL/no queued data.

The script should not always stop at the first `00`, because during testing we saw a `00` appear before some wanted values had arrived.

The improved logic is:

```python
needed = {PARAMS[m][0] for m in PARAMS}
```

Then:

```python
if frame == bytes([NULL]):
    if needed.issubset(values.keys()):
        break
    null_count += 1
    if null_count >= max_null:
        break
    continue
```

Meaning:

```text
If 00 appears before all needed tokens are present, keep polling.
If 00 appears after all needed tokens are present, stop cleanly.
If too many 00s appear, stop anyway.
```


## Debug Output Added

Verbose/debug output was added to show exactly what the read loop was receiving:

```python
self.log("ENQ RX: timeout")
self.log(f"ENQ RX: {frame.hex(' ')}")
```

This helped distinguish between:

```text
timeout/no reply
00 NULL
actual token/value frame
```

That was important during troubleshooting because `b''` and `b'\x00'` mean very different things.


## Corrected Parameter Labels

Some original labels were misleading.

Corrected meanings:

```text
d6 = display during defrost
d9 = defrost priority over compressor protection
dC = defrost time base
```

This matters because writing the wrong CAREL parameter based on a bad description could change the behaviour of defrost or compressor protection.


## What The Final carel_final.py Does

The final script:

1. Opens the serial port at 19200 8N2.
2. Builds CAREL TTL frames using STX/ETX and sum checksum.
3. Writes by unlocking, enabling write, then writing an indexed value.
4. Reads by sending `F1`, then polling the queued token stream.
5. Decodes S/U/B token frames.
6. Prints parameters, probes and status bits.

The read command:

```bash
sudo python carel_final.py --port /dev/ttyACM0 --unit 1 read
```

The write command:

```bash
sudo python carel_final.py --port /dev/ttyACM0 --unit 1 --password 22 write St 4.0
```


## Is It EEPROM Read?

No, not in the strict sense.

It is not direct EEPROM read and it is not Modbus-style register polling.

The method is polling based, but it polls a CAREL-exported token stream:

```text
F1 starts the export/table dump
ENQ asks for the next queued item
ACK confirms each item
00 means no more data
```

So the best description is:

```text
polling-based CAREL table dump over the TTL programming-key protocol
```


## What Other Controllers Might This Apply To

This method may apply to CAREL controllers that use the same or closely related CAREL easy/PJEZ TTL programming-key protocol.

Most likely candidates:

```text
CAREL PJEZ / PJ Easy family
CAREL easy / easy compact variants
possibly related PJEZ models using the same key/programming interface
```

It may apply to some related CAREL refrigeration controllers, but only after testing:

```text
CAREL ir33 / ir33+ family
other CAREL parametric controllers with similar key/programming accessories
```

Do not assume it applies to:

```text
CAREL pCO / pLAN controllers
controllers in Modbus RTU mode
RS485 Supervisor Protocol using two-digit address and XOR checksum
newer CAREL controllers with different serial cards or firmware
```

Before applying it to another controller, prove the signature:

```text
1? returns the identity frame
1U710016 returns ACK
1A00001 returns ACK
1F1 returns ACK
ENQ after F1 returns token/value frames
```


## Final Working Summary

Writing:

```text
1U710016  -> password/unlock
1A00001   -> write enable
1A8xxxx   -> write St/setpoint
```

Reading:

```text
1F1       -> start table dump
ENQ + unit -> receive next token/value frame
ACK       -> accept frame and request next later
00        -> no queued data/end
```

The key lesson:

```text
The CAREL PJEZ TTL port accepts many commands with ACK, but only proven behaviour counts.
F1 is the useful read/table-dump trigger.
A00001 is the correct write-enable command.
```

