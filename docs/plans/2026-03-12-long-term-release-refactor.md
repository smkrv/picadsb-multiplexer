# PicADSB Multiplexer — Long-Term Release Refactor Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the monolithic picadsb-multiplexer into a well-structured, secure, testable, long-term maintainable project with CI/CD pipeline.

**Architecture:** Incremental refactoring from a single 1620-line god-object into focused modules (config, device, adsb, transport, stats, health). Fix critical bugs first, then security, then architecture. Each phase goes through 3-agent review cycle (code review → security review → architectural review), documentation, and local commit.

**Tech Stack:** Python 3.11+, pyserial, pyModeS, psutil, pytest, Docker, GitHub Actions

**Repository:** https://github.com/smkrv/picadsb-multiplexer

---

## Process per Phase

Every phase follows this cycle:

```
1. Implement fixes/changes
2. Run 3 parallel review agents:
   - Code Review (bugs, logic, quality)
   - Security Review (input validation, injection, network)
   - Architectural Review (coupling, SoC, scalability)
3. Fix issues found by reviewers
4. Re-review if critical issues found
5. Update documentation (memory, changelog)
6. Local commit
```

Push happens only after ALL phases complete.

---

## Chunk 1: Phase 1 — Critical Bug Fixes

### Task 1.1: Fix serial buffer data loss (C1)

**Problem:** `_process_serial_data()` (line 665) creates local `message = bytearray()` on every call. If a serial message is split across two reads, the first half is lost. `self._buffer` (line 260) is declared but never used.

**Files:**
- Modify: `picadsb-multiplexer.py:653-709`

- [ ] **Step 1.1.1: Fix `_process_serial_data()` to use persistent buffer**

Replace the local `message = bytearray()` with `self._serial_parse_buffer` that persists between calls:

```python
def _process_serial_data(self):
    """Process incoming data from the ADSB device with optimized buffering."""
    try:
        if not self.ser.is_open:
            self.logger.error("Serial port is closed")
            return

        if self.ser.in_waiting:
            self._last_data_time = time.time()
            data = self.ser.read(min(self.ser.in_waiting, 8192))
            self.stats['bytes_received'] += len(data)

            for byte in data:
                byte_val = bytes([byte])

                if byte_val in b'*#@$%&':
                    if self._serial_parse_buffer and len(self._serial_parse_buffer) > 5:
                        self.logger.debug(f"Incomplete message: {bytes(self._serial_parse_buffer)!r}")
                    self._serial_parse_buffer = bytearray([byte])
                    continue

                if not self._serial_parse_buffer:
                    continue

                if byte_val == b';':
                    self._serial_parse_buffer.append(byte)
                    final_msg = bytes(self._serial_parse_buffer)

                    if self.validate_message(final_msg):
                        try:
                            self.message_queue.put_nowait(final_msg + b'\n')
                            self.stats['messages_processed'] += 1
                            self.logger.debug(f"Processed message: {final_msg!r}")
                        except queue.Full:
                            self.stats['messages_dropped'] += 1
                    else:
                        if len(self._serial_parse_buffer) > 3:
                            try:
                                self.message_queue.put_nowait(final_msg + b'\n')
                                self.stats['recovered_messages'] += 1
                                self.logger.debug(f"Recovered partial message: {final_msg!r}")
                            except queue.Full:
                                self.stats['messages_dropped'] += 1
                        else:
                            self.stats['invalid_messages'] += 1
                    self._serial_parse_buffer = bytearray()
                else:
                    self._serial_parse_buffer.append(byte)
                    if len(self._serial_parse_buffer) > self.MAX_MESSAGE_LENGTH:
                        self._serial_parse_buffer = self._serial_parse_buffer[-self.MAX_MESSAGE_LENGTH:]
                        self.stats['buffer_truncated'] += 1

    except Exception as e:
        self.logger.error(f"Error in serial processing: {e}")
        self.stats['errors'] += 1
```

- [ ] **Step 1.1.2: Update `__init__` — replace dead `self._buffer` with `self._serial_parse_buffer`**

In `__init__` (line 260), replace:
```python
self._buffer = b''
```
with:
```python
self._serial_parse_buffer = bytearray()
```

- [ ] **Step 1.1.3: Update `_reset_device` — fix buffer reference**

In `_reset_device()` (line 568), replace:
```python
self._buffer = b''
```
with:
```python
self._serial_parse_buffer = bytearray()
```

- [ ] **Step 1.1.4: Update `_check_sync_state` — fix buffer reference**

In `_check_sync_state()` (line 781), replace:
```python
self._buffer = b''
```
with:
```python
self._serial_parse_buffer = bytearray()
```

---

### Task 1.2: Fix Beast escape sequence bug (C2)

**Problem:** `_convert_to_beast()` (line 1074) writes timestamp and signal bytes directly into `result` buffer without escaping `0x1A` bytes. If timestamp or signal contains `0x1A`, dump1090 will misparse the frame. `_create_beast_message()` (line 1034) correctly uses `_escape_beast_data()`. This is the root cause of C3 (duplication with diverging behavior).

**Files:**
- Modify: `picadsb-multiplexer.py:1074-1116`

- [ ] **Step 1.2.1: Rewrite `_convert_to_beast()` to use `_create_beast_message()`**

Replace the entire `_convert_to_beast()` method:

