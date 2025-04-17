import os
import json
import paramiko
import subprocess
import threading
import time
import secrets
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_socketio import SocketIO

# Configuration
CONFIG_DIR = os.path.expanduser("/data")
SSH_DIR = os.path.join(CONFIG_DIR, ".ssh")

# Ensure directories exist with proper permissions
os.makedirs(SSH_DIR, exist_ok=True)
try:
    os.chmod(SSH_DIR, 0o700)  # Set proper permissions for SSH directory
except Exception as e:
    print(f"Warning: Could not set permissions on SSH directory: {e}")

app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(16)
socketio = SocketIO(app)

class SSHConnection:
    def __init__(self, name, local_port, remote_port, vps_ip, vps_user, key_path, 
                 ssh_port=22, alive_interval=60, exit_on_failure=True):
        self.name = name
        self.local_port = local_port
        self.remote_port = remote_port
        self.vps_ip = vps_ip
        self.vps_user = vps_user
        self.key_path = key_path
        self.ssh_port = ssh_port
        self.alive_interval = alive_interval
        self.exit_on_failure = exit_on_failure
        self.process = None
        self.active = False

    def start(self):
        if self.process and self.process.poll() is None:
            return False

        # Ensure key has correct permissions
        try:
            os.chmod(self.key_path, 0o600)
        except Exception as e:
            print(f"Warning: Could not set permissions on key file: {e}")

        cmd = [
            "ssh", 
            "-o", "StrictHostKeyChecking=no",
            "-N", 
            "-R", f"{self.remote_port}:localhost:{self.local_port}",
            "-i", self.key_path,
            "-p", str(self.ssh_port),
            "-o", f"ServerAliveInterval={self.alive_interval}",
            "-o", f"ExitOnForwardFailure={'yes' if self.exit_on_failure else 'no'}"
        ]

        if self.vps_user:
            cmd.append(f"{self.vps_user}@{self.vps_ip}")
        else:
            cmd.append(self.vps_ip)

        self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.active = True
        return True

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
                if self.process.poll() is None:
                    self.process.kill()
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.active = False

    def is_active(self):
        if self.process:
            return self.process.poll() is None
        return False

    def to_dict(self):
        return {
            "name": self.name,
            "local_port": self.local_port,
            "remote_port": self.remote_port,
            "vps_ip": self.vps_ip,
            "vps_user": self.vps_user,
            "key_path": self.key_path,
            "ssh_port": self.ssh_port,
            "alive_interval": self.alive_interval,
            "exit_on_failure": self.exit_on_failure,
            "active": self.is_active()
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["name"],
            data["local_port"],
            data["remote_port"],
            data["vps_ip"],
            data["vps_user"],
            data["key_path"],
            data.get("ssh_port", 22),
            data.get("alive_interval", 60),
            data.get("exit_on_failure", True)
        )

# Global connections dictionary
connections = {}
config_file = os.path.join(CONFIG_DIR, "ssh_tunnel_manager.json")

def load_connections():
    global connections
    if not os.path.exists(config_file):
        return

    try:
        with open(config_file, "r") as f:
            data = json.load(f)

        connections = {name: SSHConnection.from_dict(conn_data) 
                      for name, conn_data in data.items()}
    except Exception as e:
        print(f"Failed to load connections: {str(e)}")

