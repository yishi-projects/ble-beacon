import asyncio
from bleak import BleakScanner
import struct
import uuid
import time
import socket
import platform
import os
import uuid as system_uuid
import json
from kafka import KafkaProducer
from kafka.errors import KafkaError
import datetime
import configparser

# Callback function for GUI updates - will be set by the GUI
_gui_callback = None

# Add a global variable to control scanning
_scanning_active = False

def set_gui_callback(callback_func):
    """Set the callback function for GUI updates."""
    global _gui_callback
    print(f"DEBUG: Setting GUI callback: {callback_func}")
    _gui_callback = callback_func

# Load configuration from file
def load_config():
    """Load configuration from ~/.ble/config.conf or create with defaults if it doesn't exist."""
    config = configparser.ConfigParser()
    
    # Default configuration
    config['kafka'] = {
        'broker': 'localhost:9092',
        'topic': 'ble_beacons'
    }
    
    # Create config directory if it doesn't exist
    config_dir = os.path.expanduser("~/.ble")
    os.makedirs(config_dir, exist_ok=True)
    
    config_file = os.path.join(config_dir, "config.conf")
    
    # If config file exists, read it
    if os.path.exists(config_file):
        try:
            config.read(config_file)
            print(f"DEBUG: Loaded configuration from {config_file}")
        except Exception as e:
            print(f"DEBUG: Error reading config file: {e}")
    else:
        # Create default config file
        try:
            with open(config_file, 'w') as f:
                config.write(f)
            print(f"DEBUG: Created default configuration at {config_file}")
        except Exception as e:
            print(f"DEBUG: Error creating config file: {e}")
    
    return config

# Load configuration
config = load_config()

# Kafka configuration
KAFKA_BROKER = os.environ.get('KAFKA_BROKER', config['kafka']['broker'])
KAFKA_TOPIC = os.environ.get('KAFKA_TOPIC', config['kafka']['topic'])

def create_kafka_producer():
    """Create a Kafka producer with error handling."""
    print(f"DEBUG: Creating Kafka producer with broker {KAFKA_BROKER}")
    try:
        producer = KafkaProducer(
            bootstrap_servers=[KAFKA_BROKER],
            value_serializer=lambda v: json.dumps(v).encode('utf-8')
        )
        print("DEBUG: Kafka producer created successfully")
        return producer
    except Exception as e:
        print(f"DEBUG: Error creating Kafka producer: {e}")
        return None

def get_host_id():
    """Get a unique host ID that persists across reboots."""
    print("DEBUG: Getting host ID")
    try:
        if platform.system() == 'Darwin':  # macOS
            # Use the hardware UUID on macOS
            cmd = 'ioreg -rd1 -c IOPlatformExpertDevice | grep -i "UUID" | cut -c27-62'
            import subprocess
            result = subprocess.check_output(cmd, shell=True).decode('utf-8').strip()
            print(f"DEBUG: Got macOS hardware UUID: {result}")
            return result
        else:
            # Fallback to hostname + first MAC address
            hostname = socket.gethostname()
            print(f"DEBUG: Using hostname for host ID: {hostname}")
            return hostname
    except Exception as e:
        print(f"DEBUG: Error getting host ID: {e}")
        # Generate a random UUID as fallback
        fallback_id = str(system_uuid.uuid4())
        print(f"DEBUG: Using fallback random UUID: {fallback_id}")
        return fallback_id