```python
def _convert_to_beast(self, message: bytes) -> Optional[bytes]:
    """Convert raw ADS-B message to Beast format."""
    try:
        raw_data = message[1:-1].strip(b';')
        try:
            data = bytes.fromhex(raw_data.decode())
        except ValueError as e:
            self.logger.debug(f"Hex conversion error: {e}")
            return None

        if len(data) == BeastFormat.MODES_SHORT_LEN:
            msg_type = BeastFormat.TYPE_MODES_SHORT
        elif len(data) == BeastFormat.MODES_LONG_LEN:
            msg_type = BeastFormat.TYPE_MODES_LONG
        elif len(data) == BeastFormat.MODEA_LEN:
            msg_type = BeastFormat.TYPE_MODEA
        else:
            self.logger.debug(f"Unsupported data length: {len(data)}")
            return None

        return self._create_beast_message(msg_type, data)

    except Exception as e:
        self.logger.error(f"Beast conversion error: {e}")
        return None
```

This eliminates C3 (duplication) as well — now there is exactly ONE Beast message creation path.

---

### Task 1.3: Fix health check false-positive loop (C4)

**Problem:** `health_check.sh` (line 26) greps for `"Health check failed"` in log files. `check_health()` (line 1393-1400) writes that exact string to logs. After one failure, ALL subsequent checks find the old log entry and always fail.

**Files:**
- Modify: `health_check.sh`
- Modify: `picadsb-multiplexer.py:1373-1419`

- [ ] **Step 1.3.1: Rewrite `health_check.sh` — remove log-grepping**

```bash
#!/bin/bash

# Health check script for ADS-B multiplexer

# Check if main process is running
if ! ps aux | grep "python3 -u picadsb-multiplexer.py" | grep -v grep > /dev/null; then
    echo "Process not running"
    exit 1
fi

# Check if serial device exists
if [ ! -e "${ADSB_DEVICE}" ]; then
    echo "Device ${ADSB_DEVICE} not found"
    exit 1
fi

# Check if TCP port is listening using ss (netstat is deprecated)
if ! ss -ln | grep -q ":${ADSB_TCP_PORT} "; then
    echo "Port ${ADSB_TCP_PORT} not listening"
    exit 1
fi

# All checks passed
exit 0
```

Changes:
1. Removed the log-grepping block entirely (lines 24-30)
2. Replaced `netstat` with `ss` (M7)

- [ ] **Step 1.3.2: Update Dockerfile — replace `net-tools` with `iproute2`**

In `Dockerfile` line 9, replace `net-tools` with `iproute2` (provides `ss`):

```dockerfile
RUN apt-get update && apt-get install -y \
    udev \
    logrotate \
    curl \
    procps \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*
```

---

### Task 1.4: Fix blocking reconnection (C5)

**Problem:** `_reconnect()` (line 733) calls `_init_serial()` which includes full `_initialize_device()` (12 commands × 1.9s = ~23s). With 999 attempts and 10s delay, worst case = ~9 hours blocking the main loop.

**Files:**
- Modify: `picadsb-multiplexer.py:733-767`

- [ ] **Step 1.4.1: Add exponential backoff and reduce max attempts**

```python
MAX_RECONNECT_ATTEMPTS = 50   # was 999
RECONNECT_DELAY = 5           # was 10, base delay
MAX_RECONNECT_DELAY = 300     # 5 minutes max delay

def _reconnect(self) -> bool:
    """Attempt to reconnect to the device with exponential backoff."""
    self.logger.info("Attempting to reconnect...")
    delay = self.RECONNECT_DELAY

    for attempt in range(self.MAX_RECONNECT_ATTEMPTS):
        try:
            self.logger.info(f"Reconnection attempt {attempt + 1}/{self.MAX_RECONNECT_ATTEMPTS} (delay: {delay}s)")

            if hasattr(self, 'ser') and self.ser.is_open:
                self.ser.close()

            time.sleep(delay)

            self._init_serial()

            for _ in range(3):
                self.ser.write(self.format_command(b'\x00'))
                response = self._read_response()
                if response and self.verify_response(b'\x00', response):
                    self.logger.info("Successfully reconnected to device")
                    self.stats['reconnects'] += 1
                    self._last_data_time = time.time()
                    self._no_data_logged = False
                    self._sync_state = True
                    return True
                time.sleep(0.1)

        except Exception as e:
            self.logger.error(f"Reconnection attempt {attempt + 1} failed: {e}")

        # Exponential backoff with cap
        delay = min(delay * 2, self.MAX_RECONNECT_DELAY)

    self.logger.error(f"Failed to reconnect after {self.MAX_RECONNECT_ATTEMPTS} attempts")
    return False
```

---

### Task 1.5: Remove dead code (M2)

**Files:**
- Modify: `picadsb-multiplexer.py`

- [ ] **Step 1.5.1: Remove unused attributes from `__init__`**

Remove these lines:
- Line 258: `self.firmware_version = None`
- Line 259: `self.device_id = None`
- Line 292: `self.last_mode_check = time.time()`
- Line 293: `self.mode_check_interval = 300`

- [ ] **Step 1.5.2: Remove duplicate constant**

Remove class constant (line 188):
```python
KEEPALIVE_INTERVAL = 30  # remove this, HEARTBEAT_INTERVAL is used instead
```

- [ ] **Step 1.5.3: Remove unused methods**

Remove these methods entirely:
- `_encode_beast_timestamp()` (lines 950-966) — duplicates `TimestampGenerator.get_timestamp()`
- `_validate_beast_message()` (lines 1118-1166) — never called
- `_validate_message_length()` (lines 1168-1182) — never called

- [ ] **Step 1.5.4: Remove duplicate timeout check**

In `_check_timeouts()` (line 1250), remove the no-data check (lines 1254-1256) since `_check_device_status()` already handles this. Keep only the client timeout check:

```python
def _check_timeouts(self):
    """Check for inactive clients and remove them."""
    current_time = time.time()
    for client in list(self.clients):
        if current_time - self.client_last_active.get(client, 0) > self.NO_DATA_TIMEOUT:
            self.logger.warning(f"Closing inactive client")
            self._remove_client(client)
```

Note: also remove `client.getpeername()` call in the warning — it can throw if client already disconnected.

---

### Task 1.6: Fix remote reconnect storm (M4)

**Problem:** `_send_to_remote()` (line 429) immediately calls `_connect_to_remote()` on error, bypassing the 60-second cooldown in `_connect_to_remote()` since `_last_connect_attempt` may not be set yet.

**Files:**
- Modify: `picadsb-multiplexer.py:411-429`

- [ ] **Step 1.6.1: Remove immediate reconnect from `_send_to_remote()`**

```python
def _send_to_remote(self, data: bytes):
    """Send data to remote server in Beast format."""
    if not self.remote_socket:
        return

    try:
        sent = self.remote_socket.send(data)
        if sent == 0:
            raise BrokenPipeError("Zero bytes sent")
        self.logger.debug(f"Sent to remote: {data.hex()}")
    except Exception as e:
        self.logger.error(f"Error sending to remote server: {e}")
        try:
            self.remote_socket.close()
        except Exception:
            pass
        self.remote_socket = None
        # Reconnection will happen via _check_remote_connection() on next cycle
```

- [ ] **Step 1.6.2: Initialize `_last_connect_attempt` in `__init__`**

Add to `__init__` after line 297:
```python
self._last_connect_attempt = 0
```

This removes the `hasattr` check in `_connect_to_remote()` (line 389).

- [ ] **Step 1.6.3: Simplify `_connect_to_remote()` — remove hasattr**

Replace lines 389-391:
```python
if hasattr(self, '_last_connect_attempt') and \
   current_time - self._last_connect_attempt < 60:
    return
```
with:
```python
if current_time - self._last_connect_attempt < 60:
    return
```

---

### Task 1.7: Phase 1 Review Cycle

- [ ] **Step 1.7.1: Run 3 parallel review agents** (code review, security review, architectural review)
- [ ] **Step 1.7.2: Fix any issues found**
- [ ] **Step 1.7.3: Re-review if critical issues were found**
- [ ] **Step 1.7.4: Update memory documentation**
- [ ] **Step 1.7.5: Local commit**

```bash
git add picadsb-multiplexer.py health_check.sh Dockerfile
git commit -m "fix: critical bugs — serial buffer persistence, Beast escaping, health check, reconnection

- Fix serial data loss on split reads (use persistent parse buffer)
- Eliminate Beast escape bug by unifying message creation path
- Remove log-grepping from health_check.sh (false-positive loop)
- Replace netstat with ss, net-tools with iproute2
- Add exponential backoff to reconnection (50 attempts, 5s-300s)
- Remove dead code: unused attributes, duplicate methods, duplicate timeout check
- Fix remote reconnect storm (defer to periodic check)"
```

---

## Chunk 2: Phase 2 — Security Hardening

### Task 2.1: Fix shell injection in entrypoint.sh (H1)

**Problem:** `entrypoint.sh` line 39-52 builds command string from env vars without quoting. `exec ${CMD}` splits on spaces — injection possible.

**Files:**
- Modify: `entrypoint.sh`

- [ ] **Step 2.1.1: Rewrite entrypoint.sh with proper quoting and exec array**

```bash
#!/bin/bash
set -euo pipefail

# Set defaults if environment variables are not set
MAX_LOG_SIZE="${MAX_LOG_SIZE:-100M}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-7}"

# Wait for device to appear
timeout=30
counter=0
while [ ! -e "${ADSB_DEVICE}" ] && [ "$counter" -lt "$timeout" ]; do
    echo "Waiting for device ${ADSB_DEVICE}..."
    sleep 1
    counter=$((counter + 1))
done

if [ ! -e "${ADSB_DEVICE}" ]; then
    echo "Error: Device ${ADSB_DEVICE} not found after ${timeout} seconds"
    exit 1
fi

# Check if logs directory exists and manage size
if [ -d "/app/logs" ]; then
    current_size=$(du -sm /app/logs | cut -f1)
    max_size="${MAX_LOG_SIZE%M}"

    if [ "$current_size" -gt "$max_size" ]; then
        echo "Log directory size ($current_size MB) exceeds limit ($max_size MB). Cleaning old logs..."
        find /app/logs -name "*.log" -type f -mtime +"${LOG_RETENTION_DAYS}" -delete
    fi
fi

# Build command as array for safe execution
CMD=(python3 -u picadsb-multiplexer.py
    --port "${ADSB_TCP_PORT}"
    --serial "${ADSB_DEVICE}"
    --log-level "${ADSB_LOG_LEVEL}"
)

# Add optional remote parameters only if both host and port are set
if [ -n "${ADSB_REMOTE_HOST:-}" ] && [ -n "${ADSB_REMOTE_PORT:-}" ]; then
    CMD+=(--remote-host "${ADSB_REMOTE_HOST}" --remote-port "${ADSB_REMOTE_PORT}")
fi

# Add no-init flag if set
if [ "${ADSB_NO_INIT:-false}" = "true" ]; then
    CMD+=(--no-init)
fi

# Execute the constructed command
exec "${CMD[@]}"
```