def save_connections():
    data = {name: conn.to_dict() for name, conn in connections.items()}
    try:
        with open(config_file, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Failed to save connections: {str(e)}")

# Connection monitor thread
def connection_monitor():
    while True:
        status_changed = False
        for conn in connections.values():
            was_active = conn.active
            is_active_now = conn.is_active()

            if was_active and not is_active_now:
                # Try to restart if it died
                conn.start()
                status_changed = True
            elif was_active != is_active_now:
                status_changed = True

        if status_changed:
            socketio.emit('status_update', get_connections_list())

        time.sleep(5)

# Start the monitor thread
monitor_thread = threading.Thread(target=connection_monitor, daemon=True)
monitor_thread.start()

# Load connections at startup
load_connections()

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/connections', methods=['GET'])
def get_connections():
    return jsonify(get_connections_list())

def get_connections_list():
    return [conn.to_dict() for conn in connections.values()]

@app.route('/api/connections', methods=['POST'])
def add_connection():
    data = request.json

    # Validate required fields
    required_fields = ['name', 'local_port', 'remote_port', 'vps_ip', 'key_path']
    for field in required_fields:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400

    # Check if name already exists
    if data['name'] in connections:
        return jsonify({'error': f'Connection with name "{data["name"]}" already exists'}), 400

    # Check if key file exists
    if not os.path.exists(data['key_path']):
        return jsonify({'error': f'SSH key file not found: {data["key_path"]}'}), 400

    # Create connection
    conn = SSHConnection(
        name=data['name'],
        local_port=int(data['local_port']),
        remote_port=int(data['remote_port']),
        vps_ip=data['vps_ip'],
        vps_user=data.get('vps_user', ''),
        key_path=data['key_path'],
        ssh_port=int(data.get('ssh_port', 22)),
        alive_interval=int(data.get('alive_interval', 60)),
        exit_on_failure=data.get('exit_on_failure', True)
    )

    connections[data['name']] = conn
    save_connections()

    return jsonify({'success': True, 'connection': conn.to_dict()})

@app.route('/api/connections/<name>', methods=['PUT'])
def update_connection(name):
    if name not in connections:
        return jsonify({'error': f'Connection "{name}" not found'}), 404

    data = request.json
    conn = connections[name]

    # Check if new name already exists
    if 'name' in data and data['name'] != name and data['name'] in connections:
        return jsonify({'error': f'Connection with name "{data["name"]}" already exists'}), 400

    # Check if key file exists
    if 'key_path' in data and not os.path.exists(data['key_path']):
        return jsonify({'error': f'SSH key file not found: {data["key_path"]}'}), 400

    # Stop connection if it's running
    was_active = conn.is_active()
    if was_active:
        conn.stop()

    # Update connection details
    if 'name' in data and data['name'] != name:
        del connections[name]
        conn.name = data['name']
        connections[data['name']] = conn

    if 'local_port' in data:
        conn.local_port = int(data['local_port'])
    if 'remote_port' in data:
        conn.remote_port = int(data['remote_port'])
    if 'vps_ip' in data:
        conn.vps_ip = data['vps_ip']
    if 'vps_user' in data:
        conn.vps_user = data['vps_user']
    if 'key_path' in data:
        conn.key_path = data['key_path']
    if 'ssh_port' in data:
        conn.ssh_port = int(data['ssh_port'])
    if 'alive_interval' in data:
        conn.alive_interval = int(data['alive_interval'])
    if 'exit_on_failure' in data:
        conn.exit_on_failure = data['exit_on_failure']

    # Restart if it was active
    if was_active:
        conn.start()

    save_connections()

    return jsonify({'success': True, 'connection': conn.to_dict()})

@app.route('/api/connections/<name>', methods=['DELETE'])
def delete_connection(name):
    if name not in connections:
        return jsonify({'error': f'Connection "{name}" not found'}), 404

    conn = connections[name]
    if conn.is_active():
        conn.stop()

    del connections[name]
    save_connections()

    return jsonify({'success': True})

@app.route('/api/connections/<name>/start', methods=['POST'])
def start_connection(name):
    if name not in connections:
        return jsonify({'error': f'Connection "{name}" not found'}), 404

    conn = connections[name]
    if conn.is_active():
        return jsonify({'error': f'Connection "{name}" is already running'}), 400

    if conn.start():
        return jsonify({'success': True, 'connection': conn.to_dict()})
    else:
        return jsonify({'error': f'Failed to start connection "{name}"'}), 500

@app.route('/api/connections/<name>/stop', methods=['POST'])
def stop_connection(name):
    if name not in connections:
        return jsonify({'error': f'Connection "{name}" not found'}), 404

    conn = connections[name]
    if not conn.is_active():
        return jsonify({'error': f'Connection "{name}" is not running'}), 400

    conn.stop()
    return jsonify({'success': True, 'connection': conn.to_dict()})

@app.route('/api/ssh-keys', methods=['GET'])
def list_ssh_keys():
    keys = []
    for filename in os.listdir(SSH_DIR):
        if not filename.endswith('.pub'):
            path = os.path.join(SSH_DIR, filename)
            if os.path.isfile(path):
                keys.append({
                    'name': filename,
                    'path': path,
                    'has_public_key': os.path.exists(f"{path}.pub")
                })
    return jsonify(keys)

@app.route('/api/ssh-keys', methods=['POST'])
def generate_ssh_key():
    data = request.json
    key_name = data.get('name', 'id_rsa')

    if not key_name:
        return jsonify({'error': 'Key name is required'}), 400

    key_path = os.path.join(SSH_DIR, key_name)

    if os.path.exists(key_path):
        return jsonify({'error': f'Key with name "{key_name}" already exists'}), 400

    try:
        # Generate key pair
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(key_path)

        # Try to set proper permissions
        try:
            os.chmod(key_path, 0o600)  # Set proper permissions for private key
        except Exception as e:
            print(f"Warning: Could not set permissions on key file: {e}")

        # Save public key
        with open(f"{key_path}.pub", "w") as f:
            f.write(f"ssh-rsa {key.get_base64()} ssh-tunnel-manager-key")

        return jsonify({
            'success': True, 
            'key': {
                'name': key_name,
                'path': key_path,
                'has_public_key': True
            }
        })
    except Exception as e:
        return jsonify({'error': f'Failed to generate SSH key: {str(e)}'}), 500

@app.route('/api/ssh-keys/<name>/public', methods=['GET'])
def get_public_key(name):
    key_path = os.path.join(SSH_DIR, name)
    public_key_path = f"{key_path}.pub"

    if not os.path.exists(key_path):
        return jsonify({'error': f'SSH key not found: {name}'}), 404

    if not os.path.exists(public_key_path):
        return jsonify({'error': f'Public key not found for: {name}'}), 404

    try:
        with open(public_key_path, "r") as f:
            public_key = f.read().strip()

        return jsonify({'success': True, 'public_key': public_key})
    except Exception as e:
        return jsonify({'error': f'Failed to read public key: {str(e)}'}), 500

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)