def process_beacon_data(producer, beacon_type, beacon_data, host_id, timestamp):
    """Process beacon data and send to Kafka."""
    print(f"DEBUG: Processing beacon data: type={beacon_type}, data={beacon_data}")
    
    # Add common fields
    message = {
        'type': beacon_type,
        'host_id': host_id,
        'timestamp': timestamp,
        'rssi': beacon_data.get('rssi', 0),
        'address': beacon_data.get('address', 'unknown')
    }
    
    # Add type-specific fields
    message.update(beacon_data)
    
    # Call GUI callback if set
    if _gui_callback:
        print(f"DEBUG: Calling GUI callback with {beacon_type} and data")
        try:
            _gui_callback(beacon_type, beacon_data)
            print("DEBUG: GUI callback completed successfully")
        except Exception as e:
            print(f"DEBUG: Error in GUI callback: {e}")
            import traceback
            print(traceback.format_exc())
    else:
        print("DEBUG: No GUI callback set")
    
    # Send to Kafka if producer is available
    if producer:
        print(f"DEBUG: Sending to Kafka topic {KAFKA_TOPIC}")
        try:
            future = producer.send(KAFKA_TOPIC, message)
            producer.flush()
            print("DEBUG: Message sent to Kafka")
        except Exception as e:
            print(f"DEBUG: Error sending to Kafka: {e}")
    else:
        print("DEBUG: No Kafka producer available")
    
    return message

def create_beacon_processor(host_id, producer):
    """Create a beacon processor that captures the host ID and producer."""
    def process_beacon(beacon_type, beacon_data):
        timestamp = datetime.datetime.now().isoformat()
        return process_beacon_data(producer, beacon_type, beacon_data, host_id, timestamp)
    return process_beacon