Changes:
1. `set -euo pipefail` — fail on errors
2. All variables quoted
3. Command built as bash array, executed with `"${CMD[@]}"`
4. `${ADSB_REMOTE_HOST:-}` — safe default for unset vars with `set -u`

---

### Task 2.2: Docker security hardening (H2)

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`

- [ ] **Step 2.2.1: Add non-root user to Dockerfile**

After `RUN mkdir -p /app/logs`, add:

```dockerfile
# Create non-root user
RUN groupadd -r adsb && useradd -r -g adsb -d /app -s /sbin/nologin adsb && \
    chown -R adsb:adsb /app

USER adsb
```

- [ ] **Step 2.2.2: Remove `chmod 666` from entrypoint.sh**

Remove line 36 (`chmod 666 ${ADSB_DEVICE}`) from entrypoint.sh — device permissions should be handled by Docker group mapping.

- [ ] **Step 2.2.3: Add security options to docker-compose.yml**

```yaml
services:
  picadsb-multiplexer:
    image: ghcr.io/smkrv/picadsb-multiplexer:latest
    container_name: picadsb-multiplexer
    restart: unless-stopped
    devices:
      - /dev/ttyACM0:/dev/ttyACM0
    ports:
      - "31002:31002"
    environment:
      - ADSB_TCP_PORT=31002
      - ADSB_DEVICE=/dev/ttyACM0
      - ADSB_LOG_LEVEL=INFO
      - ADSB_REMOTE_HOST=
      - ADSB_REMOTE_PORT=
    volumes:
      - ./logs:/app/logs
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    mem_limit: 256m
    pids_limit: 50
```

Note: `read_only: true` requires `/app/logs` as a mounted volume (already present) and the `USER adsb` in Dockerfile must have write access to the volume.

---

### Task 2.3: Add TCP client limit (H3, H4)

**Files:**
- Modify: `picadsb-multiplexer.py`

- [ ] **Step 2.3.1: Add MAX_CLIENTS constant and enforce in `_accept_new_client()`**

Add constant to class (after line 197):
```python
MAX_CLIENTS = 50
```

Modify `_accept_new_client()` (line 874):
```python
def _accept_new_client(self):
    """Accept new client connection with limit enforcement."""
    try:
        client_socket, address = self.server_socket.accept()

        if len(self.clients) >= self.MAX_CLIENTS:
            self.logger.warning(f"Max clients ({self.MAX_CLIENTS}) reached, rejecting {address}")
            client_socket.close()
            return

        self._configure_client_socket(client_socket)
        self.clients.append(client_socket)
        self.stats['clients_total'] += 1
        self.stats['clients_current'] = len(self.clients)
        self.logger.info(f"New client connected from {address} ({len(self.clients)}/{self.MAX_CLIENTS})")
    except Exception as e:
        self.logger.error(f"Error accepting client: {e}")
```

---

### Task 2.4: Pin GitHub Actions to SHA (H5)

**Files:**
- Modify: `.github/workflows/docker-publish.yml`

- [ ] **Step 2.4.1: Pin all actions to specific commit SHAs**

Look up current latest commit SHAs for each action and pin them. Example format:

```yaml
steps:
  - name: Checkout repository
    uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2

  - name: Set up QEMU
    uses: docker/setup-qemu-action@29109295f81e9208d7d86ff1c6c12d2833863392 # v3.6.0

  - name: Set up Docker Buildx
    uses: docker/setup-buildx-action@b5ca514318bd6ebac0fb2aedd5d36ec1b5c232a2 # v3.10.0

  - name: Log in to the Container registry
    uses: docker/login-action@74a5d142397b4f367a81961eba4e8cd7edddf772 # v3.4.0

  - name: Extract metadata (tags, labels) for Docker
    id: meta
    uses: docker/metadata-action@902fa8ec7d6ecbf8d84d538b9b233a880e428804 # v5.7.0

  - name: Build and push Docker image
    uses: docker/build-push-action@14487ce63c7a62a4a324b0bfb37086795e31c6c1 # v6.16.0
```

Note: actual SHAs must be verified at implementation time — use `gh api` to look up exact commit hashes for latest stable versions.

---

### Task 2.5: Pin Python dependencies (H6)

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 2.5.1: Pin all dependencies with upper bounds**

```
pyserial==3.5
pyModeS>=2.0,<3.0
psutil>=5.9.0,<7.0
```

---

### Task 2.6: Phase 2 Review Cycle

- [ ] **Step 2.6.1: Run 3 parallel review agents**
- [ ] **Step 2.6.2: Fix issues found**
- [ ] **Step 2.6.3: Re-review if critical**
- [ ] **Step 2.6.4: Update documentation**
- [ ] **Step 2.6.5: Local commit**

```bash
git add entrypoint.sh Dockerfile docker-compose.yml picadsb-multiplexer.py \
    .github/workflows/docker-publish.yml requirements.txt
git commit -m "security: harden Docker, shell scripts, TCP server, CI pipeline

- Fix shell injection in entrypoint.sh (bash array + set -euo pipefail)
- Add non-root user to Docker container
- Remove chmod 666 on device
- Add security_opt, cap_drop, mem/pid limits to compose
- Enforce MAX_CLIENTS=50 on TCP server
- Pin GitHub Actions to commit SHAs
- Pin pyModeS upper version bound"
```

---

## Chunk 3: Phase 3 — Architectural Refactoring (Config + Version)

### Task 3.1: Extract configuration into dataclass

**Problem:** Config values scattered across class constants (lines 188-197), `__init__` (lines 266-297), and magic numbers throughout the code. Version hardcoded as string `"v1.0"` (line 930).

**Files:**
- Create: `picadsb/config.py`
- Create: `picadsb/__init__.py`
- Modify: `picadsb-multiplexer.py`

- [ ] **Step 3.1.1: Create `picadsb/__init__.py` with version**

```python
"""PicADSB Multiplexer — TCP multiplexer for MicroADSB/adsbPIC USB receivers."""

