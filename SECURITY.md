# Security

Plomada lets an MCP client change the model open in SketchUp. These are the boundaries it keeps.

## Loopback only

The extension listens on `127.0.0.1:7883` (port configurable from 1024 to 65535). It refuses to start when the
configured host is anything but a loopback literal (`127.0.0.1` or `::1`). There is no HTTP server, so a web page cannot
talk to it through the browser's HTTP stack. A page could open a raw TCP connection, but it would still have to pass the
handshake below. It accepts at most 8 clients at once and closes any frame larger than 32 MiB.

## Token

- On first load the extension writes `SecureRandom.hex(32)`, 64 hex characters, to `%LOCALAPPDATA%\Plomada\bridge.token`.
  It writes `bridge.token.tmp` first and then renames it, so a reader never sees half a token. The file sits in your
  per-user profile.
- The first frame of every connection must be `hello` with `protocol: 1` and the token, within 10 s. A wrong protocol is
  answered with `-32002` and the socket closes. A wrong or missing token is answered with `-32001` and the socket closes.
  A socket that never says hello is closed after 10 s.
- Tokens are compared in constant time: unequal lengths are rejected first, then every byte is XOR-accumulated.
- The bridge reads the same file. Nothing else needs the token. To rotate it, delete the file and restart SketchUp, then
  restart the MCP client.

## `execute_ruby` is off by default

`execute_ruby` runs arbitrary Ruby inside SketchUp, so it is disabled until you tick **Allow execute_ruby** in
**Extensions > Plomada > Settings** (`Sketchup.read_default("Plomada", "allow_ruby", false)`). Until then every call is
answered with `-32010`. When enabled:

- Code that mentions `system`, `exec`, `spawn`, `fork`, backticks, `%x`, `Thread.new`, `exit` or `Sketchup.quit` is
  refused before it runs.
- The code runs inside one operation, so it is one undo step and an exception rolls it back.
- A 10 s soft deadline is checked on every Ruby line (a `TracePoint`, no threads). It stops Ruby loops but cannot stop a
  native SketchUp call that is already running.
- Captured `$stdout` is capped at 64 KiB and the source at 200 KiB.

This is a guard against mistakes, not a sandbox: Ruby inside SketchUp can still reach the file system. Enable it only
for a client you trust.

## Audit log

Every mutating call adds one line to `%LOCALAPPDATA%\Plomada\audit.log`: UTC timestamp, client id, method, duration and
outcome. Params and the token are never written. The file rotates to `audit.log.1` at 5 MiB. The settings dialog shows
the last 20 lines.

## No network, no telemetry

The only socket is the loopback one above. Plomada makes no outbound connections and sends no telemetry.

## Reporting a vulnerability

Open a private security advisory on the repository:
<https://github.com/Mats2208/plomada-sketchup-mcp/security/advisories/new>.