async def scan_ble_devices():
    """Scan for BLE devices and process beacon data."""
    print("DEBUG: Starting BLE scan")
    
    # Get host ID
    host_id = get_host_id()
    print(f"DEBUG: Host ID: {host_id}")
    
    # Create Kafka producer
    producer = create_kafka_producer()
    
    # Create beacon processor
    process_beacon = create_beacon_processor(host_id, producer)
    
    # Counter for logging
    scan_count = 0
    
    # Flag to check if scanning should continue
    # This will be checked by the GUI thread
    global _scanning_active
    _scanning_active = True
    
    try:
        print("DEBUG: Starting continuous scan loop")
        while _scanning_active:
            scan_count += 1
            print(f"DEBUG: Starting scan #{scan_count}")
            
            # Scan for devices
            devices = await BleakScanner.discover(timeout=1.0)
            print(f"DEBUG: Found {len(devices)} devices in scan #{scan_count}")
            
            # Check if scanning should stop
            if not _scanning_active:
                print("DEBUG: Scanning stopped by user")
                break
            
            # Process each device
            beacons_found = 0
            for device in devices:
                print(f"DEBUG: Processing device: {device.address} ({device.name}), RSSI: {device.rssi}")
                
                # Extract manufacturer data
                if device.metadata.get('manufacturer_data'):
                    for company_code, data in device.metadata['manufacturer_data'].items():
                        print(f"DEBUG: Found manufacturer data for company code {company_code}")
                        
                        # Check for iBeacon (Apple's company code is 0x004C)
                        if company_code == 0x004C and len(data) >= 23:
                            try:
                                # Check for iBeacon identifier (0x02, 0x15)
                                if data[0] == 0x02 and data[1] == 0x15:
                                    # Parse iBeacon data
                                    uuid_bytes = data[2:18]
                                    uuid_str = str(uuid.UUID(bytes=bytes(uuid_bytes)))
                                    major = int.from_bytes(data[18:20], byteorder='big')
                                    minor = int.from_bytes(data[20:22], byteorder='big')
                                    tx_power = data[22] - 256 if data[22] > 127 else data[22]
                                    
                                    beacon_data = {
                                        'uuid': uuid_str,
                                        'major': major,
                                        'minor': minor,
                                        'tx_power': tx_power,
                                        'rssi': device.rssi,
                                        'address': device.address,
                                        'name': device.name or 'Unknown'
                                    }
                                    
                                    print(f"DEBUG: Found iBeacon: UUID={uuid_str}, Major={major}, Minor={minor}, RSSI={device.rssi}")
                                    process_beacon('iBeacon', beacon_data)
                                    beacons_found += 1
                            except Exception as e:
                                print(f"DEBUG: Error processing iBeacon data: {e}")
                                import traceback
                                print(traceback.format_exc())
                        
                        # Check for Eddystone beacons
                        elif company_code == 0x00AA and len(data) >= 20:  # Google's company code
                            try:
                                # Check for Eddystone identifier
                                if data[0] == 0xAA and data[1] == 0xFE:
                                    frame_type = data[2]
                                    
                                    if frame_type == 0x00:  # Eddystone-UID
                                        namespace = bytes(data[3:13]).hex()
                                        instance = bytes(data[13:19]).hex()
                                        
                                        beacon_data = {
                                            'namespace': namespace,
                                            'instance': instance,
                                            'rssi': device.rssi,
                                            'address': device.address,
                                            'name': device.name or 'Unknown'
                                        }
                                        
                                        print(f"DEBUG: Found Eddystone-UID: Namespace={namespace}, Instance={instance}, RSSI={device.rssi}")
                                        process_beacon('Eddystone-UID', beacon_data)
                                        beacons_found += 1
                                    
                                    elif frame_type == 0x10:  # Eddystone-URL
                                        url_scheme = ['http://www.', 'https://www.', 'http://', 'https://'][data[3]]
                                        url_data = bytes(data[4:]).decode('ascii')
                                        url = url_scheme + url_data
                                        
                                        beacon_data = {
                                            'url': url,
                                            'rssi': device.rssi,
                                            'address': device.address,
                                            'name': device.name or 'Unknown'
                                        }
                                        
                                        print(f"DEBUG: Found Eddystone-URL: URL={url}, RSSI={device.rssi}")
                                        process_beacon('Eddystone-URL', beacon_data)
                                        beacons_found += 1
                            except Exception as e:
                                print(f"DEBUG: Error processing Eddystone data: {e}")
                                import traceback
                                print(traceback.format_exc())
                        
                        # Check for AltBeacon
                        elif len(data) >= 24:
                            try:
                                # AltBeacon has a different structure but similar concept
                                beacon_id = bytes(data[2:22]).hex()
                                
                                beacon_data = {
                                    'beacon_id': beacon_id,
                                    'rssi': device.rssi,
                                    'address': device.address,
                                    'name': device.name or 'Unknown'
                                }
                                
                                print(f"DEBUG: Found possible AltBeacon: ID={beacon_id}, RSSI={device.rssi}")
                                process_beacon('AltBeacon', beacon_data)
                                beacons_found += 1
                            except Exception as e:
                                print(f"DEBUG: Error processing AltBeacon data: {e}")
                                import traceback
                                print(traceback.format_exc())
            
            print(f"DEBUG: Scan #{scan_count} found {beacons_found} beacons")
            
            # Wait before next scan
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        print("DEBUG: BLE scan was cancelled")
        raise
    except Exception as e:
        print(f"DEBUG: Error in BLE scan: {e}")
        import traceback
        print(traceback.format_exc())
    finally:
        print("DEBUG: BLE scan ended")
        if producer:
            producer.close()
            print("DEBUG: Kafka producer closed")

# Add a function to stop scanning
def stop_scanning():
    """Stop the BLE scanning process."""
    global _scanning_active
    print("DEBUG: Stopping scanning")
    _scanning_active = False

def reload_config():
    """Reload configuration from file."""
    global KAFKA_BROKER, KAFKA_TOPIC, config
    
    # Reload configuration
    config = load_config()
    
    # Update global variables
    KAFKA_BROKER = os.environ.get('KAFKA_BROKER', config['kafka']['broker'])
    KAFKA_TOPIC = os.environ.get('KAFKA_TOPIC', config['kafka']['topic'])
    
    print(f"DEBUG: Reloaded configuration - Kafka broker: {KAFKA_BROKER}, topic: {KAFKA_TOPIC}")
    
    return config

if __name__ == "__main__":
    asyncio.run(scan_ble_devices())