__version__ = "2.0.0"
```

- [ ] **Step 3.1.2: Create `picadsb/config.py`**

```python
"""Configuration management for PicADSB Multiplexer."""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    """All configurable parameters in one place."""

    # Network
    tcp_port: int = 30002
    max_clients: int = 50
    listen_backlog: int = 10

    # Serial
    serial_port: str = "/dev/ttyACM0"
    serial_baudrate: int = 115200
    serial_timeout: float = 0.1
    serial_buffer_size: int = 131072
    skip_init: bool = False

    # Remote forwarding (optional)
    remote_host: Optional[str] = None
    remote_port: Optional[int] = None
    remote_check_interval: int = 60
    remote_reconnect_cooldown: int = 60

    # Timing
    heartbeat_interval: int = 30
    stats_interval: int = 60
    no_data_timeout: int = 600
    sync_check_interval: int = 1
    health_no_data_timeout: int = 14400
    health_startup_grace: int = 300

    # Reconnection
    max_reconnect_attempts: int = 50
    reconnect_delay: int = 5
    max_reconnect_delay: int = 300

    # Processing
    max_message_length: int = 256
    queue_maxsize: int = 5000
    signal_level: int = 0xFF

    # Logging
    log_level: str = "INFO"
    log_dir: str = "logs"

    @classmethod
    def from_env(cls) -> "Config":
        """Create Config from environment variables (for Docker)."""
        kwargs = {}
        env_map = {
            "ADSB_TCP_PORT": ("tcp_port", int),
            "ADSB_DEVICE": ("serial_port", str),
            "ADSB_LOG_LEVEL": ("log_level", str),
            "ADSB_REMOTE_HOST": ("remote_host", str),
            "ADSB_REMOTE_PORT": ("remote_port", int),
            "ADSB_NO_INIT": ("skip_init", lambda x: x.lower() == "true"),
            "ADSB_MAX_CLIENTS": ("max_clients", int),
        }
        for env_key, (attr, converter) in env_map.items():
            val = os.environ.get(env_key)
            if val:
                try:
                    kwargs[attr] = converter(val)
                except (ValueError, TypeError):
                    pass
        return cls(**kwargs)
```

- [ ] **Step 3.1.3: Refactor `PicADSBMultiplexer.__init__` to accept Config**

Replace the `__init__` signature and body to use `Config`:

```python
def __init__(self, config: 'Config' = None, **kwargs):
    """Initialize the multiplexer with given configuration."""
    if config is None:
        from picadsb.config import Config
        config = Config(**kwargs)
    self.config = config

    self._setup_logging(config.log_level)
    self.timestamp_gen = TimestampGenerator()

    # Runtime state
    self.running = True
    self._serial_parse_buffer = bytearray()
    self._last_data_time = time.time()
    self._no_data_logged = False
    self._sync_state = True
    self._last_sync_time = time.time()
    self.last_heartbeat = time.time()
    self._last_connect_attempt = 0

    # Statistics
    self.stats = { ... }  # same as before

    # Timing controls
    self.last_stats_update = time.time()
    self.last_remote_check = time.time()

    # Message handling
    self.message_queue = queue.Queue(maxsize=config.queue_maxsize)
    self.clients: List[socket.socket] = []
    self.client_last_active = {}

    # Initialize interfaces
    self._init_socket()
    self._init_serial()

    # Setup signal handlers
    signal.signal(signal.SIGINT, self._signal_handler)
    signal.signal(signal.SIGTERM, self._signal_handler)

    # Perform self-test
    if not self.self_test():
        raise RuntimeError("Self-test failed, aborting startup")
