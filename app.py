"""
Render Proxy - Forwards requests to Grid5000 via SSH tunnel.

Environment variables (set in Render dashboard):
    G5K_USER     - Your Grid5000 username
    G5K_SITE     - Grid5000 site (e.g., 'nancy', 'lyon', 'rennes')  
    G5K_SSH_KEY  - Base64-encoded SSH private key

Setup:
    1. Generate SSH key: ssh-keygen -t ed25519 -f g5k_render_key -N ""
    2. Add public key to G5K: https://api.grid5000.fr/ui/account
    3. Encode private key: cat g5k_render_key | base64 -w0
    4. Set G5K_SSH_KEY in Render to the base64 string
"""

import os
import sys
import base64
import tempfile
import subprocess
import threading
import time
import atexit

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

# Configuration from environment
G5K_USER = os.environ.get('G5K_USER')
G5K_SITE = os.environ.get('G5K_SITE', 'nancy')
G5K_SSH_KEY = os.environ.get('G5K_SSH_KEY')  # Base64-encoded private key
G5K_PORT = 5000  # Port where persistence_server.py runs on G5K
LOCAL_PORT = 15000  # Local port for SSH tunnel

# SSH tunnel state
tunnel_process = None
tunnel_lock = threading.Lock()
key_file_path = None


def setup_ssh_key():
    """Decode and save SSH key from environment."""
    global key_file_path
    
    if not G5K_SSH_KEY:
        print("ERROR: G5K_SSH_KEY not set")
        return None
    
    try:
        # Decode base64 key
        key_content = base64.b64decode(G5K_SSH_KEY).decode('utf-8')
        
        # Write to temp file
        fd, key_file_path = tempfile.mkstemp(prefix='g5k_key_', suffix='.pem')
        os.write(fd, key_content.encode())
        os.close(fd)
        os.chmod(key_file_path, 0o600)
        
        print(f"SSH key saved to {key_file_path}")
        return key_file_path
    except Exception as e:
        print(f"ERROR setting up SSH key: {e}")
        return None


def start_tunnel():
    """Start SSH tunnel to Grid5000."""
    global tunnel_process
    
    with tunnel_lock:
        # Check if already running
        if tunnel_process and tunnel_process.poll() is None:
            return True
        
        if not key_file_path:
            print("ERROR: SSH key not set up")
            return False
        
        if not G5K_USER:
            print("ERROR: G5K_USER not set")
            return False
        
        # Build SSH command
        # G5K frontend hostnames: fnancy, flyon, frennes, etc.
        g5k_frontend = f"f{G5K_SITE}"
        jump_host = "access.grid5000.fr"
        
        cmd = [
            'ssh',
            '-N',  # No command, just tunnel
            '-L', f'{LOCAL_PORT}:localhost:{G5K_PORT}',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=3',
            '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ConnectTimeout=30',
            '-i', key_file_path,
            '-J', f'{G5K_USER}@{jump_host}',
            f'{G5K_USER}@{g5k_frontend}'
        ]
        
        print(f"Starting SSH tunnel to {G5K_SITE}...")
        print(f"  Command: ssh -J {G5K_USER}@{jump_host} {G5K_USER}@{g5k_frontend}")
        print(f"  Local port: {LOCAL_PORT} -> remote port: {G5K_PORT}")
        
        try:
            tunnel_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            # Wait a bit for tunnel to establish
            time.sleep(3)
            
            if tunnel_process.poll() is not None:
                _, stderr = tunnel_process.communicate()
                print(f"SSH tunnel failed: {stderr.decode()}")
                return False
            
            print("SSH tunnel established!")
            return True
            
        except Exception as e:
            print(f"Failed to start SSH tunnel: {e}")
            return False


def ensure_tunnel():
    """Ensure SSH tunnel is running, restart if needed."""
    global tunnel_process
    
    with tunnel_lock:
        if tunnel_process and tunnel_process.poll() is None:
            return True
    
    return start_tunnel()


def cleanup():
    """Clean up tunnel and key file on exit."""
    global tunnel_process, key_file_path
    
    if tunnel_process:
        tunnel_process.terminate()
        tunnel_process.wait()
    
    if key_file_path and os.path.exists(key_file_path):
        os.remove(key_file_path)


atexit.register(cleanup)


def proxy_request(path, method='GET', json_data=None, timeout=300):
    """Forward request through tunnel to G5K server."""
    if not ensure_tunnel():
        return {'error': 'SSH tunnel not available. Check G5K_USER and G5K_SSH_KEY.'}, 503
    
    url = f'http://localhost:{LOCAL_PORT}{path}'
    
    try:
        if method == 'GET':
            resp = requests.get(url, timeout=timeout)
        elif method == 'POST':
            resp = requests.post(url, json=json_data, timeout=timeout)
        else:
            return {'error': f'Unsupported method: {method}'}, 400
        
        return resp.json(), resp.status_code
        
    except requests.exceptions.Timeout:
        return {'error': 'Request timed out'}, 504
    except requests.exceptions.ConnectionError as e:
        # Tunnel may have died, try to restart
        print(f"Connection error: {e}")
        start_tunnel()
        return {'error': 'Connection to G5K failed. Tunnel restarting.'}, 503
    except Exception as e:
        return {'error': str(e)}, 500


# ============== Routes ==============

@app.route('/', methods=['GET'])
def index():
    """Root endpoint with status info."""
    tunnel_ok = ensure_tunnel()
    return jsonify({
        'service': 'G5K Persistence Proxy',
        'g5k_site': G5K_SITE,
        'g5k_user': G5K_USER or 'NOT SET',
        'tunnel_status': 'connected' if tunnel_ok else 'disconnected',
        'endpoints': ['/health', '/persistence', '/vineyard']
    })


@app.route('/health', methods=['GET'])
def health():
    """Health check - proxies to G5K server."""
    result, status = proxy_request('/health', 'GET')
    
    # Add proxy info
    if isinstance(result, dict):
        result['proxy'] = 'render'
        result['g5k_site'] = G5K_SITE
    
    return jsonify(result), status


@app.route('/persistence', methods=['POST'])
def persistence():
    """Proxy persistence computation to G5K."""
    data = request.get_json()
    result, status = proxy_request('/persistence', 'POST', data)
    return jsonify(result), status


@app.route('/vineyard', methods=['POST'])
def vineyard():
    """Proxy vineyard computation to G5K."""
    data = request.get_json()
    result, status = proxy_request('/vineyard', 'POST', data, timeout=600)
    return jsonify(result), status


# ============== Startup ==============

def startup():
    """Run on startup."""
    print("=" * 50)
    print("Grid5000 Persistence Proxy")
    print("=" * 50)
    print(f"G5K_USER: {G5K_USER or 'NOT SET'}")
    print(f"G5K_SITE: {G5K_SITE}")
    print(f"G5K_SSH_KEY: {'SET' if G5K_SSH_KEY else 'NOT SET'}")
    print()
    
    if G5K_USER and G5K_SSH_KEY:
        setup_ssh_key()
        start_tunnel()
    else:
        print("WARNING: Missing G5K credentials. Set G5K_USER and G5K_SSH_KEY.")


# Run startup
startup()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)