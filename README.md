# ADS-B Multiplexer for MicroADSB | adsbPIC by Sprut

A Docker-based TCP multiplexer for MicroADSB (adsbPIC) USB receivers that allows sharing ADS-B data with multiple clients (like dump1090).

## Features

- Supports MicroADSB USB receivers (microADSB / adsbPIC)
- Handles multiple client connections
- Processes ADS-B messages in raw format
- Provides real-time statistics and monitoring
- Configurable TCP port and device settings
- Docker support with automatic builds
- Proper device initialization and error handling
- Automatic reconnection on device errors

## Device Specifications
- Original adsbPIC creator: [Sprut](https://sprut.de/electronic/pic/projekte/adsb/adsb_en.html)
- Device: MicroADSB USB receiver (≈2010-2014)
- Manufacturer: Miroslav Ionov
  - MicroADSB.com (website no longer available)
  - Anteni.net (active as of Feb 2025)
- Microcontroller: PIC18F2550
- Firmware: [Sprut](https://sprut.de)
- Maximum theoretical frame rate: 200,000 fpm
- Practical maximum frame rate: ≈6,000 fpm
- Communication: USB CDC (115200 baud)
- Message format: ASCII, prefixed with '*', terminated with ';'

## Requirements

- Docker
- Docker Compose (optional)
- USB port for the MicroADSB device
- Linux system with udev support

## Quick Start

### Using Docker Compose

1. Create a `docker-compose.yml`:
```yaml
version: '3.8'

services:
  adsb-muxer:
    image: ghcr.io/smkrv/picadsb-multiplexer:latest
    container_name: picadsb-multiplexer
    restart: unless-stopped
    devices:
      - /dev/ttyACM0:/dev/ttyACM0
    ports:
      - "30002:30002"
    environment:
      - ADSB_TCP_PORT=30002
      - ADSB_DEVICE=/dev/ttyACM0
      - ADSB_LOG_LEVEL=INFO
    volumes:
      - ./logs:/app/logs
```

2. Start the service:
```bash
docker-compose up -d
```

### Using Docker Run

```bash
docker run -d \
    --name adsb-muxer \
    --device=/dev/ttyACM0:/dev/ttyACM0 \
    -p 30002:30002 \
    -e ADSB_TCP_PORT=30002 \
    -e ADSB_DEVICE=/dev/ttyACM0 \
    -e ADSB_LOG_LEVEL=INFO \
    -v $(pwd)/logs:/app/logs \
    ghcr.io/smkrv/picadsb-multiplexer:latest
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| ADSB_TCP_PORT | TCP port for client connections | 30002 |
| ADSB_DEVICE | Path to USB device | /dev/ttyACM0 |
| ADSB_LOG_LEVEL | Logging level (DEBUG, INFO, WARNING, ERROR) | INFO |

### Device Permissions

The container needs access to the USB device. Make sure the device is properly mapped in the Docker configuration:

```yaml
devices:
  - /dev/ttyACM0:/dev/ttyACM0
```

You might need to add udev rules on the host system:

```bash
# Create a new udev rule
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="04d8", ATTRS{idProduct}=="000a", MODE="0666"' | \
sudo tee /etc/udev/rules.d/99-microadsb.rules

# Reload udev rules
sudo udevadm control --reload-rules
sudo udevadm trigger
```

## Integration with dump1090

### Using Docker

```bash
# Start dump1090 with network input from multiplexer
docker run -d \
    --name dump1090 \
    --network host \
    antirez/dump1090 \
    --net-only \
    --net-ri-port 30002
```

### Using Native Installation

```bash
dump1090 --net-only --net-ri-port 30002
```

## Building from Source

1. Clone the repository:
```bash
git clone https://github.com/smkrv/picadsb-multiplexer.git
cd picadsb-multiplexer
```

2. Build the Docker image:
```bash
docker build -t picadsb-multiplexer .
```

## Development

### Project Structure

```
.
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── picadsb-multiplexer.py
├── entrypoint.sh
├── .dockerignore
└── .github
    └── workflows
        └── docker-publish.yml
```

### Running Tests

```bash
# Run the multiplexer in debug mode
docker run -it --rm \
    --device=/dev/ttyACM0:/dev/ttyACM0 \
    -e ADSB_LOG_LEVEL=DEBUG \
    adsb-muxer
```

## Monitoring

### Logs

Logs are stored in the `logs` directory:
- `picadsb-multiplexer_YYYYMMDD_HHMMSS.log`: Detailed application log
- Container logs can be viewed with `docker logs adsb-muxer`

### Statistics

The multiplexer maintains real-time statistics:
- Messages processed
- Messages per minute
- Client connections
- Error counts

## Troubleshooting

### Common Issues

1. Device not found:
```bash
# Check device presence
ls -l /dev/ttyACM*

# Check USB device
lsusb | grep "04d8:000a"
```

2. Permission denied:
```bash
# Add current user to dialout group
sudo usermod -a -G dialout $USER
```

3. Connection refused:
```bash
# Check if port is open
netstat -tuln | grep 30002
```

### Debug Mode

Enable debug logging:
```bash
docker-compose up -d -e ADSB_LOG_LEVEL=DEBUG
```

## Contributing

1. Fork the repository
2. Create your feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## Acknowledgments

- Original adsbPIC creator & firmware by [Sprut](https://sprut.de)
- Original MicroADSB device by Miroslav Ionov
- [dump1090](https://github.com/antirez/dump1090) project for ADS-B decoding

## Legal Disclaimer and Limitation of Liability  

### Software Disclaimer  

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,   
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A   
PARTICULAR PURPOSE AND NONINFRINGEMENT.  

IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,   
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,   
ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER   
DEALINGS IN THE SOFTWARE.  

## 📝 License

Author: SMKRV
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/) - see [LICENSE](LICENSE) for details.

## 💡 Support the Project

The best support is:
- Sharing feedback
- Contributing ideas
- Recommending to friends
- Reporting issues
- Star the repository

If you want to say thanks financially, you can send a small token of appreciation in USDT:

**USDT Wallet (TRC10/TRC20):**
`TXC9zYHYPfWUGi4Sv4R1ctTBGScXXQk5HZ`

*Open-source is built by community passion!* 🚀

---

<div align="center">

Made with ❤️ for the Aviation & Radio Enthusiasts Community

[Report Bug](https://github.com/smkrv/picadsb-multiplexer/issues) · [Request Feature](https://github.com/smkrv/picadsb-multiplexer/issues)

</div>