```

- [ ] **Step 3.1.4: Replace all magic numbers with `self.config.*` references**

Throughout the class, replace:
- `115200` → `self.config.serial_baudrate`
- `self.HEARTBEAT_INTERVAL` → `self.config.heartbeat_interval`
- `self.NO_DATA_TIMEOUT` → `self.config.no_data_timeout`
- `self.MAX_MESSAGE_LENGTH` → `self.config.max_message_length`
- `self.MAX_RECONNECT_ATTEMPTS` → `self.config.max_reconnect_attempts`
- `self.RECONNECT_DELAY` → `self.config.reconnect_delay`
- `self.MAX_RECONNECT_DELAY` → `self.config.max_reconnect_delay`
- `self.SERIAL_BUFFER_SIZE` → `self.config.serial_buffer_size`
- `self.SYNC_CHECK_INTERVAL` → `self.config.sync_check_interval`
- `self.MAX_CLIENTS` → `self.config.max_clients`
- `self.stats_interval` → `self.config.stats_interval`
- `self.remote_check_interval` → `self.config.remote_check_interval`
- `0xFF` signal level → `self.config.signal_level`
- `14400` → `self.config.health_no_data_timeout`
- `300` startup grace → `self.config.health_startup_grace`
- `listen(5)` → `listen(self.config.listen_backlog)`
- `"PicADSB Multiplexer v1.0\n"` → `f"PicADSB Multiplexer v{__version__}\n"` (import from `picadsb`)
- `timeout=0.1` → `timeout=self.config.serial_timeout`

- [ ] **Step 3.1.5: Remove old class constants that are now in Config**

Remove lines 188-197 (class constants block). They now live in `Config`.

- [ ] **Step 3.1.6: Update `__main__` block to use Config**

```python
if __name__ == '__main__':
    import argparse
    from picadsb import __version__
    from picadsb.config import Config

    parser = argparse.ArgumentParser(
        description=f'PicADSB Multiplexer v{__version__}',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument('--port', type=int, default=30002, help='TCP port')
    parser.add_argument('--serial', default='/dev/ttyACM0', help='Serial port')
    parser.add_argument('--log-level', default='INFO',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    parser.add_argument('--no-init', action='store_true')
    parser.add_argument('--max-clients', type=int, default=50)

    remote_group = parser.add_argument_group('Remote connection (optional)')
    remote_group.add_argument('--remote-host', metavar='HOST')
    remote_group.add_argument('--remote-port', metavar='PORT', type=int)

    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')

    args = parser.parse_args()

    if bool(args.remote_host) != bool(args.remote_port):
        parser.error("Both --remote-host and --remote-port must be specified together")

    config = Config(
        tcp_port=args.port,
        serial_port=args.serial,
        log_level=args.log_level,
        skip_init=args.no_init,
        remote_host=args.remote_host,
        remote_port=args.remote_port,
        max_clients=args.max_clients,
    )

    try:
        multiplexer = PicADSBMultiplexer(config=config)
        multiplexer.run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
```

---

### Task 3.2: Phase 3 Review Cycle

- [ ] **Step 3.2.1: Run 3 parallel review agents**
- [ ] **Step 3.2.2: Fix issues found**
- [ ] **Step 3.2.3: Update documentation**
- [ ] **Step 3.2.4: Local commit**

```bash
git add picadsb/ picadsb-multiplexer.py
git commit -m "refactor: extract Config dataclass, centralize all magic numbers

- Create picadsb/ package with __version__ and Config dataclass
- Replace 30+ magic numbers with config references
- Add --version flag and --max-clients CLI arg
- Support Config.from_env() for Docker usage"
```

---

## Chunk 4: Phase 4 — Logging, psutil fix, adsb_message_parser.py into Docker

### Task 4.1: Fix psutil deferred import (M6)

**Files:**
- Modify: `picadsb-multiplexer.py`

- [ ] **Step 4.1.1: Move psutil import to top of file and handle ImportError gracefully**

Add at top of file (after line 42):
```python
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
```

Replace lines 835-840 in `_update_stats()`:
```python
# Get process memory usage
if HAS_PSUTIL:
    process = psutil.Process()
    memory_mb = process.memory_info().rss / 1024 / 1024
else:
    memory_mb = 0
```

---

### Task 4.2: Add log rotation in Python

**Files:**
- Modify: `picadsb-multiplexer.py:199-238`

- [ ] **Step 4.2.1: Replace FileHandler with RotatingFileHandler**

```python
from logging.handlers import RotatingFileHandler

def _setup_logging(self, log_level: str):
    """Configure logging with rotation."""
    numeric_level = getattr(logging, log_level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f'Invalid log level: {log_level}')

    self.logger = logging.getLogger('PicADSB')
    self.logger.setLevel(numeric_level)
    self.logger.handlers = []

    formatter = logging.Formatter(
        '%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
        '%Y-%m-%d %H:%M:%S'
    )

    # Rotating file handler: 10MB per file, keep 5 backups
    os.makedirs(self.config.log_dir, exist_ok=True)
    fh = RotatingFileHandler(
        os.path.join(self.config.log_dir, 'picadsb.log'),
        maxBytes=10 * 1024 * 1024,
        backupCount=5
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(numeric_level)
    ch.setFormatter(formatter)

    self.logger.addHandler(fh)
    self.logger.addHandler(ch)
    self.logger.propagate = False
```

Changes:
1. `RotatingFileHandler` replaces `FileHandler` — handles rotation natively
2. Fixed log filename (no timestamp) — rotated files get `.1`, `.2` suffixes
3. Uses `self.config.log_dir`

---

### Task 4.3: Copy adsb_message_parser.py into Docker (M8)

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 4.3.1: Add COPY for adsb_message_parser.py**

After line 25 (`COPY picadsb-multiplexer.py .`), add:
```dockerfile
COPY adsb_message_parser.py .
COPY picadsb/ ./picadsb/
```

---

### Task 4.4: Phase 4 Review Cycle

- [ ] **Step 4.4.1: Run 3 parallel review agents**
- [ ] **Step 4.4.2: Fix issues found**
- [ ] **Step 4.4.3: Update documentation**
- [ ] **Step 4.4.4: Local commit**

```bash
git add picadsb-multiplexer.py Dockerfile
git commit -m "fix: proper psutil import, log rotation, Docker includes all files

- Move psutil to top-level import with graceful fallback
- Replace FileHandler with RotatingFileHandler (10MB, 5 backups)
- Copy adsb_message_parser.py and picadsb/ package into Docker image"
```

---

## Chunk 5: Phase 5 — Final Comprehensive Review + Tests + CI/CD

### Task 5.1: Final 3-agent comprehensive review

- [ ] **Step 5.1.1: Run Code Review agent** — full scan of all files, focusing on bugs, logic errors, HA conventions
- [ ] **Step 5.1.2: Run Security Review agent** — full security audit including all changes from phases 1-4
- [ ] **Step 5.1.3: Run Architectural Review agent** — coupling, SoC, scalability, completeness
- [ ] **Step 5.1.4: Fix any remaining issues found**
- [ ] **Step 5.1.5: Re-review if any critical issues**

---

### Task 5.2: Add unit tests

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_crc24.py`
- Create: `tests/test_beast.py`
- Create: `tests/test_config.py`
- Create: `tests/test_parser.py`
- Create: `pytest.ini` or `pyproject.toml` (test config)

- [ ] **Step 5.2.1: Create pytest configuration**

Create `pyproject.toml`:
```toml
[project]
name = "picadsb-multiplexer"
version = "2.0.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
addopts = "-v --tb=short"
```

- [ ] **Step 5.2.2: Create `tests/__init__.py`**

Empty file.

- [ ] **Step 5.2.3: Create `tests/test_crc24.py`**

```python
"""Tests for CRC24 implementation."""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the class under test
# We need to import carefully since picadsb-multiplexer.py has a hyphen
import importlib
spec = importlib.util.spec_from_file_location(
    "picadsb_multiplexer",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "picadsb-multiplexer.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CRC24 = mod.CRC24


class TestCRC24:
    """Tests for CRC-24 computation and verification."""

    def test_compute_valid_df17(self):
        """Known valid DF17 message should have CRC remainder 0."""
        import pyModeS as pms
        hex_msg = "8D406B902015A678D4D220AA4BDA"
        remainder = pms.common.crc(hex_msg)
        assert remainder == 0

    def test_compute_invalid_df17(self):
        """Known invalid DF17 message should have non-zero remainder."""
        import pyModeS as pms
        hex_msg = "8D4CA251204994B1C36E60A5343D"
        remainder = pms.common.crc(hex_msg)
        assert remainder != 0

    def test_compute_returns_byte(self):
        """CRC compute should return value in 0-255 range."""
        data = bytes.fromhex("8D406B902015A678D4D220AA4BDA")
        result = CRC24.compute(data)
        assert 0 <= result <= 255

    def test_compute_short_data_returns_zero(self):
        """Data shorter than 3 bytes should return 0."""
        assert CRC24.compute(b'\x00\x01') == 0

    def test_verify_valid_message(self):
        """Verify should return True for valid message with correct CRC."""
        # Build a message with known CRC
        data = bytes.fromhex("8D406B902015A678D4D220AA4BDA")
        # Pad to minimum length (11 bytes for Beast)
        padded = b'\x00' * 4 + data  # type + timestamp(6) needs padding
        crc = CRC24.compute(padded)
        full_msg = padded + bytes([crc])
        assert CRC24.verify(full_msg) is True

    def test_verify_short_message(self):
        """Messages shorter than 11 bytes should fail verification."""
        assert CRC24.verify(b'\x00' * 10) is False
```

- [ ] **Step 5.2.4: Create `tests/test_beast.py`**

```python
"""Tests for Beast format encoding."""
import pytest
import sys
import os
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

spec = importlib.util.spec_from_file_location(
    "picadsb_multiplexer",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "picadsb-multiplexer.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

BeastFormat = mod.BeastFormat
TimestampGenerator = mod.TimestampGenerator


class TestBeastFormat:
    """Tests for Beast format constants."""

    def test_escape_byte(self):
        assert BeastFormat.ESCAPE == 0x1A

    def test_message_types(self):
        assert BeastFormat.TYPE_MODEA == 0x31
        assert BeastFormat.TYPE_MODES_SHORT == 0x32
        assert BeastFormat.TYPE_MODES_LONG == 0x33

    def test_message_lengths(self):
        assert BeastFormat.MODES_SHORT_LEN == 7
        assert BeastFormat.MODES_LONG_LEN == 14
        assert BeastFormat.MODEA_LEN == 2


class TestTimestampGenerator:
    """Tests for MLAT timestamp generation."""

    def test_returns_6_bytes(self):
        gen = TimestampGenerator()
        ts = gen.get_timestamp()
        assert len(ts) == 6

    def test_monotonic(self):
        gen = TimestampGenerator()
        ts1 = gen.get_timestamp()
        ts2 = gen.get_timestamp()
        assert int.from_bytes(ts2, 'big') >= int.from_bytes(ts1, 'big')

    def test_no_zero_after_first(self):
        gen = TimestampGenerator()
        _ = gen.get_timestamp()
        ts = gen.get_timestamp()
        assert int.from_bytes(ts, 'big') > 0
```

- [ ] **Step 5.2.5: Create `tests/test_config.py`**

```python
"""Tests for Config dataclass."""
import pytest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from picadsb.config import Config


class TestConfig:
    """Tests for configuration management."""

    def test_defaults(self):
        c = Config()
        assert c.tcp_port == 30002
        assert c.serial_port == "/dev/ttyACM0"
        assert c.max_clients == 50
        assert c.log_level == "INFO"

    def test_custom_values(self):
        c = Config(tcp_port=31002, max_clients=100)
        assert c.tcp_port == 31002
        assert c.max_clients == 100

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("ADSB_TCP_PORT", "31002")
        monkeypatch.setenv("ADSB_DEVICE", "/dev/ttyUSB0")
        monkeypatch.setenv("ADSB_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("ADSB_NO_INIT", "true")

        c = Config.from_env()
        assert c.tcp_port == 31002
        assert c.serial_port == "/dev/ttyUSB0"
        assert c.log_level == "DEBUG"
        assert c.skip_init is True

    def test_from_env_ignores_empty(self, monkeypatch):
        monkeypatch.setenv("ADSB_REMOTE_HOST", "")
        c = Config.from_env()
        assert c.remote_host is None

    def test_from_env_ignores_invalid(self, monkeypatch):
        monkeypatch.setenv("ADSB_TCP_PORT", "not_a_number")
        c = Config.from_env()
        assert c.tcp_port == 30002  # default
```

- [ ] **Step 5.2.6: Create `tests/test_parser.py`**

```python
"""Tests for message validation."""
import pytest
import sys
import os
import importlib
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

spec = importlib.util.spec_from_file_location(
    "picadsb_multiplexer",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "picadsb-multiplexer.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

BeastFormat = mod.BeastFormat


class TestValidateMessage:
    """Test message validation logic without instantiating the full multiplexer."""

    @pytest.fixture
    def validator(self):
        """Create a minimal object with validate_message method."""
        obj = object.__new__(mod.PicADSBMultiplexer)
        obj.logger = MagicMock()
        return obj

    def test_valid_short_message(self, validator):
        """7-byte Mode-S short message."""
        msg = b'*' + b'8D406B902015A6' + b';'  # 7 hex bytes = 14 chars
        # Actually need valid hex: 7 bytes = 14 hex chars
        msg = b'*8D406B90201500;'  # 7 bytes
        assert validator.validate_message(msg) is True

    def test_valid_long_message(self, validator):
        """14-byte Mode-S long message."""
        msg = b'*8D406B902015A678D4D220AA4BDA;'  # 14 bytes = 28 hex chars
        assert validator.validate_message(msg) is True

    def test_too_short(self, validator):
        """Messages shorter than 3 bytes should be invalid."""
        assert validator.validate_message(b'*;') is False

    def test_no_start_marker(self, validator):
        """Messages without start marker should be invalid."""
        assert validator.validate_message(b'8D406B90;') is False

    def test_no_end_marker(self, validator):
        """Messages without semicolon should be invalid."""
        assert validator.validate_message(b'*8D406B90') is False

    def test_invalid_hex(self, validator):
        """Non-hex content should be invalid."""
        assert validator.validate_message(b'*ZZZZZZZZZZZZZZ;') is False

    def test_hash_message_valid(self, validator):
        """# prefixed messages should be valid."""
        assert validator.validate_message(b'#43-02;') is True

    def test_unsupported_length(self, validator):
        """Unsupported data length should be invalid."""
        msg = b'*8D406B90201500AA;'  # 8 bytes - not 2, 7, or 14
        assert validator.validate_message(msg) is False
```

- [ ] **Step 5.2.7: Run tests locally**

```bash
cd /Users/makarov/GitHub/picadsb-multiplexer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest
pytest tests/ -v
```

---

### Task 5.3: Add CI/CD test workflow

**Files:**
- Modify: `.github/workflows/docker-publish.yml`
- Create: `.github/workflows/test.yml`

- [ ] **Step 5.3.1: Create test workflow**

```yaml
name: Tests

on:
  push:
    branches: ["main"]
  pull_request:
    branches: ["main"]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]

    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2

      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: ${{ matrix.python-version }}

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          pip install pytest

      - name: Run tests
        run: pytest tests/ -v --tb=short
```

Note: SHA for setup-python must be verified at implementation time.

- [ ] **Step 5.3.2: Add test requirement to Docker build workflow**

In `docker-publish.yml`, add `needs: test` if test job is in a separate workflow, or add a test step before the build step. Simplest: make Docker workflow depend on test workflow passing by adding test as a job:

Add before `build` job:
```yaml
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: "3.11"
      - run: |
          pip install -r requirements.txt pytest
          pytest tests/ -v --tb=short

  build:
    needs: test
    runs-on: ubuntu-latest
    ...
```

---

### Task 5.4: Update .gitignore for new structure

**Files:**
- Modify: `.gitignore`

- [ ] **Step 5.4.1: Add test and venv patterns**

Append to `.gitignore`:
```
# Virtual environment
.venv/
venv/

# Test artifacts
.pytest_cache/
__pycache__/
*.pyc

# IDE
.vscode/
.idea/
```

---

### Task 5.5: Final commit and documentation

- [ ] **Step 5.5.1: Update memory with final project state**
- [ ] **Step 5.5.2: Local commit**

```bash
git add tests/ pyproject.toml .github/workflows/ .gitignore
git commit -m "test: add unit tests and CI/CD pipeline

- Add pytest tests for CRC24, BeastFormat, TimestampGenerator, Config, message validation
- Add GitHub Actions test workflow (Python 3.11, 3.12)
- Add test gate before Docker build in CI
- Update .gitignore for test artifacts and venv"
```

- [ ] **Step 5.5.3: Final review — 3 agents**
- [ ] **Step 5.5.4: Fix any remaining issues, commit if needed**
- [ ] **Step 5.5.5: Report ready for push**

---

## Summary of Commits

| # | Phase | Commit message |
|---|---|---|
| 1 | Critical fixes | `fix: critical bugs — serial buffer, Beast escaping, health check, reconnection` |
| 2 | Security | `security: harden Docker, shell scripts, TCP server, CI pipeline` |
| 3 | Config refactor | `refactor: extract Config dataclass, centralize all magic numbers` |
| 4 | Logging + Docker | `fix: proper psutil import, log rotation, Docker includes all files` |
| 5 | Tests + CI/CD | `test: add unit tests and CI/CD pipeline` |

After all phases: `git push origin main`

---

## Cross-Session Continuity

This plan spans multiple sessions. Progress is tracked via:
1. **This document** — checkboxes track completion
2. **Memory files** — `memory/project_overview.md` updated after each phase
3. **Git commits** — each phase produces a local commit
4. **Git log** — shows which phases are completed

To resume: check `git log --oneline` and this document's checkboxes.
