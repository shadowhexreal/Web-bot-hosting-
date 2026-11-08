from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
import json
import os
import subprocess
import random
import string
import uuid
from datetime import datetime, timedelta
import sys
import shutil
import threading
import time
import zipfile
import psutil
import hmac
import re
import secrets
from werkzeug.security import generate_password_hash, check_password_hash
import platform
import tarfile
import tempfile
import glob
import hashlib
import io
import atexit
import functools
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or 'jubayer-super-secret-key-2026'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('COOKIE_SECURE') == '1'

USERS_FILE = 'users.json'
BOTS_DIR = 'bots'
CPU_HISTORY = {}
CRASH_COUNT = {}
NET_STATS = {}

os.makedirs(BOTS_DIR, exist_ok=True)

# ============================================
# রেট লিমিট
# ============================================

class RateLimiter:
    def check_rate(self, server_id, limit_percent):
        if server_id not in CPU_HISTORY:
            CPU_HISTORY[server_id] = []
        users = load_users()
        server = None
        for uname, data in users.items():
            if uname == 'admin': continue
            servers = data.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    server = s
                    break
        if not server or server.get('status') != 'running':
            return False, 0
        pid = server.get('pid')
        if not pid: return False, 0
        try:
            proc = psutil.Process(pid)
            cpu = proc.cpu_percent(interval=1)
            now = time.time()
            CPU_HISTORY[server_id].append({'time': now, 'cpu': cpu})
            CPU_HISTORY[server_id] = [h for h in CPU_HISTORY[server_id] if now - h['time'] < 30]
            recent = [h['cpu'] for h in CPU_HISTORY[server_id] if now - h['time'] < 10]
            if recent:
                avg_cpu = sum(recent) / len(recent)
                if avg_cpu > limit_percent:
                    return True, avg_cpu
        except: pass
        return False, 0

rate_limiter = RateLimiter()

# ============================================
# অটো-রিস্টার্ট
# ============================================

def should_auto_restart(server_id):
    if server_id not in CRASH_COUNT:
        CRASH_COUNT[server_id] = {'count': 0, 'last_crash': time.time()}
    crash_info = CRASH_COUNT[server_id]
    if time.time() - crash_info['last_crash'] < 60:
        if crash_info['count'] >= 3:
            return False
    else:
        crash_info['count'] = 0
    crash_info['count'] += 1
    crash_info['last_crash'] = time.time()
    return True

# ============================================
# হেল্পার
# ============================================

def generate_random_password(length=10):
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=length))

def load_users():
    if not os.path.exists(USERS_FILE):
        default = {"admin": {"password": "admin123", "role": "admin"}}
        save_users(default)
        return default
    with open(USERS_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if 'admin' not in data:
        data['admin'] = {"password": "admin123", "role": "admin"}
        save_users(data)
    return data

def save_users(data):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def get_server_dir(server_id):
    server_dir = os.path.join(BOTS_DIR, server_id)
    os.makedirs(server_dir, exist_ok=True)
    return server_dir

def check_server_valid(server_id):
    users = load_users()
    for uname, data in users.items():
        if uname == 'admin': continue
        servers = data.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                if s.get('suspended') or data.get('banned'):
                    return False, "suspended"
                expiry = s.get('expiry', '')
                if expiry:
                    try:
                        exp_date = datetime.strptime(expiry, '%Y-%m-%d %H:%M:%S.%f')
                        if datetime.now() > exp_date:
                            return False, "expired"
                    except: pass
                return True, s
    return False, "deleted"

def get_server_by_id(server_id):
    users = load_users()
    for uname, data in users.items():
        if uname == 'admin': continue
        servers = data.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                return s, uname
    return None, None

def create_default_files(server_dir):
    main_py = os.path.join(server_dir, 'main.py')
    if not os.path.exists(main_py):
        with open(main_py, 'w', encoding='utf-8') as f:
            f.write('''# JUBAYER HOSTING - Default Bot
import time

print("=" * 40)
print("Bot is running on JUBAYER HOSTING")
print("Server is ready!")
print("=" * 40)

counter = 0
while True:
    counter += 1
    print(f"[{time.strftime('%H:%M:%S')}] Heartbeat #{counter} | Server active")
    time.sleep(10)
''')
    
    req_file = os.path.join(server_dir, 'requirements.txt')
    if not os.path.exists(req_file):
        with open(req_file, 'w', encoding='utf-8') as f:
            f.write('# Add your pip packages here\n')

# ============================================
# বট রান
# ============================================

def run_bot(server_id, main_file='main.py', requirements_file='requirements.txt'):
    server_dir = get_server_dir(server_id)
    main_path = os.path.join(server_dir, main_file)
    log_file = os.path.join(server_dir, 'output.log')
    python_exe = sys.executable
    if main_file.lower().endswith(('.html', '.htm')):
        return None, "Static websites do not run as a process. Use the Website tab to publish your site."
    is_node = main_file.lower().endswith(('.js', '.mjs', '.cjs'))
    node_exe = find_node() if is_node else None
    
    def log(msg):
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"{msg}\n")
                f.flush()
        except: pass
    
    if not os.path.exists(main_path):
        return None, f"ERROR: {main_file} not found!"
    
    if os.path.exists(log_file):
        try: os.remove(log_file)
        except: open(log_file, 'w').close()
    
    ts = lambda: datetime.now().strftime('%I:%M:%S %p')
    
    server, _ = get_server_by_id(server_id)
    cpu_limit = server.get('cpu_limit', 80) if server else 80
    log(f"[{ts()}] Checking rate limit...")
    log(f"[{ts()}] Rate limit: {cpu_limit}%")
    log("")
    
    if is_node:
        if not node_exe:
            log(f"[{ts()}] Node.js was not found on this host. Installing a private copy (one-time)...")
            node_exe, install_err = install_node(lambda m: log(f"[{ts()}] {m}"))
            if not node_exe:
                log(f"[{ts()}] ERROR: {install_err}")
                return None, install_err
            log(f"[{ts()}] Node.js ready: {node_exe}")
        npm_exe = find_npm(node_exe)
        if npm_exe and os.path.exists(os.path.join(server_dir, 'package.json')):
            log(f"[{ts()}] Run: npm install")
            log("")
            try:
                proc = subprocess.Popen(
                    [npm_exe, 'install', '--no-audit', '--no-fund'],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, universal_newlines=True, cwd=server_dir, env=node_env(node_exe),
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                )
                for line in iter(proc.stdout.readline, ''):
                    if line.strip():
                        log(f"[{ts()}] {line.rstrip()}")
                proc.wait()
                log("")
                if proc.returncode != 0:
                    log(f"[{ts()}] npm install finished with errors")
                else:
                    log(f"[{ts()}] npm install complete!")
            except Exception as e:
                log(f"[{ts()}] npm error: {str(e)}")
        else:
            log(f"[{ts()}] No package.json found, skipping npm install...")
    elif requirements_file and requirements_file.strip():
        req_path = os.path.join(server_dir, requirements_file.strip())
        log(f"[{ts()}] Run: pip install -r {requirements_file}")
        log("")
        
        if os.path.exists(req_path):
            with open(req_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            
            lines = [l.strip() for l in content.split('\n') if l.strip() and not l.strip().startswith('#')]
            
            if lines:
                try:
                    proc = subprocess.Popen(
                        [python_exe, '-m', 'pip', 'install', '-r', os.path.abspath(req_path), '--disable-pip-version-check'],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1, universal_newlines=True,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
                    )
                    
                    for line in iter(proc.stdout.readline, ''):
                        if line.strip():
                            log(f"[{ts()}] {line.rstrip()}")
                    
                    proc.wait()
                    log("")
                    
                    if proc.returncode != 0:
                        log(f"[{ts()}] Some packages failed to install")
                    else:
                        log(f"[{ts()}] Requirements installation complete!")
                except Exception as e:
                    log(f"[{ts()}] pip error: {str(e)}")
            else:
                log(f"[{ts()}] {requirements_file} is empty, skipping...")
        else:
            log(f"[{ts()}] {requirements_file} not found, skipping...")
    else:
        log(f"[{ts()}] No requirements file set, skipping...")
    
    log("")
    if is_node:
        log(f"[{ts()}] Run: node {main_file}")
    else:
        log(f"[{ts()}] Run: python {main_file}")
        log(f"[{ts()}] Python {sys.version.split()[0]}")
    log("")
    
    try:
        main_path_abs = os.path.abspath(main_path)
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        if is_node:
            env['PATH'] = os.path.dirname(node_exe) + os.pathsep + env.get('PATH', '')
        for env_key, env_val in ((server or {}).get('env_vars') or {}).items():
            env[str(env_key)] = str(env_val)
        
        proc = subprocess.Popen(
            [node_exe if is_node else python_exe, main_path_abs],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=server_dir,
            text=True, encoding='utf-8', errors='replace',
            bufsize=1, env=env, universal_newlines=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        )
        
        log(f"[{ts()}] Server marked as running")
        log(f"[{ts()}] PID: {proc.pid}")
        log("")
        
        def rate_monitor():
            while proc.poll() is None:
                time.sleep(5)
                exceeded, avg_cpu = rate_limiter.check_rate(server_id, cpu_limit)
                if exceeded:
                    log(f"[{datetime.now().strftime('%I:%M:%S %p')}] CPU Limit! {avg_cpu:.1f}% > {cpu_limit}%")
                    proc.terminate()
                    time.sleep(2)
                    if proc.poll() is None: proc.kill()
                    
                    users = load_users()
                    for uname, data in users.items():
                        if uname == 'admin': continue
                        servers = data.get('servers', [])
                        if not isinstance(servers, list): continue
                        for s in servers:
                            if isinstance(s, dict) and s.get('server_id') == server_id:
                                s['status'] = 'stopped'
                                s['pid'] = None
                                s['rate_limit_exceeded'] = True
                                s['stopped_by_user'] = False
                                save_users(users)
                                break
                    break
        
        threading.Thread(target=rate_monitor, daemon=True).start()
        
        def stream_output():
            try:
                with open(log_file, 'a', encoding='utf-8') as f:
                    for line in iter(proc.stdout.readline, ''):
                        if line:
                            line = line.rstrip('\n\r')
                            if line:
                                f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] {line}\n")
                                f.flush()
            except: pass
        
        threading.Thread(target=stream_output, daemon=True).start()
        
        return proc.pid, None
        
    except Exception as e:
        log(f"[{ts()}] Error: {str(e)}")
        return None, str(e)

def stop_bot_process(pid):
    try:
        if sys.platform == 'win32':
            subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
        else:
            os.kill(pid, 15)
        return True
    except: return False

def monitor_bot(server_id, pid):
    while True:
        try:
            if sys.platform == 'win32':
                result = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'], capture_output=True, text=True)
                if str(pid) not in result.stdout:
                    break
            else:
                try: os.kill(pid, 0)
                except: break
        except: break
        time.sleep(5)
    
    server, _ = get_server_by_id(server_id)
    if not server: return
    if server.get('stopped_by_user'): return
    if server.get('rate_limit_exceeded'): return
    
    if should_auto_restart(server_id):
        time.sleep(3)
        new_pid, error = run_bot(server_id, server.get('main_file', 'main.py'), 
                                 server.get('requirements_file', 'requirements.txt'))
        if new_pid:
            users = load_users()
            for uname, data in users.items():
                if uname == 'admin': continue
                servers = data.get('servers', [])
                if not isinstance(servers, list): continue
                for s in servers:
                    if isinstance(s, dict) and s.get('server_id') == server_id:
                        s['status'] = 'running'
                        s['pid'] = new_pid
                        s['started_at'] = str(datetime.now())
                        s['rate_limit_exceeded'] = False
                        s['stopped_by_user'] = False
                        save_users(users)
                        break
            threading.Thread(target=monitor_bot, args=(server_id, new_pid), daemon=True).start()
    else:
        users = load_users()
        for uname, data in users.items():
            if uname == 'admin': continue
            servers = data.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    s['status'] = 'stopped'
                    s['pid'] = None
                    save_users(users)
                    return

def get_process_stats(pid):
    try:
        proc = psutil.Process(pid)
        cpu = proc.cpu_percent(interval=0.5)
        mem = proc.memory_info()
        ram = mem.rss / (1024 * 1024)
        return {
            'cpu_percent': round(cpu, 1),
            'ram_mb': round(ram, 1),
            'ram_display': f"{ram:.1f} MB" if ram < 1024 else f"{ram/1024:.1f} GB",
        }
    except:
        return {'cpu_percent': 0, 'ram_mb': 0, 'ram_display': '0 MB'}

def get_network_stats(psutil_pid):
    try:
        proc = psutil.Process(psutil_pid)
        io = proc.io_counters()
        if io:
            read_kb = io.read_bytes / 1024
            write_kb = io.write_bytes / 1024
            return format_bytes(read_kb), format_bytes(write_kb)
    except: pass
    return "0 KB", "0 KB"

def format_bytes(kb):
    if kb < 1024: return f"{kb:.1f} KB"
    mb = kb / 1024
    if mb < 1024: return f"{mb:.1f} MB"
    gb = mb / 1024
    return f"{gb:.2f} GB"

# ============================================
# 🔥 পাবলিক API - সার্ভার তৈরি
# ============================================

@app.route('/api/create', methods=['GET'])
def api_create_server():
    username = request.args.get('username', '').strip()
    password = request.args.get('password', '').strip()
    server_type = request.args.get('type', 'python').strip()
    ram = request.args.get('ram', '1GB').strip()
    disk = request.args.get('disk', '1GB').strip()
    cpu_limit = int(request.args.get('cpu', '30'))
    days = int(request.args.get('days', '3'))
    
    if not password:
        password = generate_random_password(10)
    
    if not username:
        username = f"JUBAYER_CODEX{random.randint(10000, 99999)}"
    
    if len(username) < 3:
        return jsonify({'status': 'error', 'message': 'Username must be at least 3 characters!'}), 400
    
    if len(password) < 4:
        return jsonify({'status': 'error', 'message': 'Password must be at least 4 characters!'}), 400
    
    if cpu_limit < 10 or cpu_limit > 100:
        return jsonify({'status': 'error', 'message': 'CPU limit must be between 10 and 100!'}), 400
    
    if days < 1 or days > 365:
        return jsonify({'status': 'error', 'message': 'Days must be between 1 and 365!'}), 400
    
    users = load_users()
    
    if username in users:
        return jsonify({'status': 'error', 'message': f"Username '{username}' already exists!"}), 400
    
    server_id = str(uuid.uuid4())[:8]
    expiry_date = datetime.now() + timedelta(days=days)
    
    create_default_files(get_server_dir(server_id))
    
    host = request.host
    is_local = host.startswith('localhost') or host.startswith('127.0.0.1') or host.startswith('192.168')
    scheme = 'http' if is_local else 'https'
    full_url = f"{scheme}://{host}/{server_id}/login"
    
    new_server = {
        'server_id': server_id,
        'login_url': f"/{server_id}/login",
        'dashboard_url': f"/{server_id}/home",
        'full_link': full_url,
        'type': server_type,
        'ram': ram, 'disk': disk,
        'status': 'stopped', 'pid': None,
        'created': str(datetime.now()),
        'expiry': str(expiry_date),
        'main_file': 'main.py',
        'requirements_file': 'requirements.txt',
        'cpu_limit': cpu_limit,
        'rate_limit_exceeded': False,
        'stopped_by_user': False
    }
    
    users[username] = {'password': password, 'role': 'user', 'servers': [new_server]}
    save_users(users)
    
    return jsonify({
        'status': 'success',
        'message': 'Panel created successfully!',
        'username': username,
        'password': password,
        'server_type': server_type,
        'ram': ram,
        'disk': disk,
        'cpu_limit': cpu_limit,
        'validity': f'{days} days',
        'expiry_date': expiry_date.strftime('%Y-%m-%d'),
        'full_url': full_url,
        'server_id': server_id
    }), 200

# ============================================
# রাউটস
# ============================================

@app.route('/')
def index():
    return render_template('landing.html')

@app.route('/landing')
def landing():
    return render_template('landing.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        users = load_users()
        if username == 'admin' and password == users.get('admin', {}).get('password'):
            session['user'] = 'admin'
            session['role'] = 'admin'
            return redirect(url_for('admin_dashboard'))
        return render_template('login.html', error="Invalid credentials!")
    return render_template('login.html', error=None)

@app.route('/<server_id>/login', methods=['GET', 'POST'])
def server_login(server_id):
    valid, result = check_server_valid(server_id)
    if not valid:
        return render_template('error.html', error_type=result if result else "deleted", server_link=server_id)
    
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        
        # Per-server credentials (auto generated when the server was created)
        srv_user = result.get('srv_user')
        if srv_user and safe_eq(srv_user, username) and safe_eq(result.get('srv_pass', ''), password):
            owner = get_server_by_id(server_id)[1]
            return start_server_session(owner, result)
        
        users = load_users()
        for uname, data in users.items():
            if uname == 'admin': continue
            servers = data.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    if username == uname and password == data.get('password'):
                        session['user'] = uname
                        session['role'] = 'user'
                        session['current_server_id'] = server_id
                        return redirect(url_for('server_home', server_id=server_id))
                    else:
                        return render_template('auth.html', mode='server', server_id=server_id, error="Invalid credentials!")
        return render_template('auth.html', mode='server', server_id=server_id, error="Invalid login!")
    return render_template('auth.html', mode='server', server_id=server_id, error=None)

@app.route('/<server_id>/home')
def server_home(server_id):
    if 'user' not in session or session.get('role') != 'user':
        return redirect(url_for('server_login', server_id=server_id))
    if session.get('current_server_id') != server_id:
        session.clear()
        return redirect(url_for('server_login', server_id=server_id))
    
    valid, result = check_server_valid(server_id)
    if not valid:
        session.clear()
        return render_template('error.html', error_type=result if result else "deleted", server_link=server_id)
    
    return render_template('home.html', username=session['user'], current_server=result, acct=session.get('acct'))

@app.route('/logout')
def logout():
    server_id = session.get('current_server_id')
    was_account = session.get('acct')
    session.clear()
    if was_account:
        return redirect(url_for('account_login'))
    if server_id:
        return redirect(url_for('server_login', server_id=server_id))
    return redirect(url_for('login'))

# ============================================
# অ্যাডমিন
# ============================================

@app.route('/admin')
def admin_dashboard():
    if 'user' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))
    users = load_users()
    user_list = []
    total_servers = 0
    total_running = 0
    for uname, data in users.items():
        if uname == 'admin': continue
        servers = data.get('servers', [])
        if not isinstance(servers, list): servers = []
        running = sum(1 for s in servers if isinstance(s, dict) and s.get('status') == 'running')
        total_servers += len(servers)
        total_running += running
        user_list.append({
            'username': uname, 'password': data.get('password', ''),
            'servers': servers, 'server_count': len(servers), 'running_count': running
        })
    return render_template('admin.html', users=user_list, total_servers=total_servers, total_running=total_running)

@app.route('/admin/create_server', methods=['POST'])
def create_server():
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    
    data = request.get_json()
    username = data.get('username', '')
    password = data.get('password', '')
    server_type = data.get('server_type', 'python')
    ram = data.get('ram', '512MB')
    disk = data.get('disk', '1GB')
    expiry_days = int(data.get('expiry_days', 30))
    cpu_limit = int(data.get('cpu_limit', 80))
    
    if not username or not password:
        return jsonify({'error': 'Required!'}), 400
    
    users = load_users()
    server_id = str(uuid.uuid4())[:8]
    expiry_date = datetime.now() + timedelta(days=expiry_days)
    
    new_dir = get_server_dir(server_id)
    new_main, new_req = 'main.py', 'requirements.txt'
    if server_type == 'node':
        create_node_files(new_dir)
        new_main, new_req = 'main.js', ''
    elif server_type == 'static':
        create_static_files(new_dir)
        new_main, new_req = 'index.html', ''
    else:
        create_default_files(new_dir)
    
    new_server = {
        'server_id': server_id, 'link': server_id,
        'login_url': f"/{server_id}/login",
        'dashboard_url': f"/{server_id}/home",
        'full_link': request.host_url.rstrip('/') + f"/{server_id}/home",
        'type': server_type, 'ram': ram, 'disk': disk,
        'status': 'stopped', 'pid': None,
        'created': str(datetime.now()), 'expiry': str(expiry_date),
        'main_file': new_main, 'requirements_file': new_req,
        'cpu_limit': cpu_limit, 'rate_limit_exceeded': False, 'stopped_by_user': False
    }
    
    if username not in users:
        users[username] = {'password': password, 'role': 'user', 'servers': []}
    
    users[username]['servers'].append(new_server)
    save_users(users)
    if server_type == 'node': ensure_runtime_async('node')
    if server_type == 'static': ensure_runtime_async('cloudflared')
    log_activity('admin', 'admin', f"Created {server_type} server {server_id} for {username}")
    
    return jsonify({
        'success': True, 'username': username, 'password': password,
        'login_url': new_server['login_url'],
        'hostname': new_server['full_link'],
        'server_id': server_id
    })

@app.route('/admin/set_rate_limit/<server_id>', methods=['POST'])
def set_rate_limit(server_id):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    cpu_limit = int(request.get_json().get('cpu_limit', 80))
    users = load_users()
    for uname, udata in users.items():
        if uname == 'admin': continue
        servers = udata.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                s['cpu_limit'] = cpu_limit
                save_users(users)
                return jsonify({'success': True, 'cpu_limit': cpu_limit})
    return jsonify({'error': 'Not found'}), 404

@app.route('/admin/delete_server/<username>/<server_id>', methods=['POST'])
def delete_server(username, server_id):
    if 'user' not in session or session.get('role') != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    users = load_users()
    if username in users:
        servers = users[username].get('servers', [])
        if not isinstance(servers, list): servers = []
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                if s.get('pid'): stop_bot_process(s['pid'])
                stop_site(server_id)
                log_activity('admin', 'admin', f"Deleted server {server_id} of {username}")
                try: shutil.rmtree(get_server_dir(server_id))
                except: pass
                break
        users[username]['servers'] = [s for s in servers if isinstance(s, dict) and s.get('server_id') != server_id]
        if len(users[username]['servers']) == 0 and not users[username].get('account'):
            del users[username]
        save_users(users)
    return jsonify({'success': True})

# ============================================
# বট API
# ============================================

@app.route('/api/run/<server_id>', methods=['POST'])
def api_run(server_id):
    server, _ = get_server_by_id(server_id)
    if not server: return jsonify({'status': 'error', 'msg': 'Not found'})
    if server.get('suspended'): return jsonify({'status': 'error', 'msg': 'This server is suspended.'})
    if server.get('status') == 'running': return jsonify({'status': 'error', 'msg': 'Already running!'})
    
    server['rate_limit_exceeded'] = False
    server['stopped_by_user'] = False
    
    pid, error = run_bot(server_id, server.get('main_file', 'main.py'), server.get('requirements_file', 'requirements.txt'))
    
    if pid:
        users = load_users()
        for uname, data in users.items():
            if uname == 'admin': continue
            servers = data.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    s['status'] = 'running'
                    s['pid'] = pid
                    s['started_at'] = str(datetime.now())
                    save_users(users)
                    break
        threading.Thread(target=monitor_bot, args=(server_id, pid), daemon=True).start()
        return jsonify({'status': 'success', 'msg': 'Started!'})
    return jsonify({'status': 'error', 'msg': error or 'Failed'})

@app.route('/api/stop/<server_id>', methods=['POST'])
def api_stop(server_id):
    server, _ = get_server_by_id(server_id)
    if not server: return jsonify({'status': 'error', 'msg': 'Not found'})
    
    if server.get('pid'): stop_bot_process(server['pid'])
    
    users = load_users()
    for uname, data in users.items():
        if uname == 'admin': continue
        servers = data.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                s['status'] = 'stopped'
                s['pid'] = None
                s['stopped_by_user'] = True
                save_users(users)
                break
    
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"\n[{datetime.now().strftime('%I:%M:%S %p')}] Server stopped by user\n")
    except: pass
    
    return jsonify({'status': 'success', 'msg': 'Stopped'})

@app.route('/api/logs/<server_id>')
def api_logs(server_id):
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    if os.path.exists(log_file):
        with open(log_file, 'r', encoding='utf-8') as f: logs = f.read()
    else: logs = ""
    return jsonify({'logs': logs})

@app.route('/api/clear_logs/<server_id>', methods=['POST'])
def api_clear_logs(server_id):
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        if os.path.exists(log_file):
            try: os.remove(log_file)
            except: open(log_file, 'w').close()
        return jsonify({'status': 'success', 'msg': 'Cleared'})
    except: return jsonify({'status': 'error'}), 500

@app.route('/api/command', methods=['POST'])
def api_command():
    data = request.get_json()
    cmd = data.get('cmd', '')
    server_id = data.get('server_id', '')
    log_file = os.path.join(get_server_dir(server_id), 'output.log')
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=get_server_dir(server_id), timeout=30,
                              creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        output = (result.stdout + result.stderr)[:2000]
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] $ {cmd}\n{output}\n")
        return jsonify({'status': 'success', 'output': output})
    except: return jsonify({'status': 'error', 'msg': 'Timeout'})

@app.route('/api/stats/<server_id>')
def api_stats(server_id):
    server, _ = get_server_by_id(server_id)
    if not server:
        return jsonify({'cpu': '0%', 'ram': '0 MB', 'uptime': '0h', 'status': 'unknown', 'cpu_limit': 80, 'net_in': '0 KB', 'net_out': '0 KB'})
    
    uptime, cpu, ram, net_in, net_out = "0h 0m", "0%", "0 MB", "0 KB", "0 KB"
    
    if server.get('status') == 'running' and server.get('pid'):
        stats = get_process_stats(server['pid'])
        cpu = f"{stats['cpu_percent']}%"
        ram = stats['ram_display']
        net_in, net_out = get_network_stats(server['pid'])
    
    if server.get('status') == 'running' and server.get('started_at'):
        try:
            start = datetime.strptime(server['started_at'], '%Y-%m-%d %H:%M:%S.%f')
            diff = datetime.now() - start
            if diff.days > 0: uptime = f"{diff.days}d {diff.seconds//3600}h"
            else:
                h, m, s = diff.seconds // 3600, (diff.seconds % 3600) // 60, diff.seconds % 60
                uptime = f"{h}h {m}m {s}s"
        except: pass
    
    return jsonify({'cpu': cpu, 'ram': ram, 'uptime': uptime, 'net_in': net_in, 'net_out': net_out, 'cpu_limit': server.get('cpu_limit', 80), 'status': server.get('status', 'stopped')})

# ============================================
# পাসওয়ার্ড চেঞ্জ
# ============================================

@app.route('/api/change_password/<server_id>', methods=['POST'])
def api_change_password(server_id):
    if 'user' not in session: return jsonify({'error': 'Please login first!'}), 403
    data = request.get_json()
    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')
    
    if not current_password or not new_password: return jsonify({'error': 'All fields are required!'})
    if len(new_password) < 4: return jsonify({'error': 'Password must be at least 4 characters!'})
    
    users = load_users()
    username = session.get('user')
    
    if username in users:
        if users[username].get('password') == current_password:
            users[username]['password'] = new_password
            save_users(users)
            return jsonify({'success': True, 'msg': 'Password changed!'})
        return jsonify({'error': 'Current password is incorrect!'})
    return jsonify({'error': 'User not found!'}), 404

# ============================================
# 🔥 GITHUB API (Git ছাড়া Python Download)
# ============================================

@app.route('/api/github/deploy/<server_id>', methods=['POST'])
def api_github_deploy(server_id):
    """Download GitHub repo without git - using Python requests"""
    data = request.get_json()
    repo_url = data.get('repo_url', '').strip()
    access_token = data.get('access_token', '').strip()
    is_private = data.get('is_private', False)
    
    if not repo_url:
        return jsonify({'status': 'error', 'msg': 'Repository URL is required!'}), 400
    
    server_dir = get_server_dir(server_id)
    log_file = os.path.join(server_dir, 'github_deploy.log')
    
    # Clear old deploy log
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] Starting GitHub deployment...\n")
            f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] Repository: {repo_url}\n")
            f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] Type: {'Private' if is_private else 'Public'}\n")
            f.write("─" * 40 + "\n")
    except:
        pass
    
    def deploy_thread():
        try:
            import requests
            import shutil
            
            def deploy_log(msg):
                try:
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] {msg}\n")
                        f.flush()
                except:
                    pass
            
            deploy_log("Preparing deployment...")
            
            # Parse GitHub URL → owner/repo/branch
            # Support: https://github.com/user/repo or https://github.com/user/repo.git
            # Also: https://github.com/user/repo/tree/branch
            
            clean_url = repo_url.replace('.git', '').rstrip('/')
            
            # Extract owner and repo
            if 'github.com' not in clean_url:
                deploy_log("❌ Error: Only GitHub URLs are supported!")
                return
            
            parts = clean_url.split('github.com/')[-1].split('/')
            
            if len(parts) < 2:
                deploy_log("❌ Error: Invalid GitHub URL format!")
                return
            
            owner = parts[0]
            repo = parts[1]
            branch = 'main'  # default branch
            
            # Check if branch is specified (tree/branch_name)
            if len(parts) > 3 and parts[2] == 'tree':
                branch = parts[3]
            
            deploy_log(f"Owner: {owner}")
            deploy_log(f"Repository: {repo}")
            deploy_log(f"Branch: {branch}")
            deploy_log(f"Downloading ZIP archive...")
            
            # Build API URL
            api_url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{branch}"
            
            headers = {'Accept': 'application/vnd.github.v3+json'}
            if is_private and access_token:
                headers['Authorization'] = f'token {access_token}'
                deploy_log("Using access token for authentication")
            
            # Download ZIP
            response = requests.get(api_url, headers=headers, stream=True, timeout=60)
            
            if response.status_code == 200:
                deploy_log("✓ Repository downloaded successfully!")
                deploy_log("Extracting files...")
                
                # Save to temp zip
                temp_zip = os.path.join(server_dir, '_github_temp.zip')
                with open(temp_zip, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                # Extract
                try:
                    with zipfile.ZipFile(temp_zip, 'r') as zf:
                        # GitHub zipball has a root folder like: owner-repo-commit_hash
                        # Extract everything inside that folder
                        root_folder = zf.namelist()[0].split('/')[0]
                        
                        for member in zf.namelist():
                            # Skip the root folder
                            relative_path = '/'.join(member.split('/')[1:])
                            if not relative_path:
                                continue
                            
                            target_path = os.path.join(server_dir, relative_path)
                            
                            if member.endswith('/'):
                                # Directory
                                os.makedirs(target_path, exist_ok=True)
                            else:
                                # File
                                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                                with zf.open(member) as source, open(target_path, 'wb') as target:
                                    shutil.copyfileobj(source, target)
                                deploy_log(f"  ✓ {relative_path}")
                    
                    deploy_log("")
                    deploy_log("✅ Deployment completed successfully!")
                    deploy_log(f"Files extracted to: {server_dir}")
                    
                except Exception as e:
                    deploy_log(f"❌ Extraction error: {str(e)}")
                finally:
                    # Clean up temp zip
                    try:
                        os.remove(temp_zip)
                    except:
                        pass
                    
            elif response.status_code == 404:
                deploy_log("❌ Error: Repository not found!")
                deploy_log("Check if the URL is correct and the repo is accessible")
            elif response.status_code == 401:
                deploy_log("❌ Error: Authentication failed!")
                deploy_log("Check your access token (for private repos)")
            elif response.status_code == 403:
                deploy_log("❌ Error: Rate limit exceeded or access denied!")
                deploy_log("Try again later or use an access token")
            else:
                deploy_log(f"❌ Error: HTTP {response.status_code}")
                deploy_log(f"Response: {response.text[:200]}")
            
        except requests.exceptions.Timeout:
            try:
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] ❌ Error: Connection timeout! Check your internet.\n")
            except:
                pass
        except Exception as e:
            try:
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"[{datetime.now().strftime('%I:%M:%S %p')}] ❌ Error: {str(e)}\n")
            except:
                pass
    
    threading.Thread(target=deploy_thread, daemon=True).start()
    return jsonify({'status': 'success', 'msg': 'Deployment started! Check terminal for progress.'})

@app.route('/api/github/logs/<server_id>')
def api_github_logs(server_id):
    """Get GitHub deployment logs"""
    log_file = os.path.join(get_server_dir(server_id), 'github_deploy.log')
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                logs = f.read()
        except:
            logs = "> Ready for deployment..."
    else:
        logs = "> Ready for deployment..."
    return jsonify({'logs': logs})

@app.route('/api/github/clear_logs/<server_id>', methods=['POST'])
def api_github_clear_logs(server_id):
    """Clear GitHub deployment logs"""
    log_file = os.path.join(get_server_dir(server_id), 'github_deploy.log')
    try:
        if os.path.exists(log_file):
            os.remove(log_file)
        return jsonify({'status': 'success'})
    except:
        return jsonify({'status': 'error'}), 500

# ============================================
# ফাইল API
# ============================================

@app.route('/api/files/<server_id>')
def api_files(server_id):
    folder = request.args.get('folder', '')
    server_dir = get_server_dir(server_id)
    if folder:
        server_dir = os.path.join(server_dir, folder)
        if not os.path.abspath(server_dir).startswith(os.path.abspath(get_server_dir(server_id))):
            return jsonify({'files': []})
    if not os.path.exists(server_dir): return jsonify({'files': []})
    
    files = []
    try:
        for item in os.listdir(server_dir):
            item_path = os.path.join(server_dir, item)
            files.append({'name': item, 'is_dir': os.path.isdir(item_path), 'size': os.path.getsize(item_path) if os.path.isfile(item_path) else 0, 'modified': datetime.fromtimestamp(os.path.getmtime(item_path)).strftime('%Y-%m-%d %H:%M')})
    except: pass
    return jsonify({'files': files})

@app.route('/api/file/<server_id>', methods=['GET'])
def api_get_file(server_id):
    filename = request.args.get('filename', '')
    filepath = os.path.join(get_server_dir(server_id), filename)
    if os.path.exists(filepath) and os.path.isfile(filepath):
        with open(filepath, 'r', encoding='utf-8') as f: return jsonify({'content': f.read()})
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/file/<server_id>', methods=['POST'])
def api_save_file(server_id):
    data = request.get_json()
    filepath = os.path.join(get_server_dir(server_id), data.get('filename', ''))
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f: f.write(data.get('content', ''))
    return jsonify({'success': True})

@app.route('/api/file/<server_id>', methods=['DELETE'])
def api_delete_file(server_id):
    data = request.get_json()
    filepath = os.path.join(get_server_dir(server_id), data.get('filename', ''))
    if os.path.exists(filepath):
        if os.path.isdir(filepath): shutil.rmtree(filepath)
        else: os.remove(filepath)
    return jsonify({'success': True})

@app.route('/api/upload/<server_id>', methods=['POST'])
def api_upload(server_id):
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    folder = request.form.get('folder', '')
    server_dir = get_server_dir(server_id)
    if folder:
        server_dir = os.path.join(server_dir, folder)
        os.makedirs(server_dir, exist_ok=True)
    file = request.files['file']
    file.save(os.path.join(server_dir, file.filename))
    return jsonify({'success': True})

@app.route('/api/create_folder/<server_id>', methods=['POST'])
def api_create_folder(server_id):
    data = request.get_json()
    os.makedirs(os.path.join(get_server_dir(server_id), data.get('foldername', '')), exist_ok=True)
    return jsonify({'success': True})

@app.route('/api/rename/<server_id>', methods=['POST'])
def api_rename(server_id):
    d = request.get_json()
    server_dir = get_server_dir(server_id)
    old_path = os.path.join(server_dir, d.get('old_name', ''))
    new_path = os.path.join(server_dir, d.get('new_name', ''))
    if os.path.exists(old_path):
        os.rename(old_path, new_path)
        return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404

@app.route('/api/unzip/<server_id>', methods=['POST'])
def api_unzip(server_id):
    data = request.get_json()
    zip_path = os.path.join(get_server_dir(server_id), data.get('filename', ''))
    if os.path.exists(zip_path) and zip_path.endswith('.zip'):
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf: zf.extractall(os.path.dirname(zip_path))
            return jsonify({'status': 'success', 'msg': 'Extracted!'})
        except Exception as e: return jsonify({'status': 'error', 'msg': str(e)})
    return jsonify({'status': 'error', 'msg': 'Invalid zip'}), 400

@app.route('/api/get_startup/<server_id>')
def api_get_startup(server_id):
    server, _ = get_server_by_id(server_id)
    if server: return jsonify({'main_file': server.get('main_file', 'main.py'), 'requirements_file': server.get('requirements_file', 'requirements.txt')})
    return jsonify({'main_file': 'main.py', 'requirements_file': 'requirements.txt'})

@app.route('/api/set_startup/<server_id>', methods=['POST'])
def api_set_startup(server_id):
    d = request.get_json()
    users = load_users()
    for uname, udata in users.items():
        if uname == 'admin': continue
        servers = udata.get('servers', [])
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                s['main_file'] = d.get('main_file', 'main.py')
                s['requirements_file'] = d.get('requirements_file')
                save_users(users)
                return jsonify({'success': True})
    return jsonify({'error': 'Not found'}), 404

# ============================================
# 🔥 ACCOUNT SYSTEM - register / sign in / free test server / server login
# ============================================

FREE_TRIAL = {'ram': '512MB', 'disk': '1GB', 'cpu_limit': 30, 'days': 2}
FREE_SERVERS_PER_ACCOUNT = 1        # free test servers every new account can claim
PROVISION_SECONDS = (30, 40)        # how long "server is being created" takes
SERVER_TYPES = ('python', 'node', 'static')
RESERVED_USERNAMES = {'admin', 'root', 'administrator', 'support', 'system', 'api', 'null'}
USERNAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{2,19}$')

ACCOUNT_LOCK = threading.Lock()
RATE_BUCKETS = {}
LOGIN_FAILS = {}

def client_ip():
    header = os.environ.get('CLIENT_IP_HEADER')
    if header:
        return request.headers.get(header, '').split(',')[0].strip() or request.remote_addr or 'unknown'
    ip = request.headers.get('CF-Connecting-IP') or request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
    return ip or request.remote_addr or 'unknown'

def rate_allow(key, limit, window):
    now = time.time()
    if len(RATE_BUCKETS) > 5000:
        for k in list(RATE_BUCKETS.keys()):
            RATE_BUCKETS[k] = [x for x in RATE_BUCKETS[k] if now - x < 3600]
            if not RATE_BUCKETS[k]: del RATE_BUCKETS[k]
    hits = [x for x in RATE_BUCKETS.get(key, []) if now - x < window]
    if len(hits) >= limit:
        RATE_BUCKETS[key] = hits
        return False
    hits.append(now)
    RATE_BUCKETS[key] = hits
    return True

def login_blocked(key):
    info = LOGIN_FAILS.get(key)
    return bool(info and info['until'] > time.time())

def login_failed(key):
    info = LOGIN_FAILS.setdefault(key, {'count': 0, 'until': 0})
    if info['until'] and info['until'] <= time.time():
        info['count'], info['until'] = 0, 0
    info['count'] += 1
    if info['count'] >= 5:
        info['until'] = time.time() + 300
        info['count'] = 0

def login_succeeded(key):
    LOGIN_FAILS.pop(key, None)

def safe_eq(a, b):
    return hmac.compare_digest(str(a).encode('utf-8'), str(b).encode('utf-8'))

def find_account(users, username):
    low = username.lower()
    for uname, data in users.items():
        if uname == 'admin' or not isinstance(data, dict): continue
        if uname.lower() == low: return uname, data
    return None, None

def account_password_ok(data, password):
    if data.get('pw_hash'):
        return check_password_hash(data['pw_hash'], password)
    stored = data.get('password')
    return bool(stored) and safe_eq(stored, password)

def current_account():
    name = session.get('acct')
    if not name: return None, None
    users = load_users()
    data = users.get(name)
    if name == 'admin' or not isinstance(data, dict) or data.get('banned'):
        session.pop('acct', None)
        return None, None
    return name, data

def parse_expiry(value):
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try: return datetime.strptime(value, fmt)
        except: pass
    return None

def public_base():
    host = request.host
    local = host.startswith('localhost') or host.startswith('127.0.0.1') or host.startswith('192.168')
    return ('http' if local else 'https') + '://' + host

def server_view(s):
    """Safe, template-friendly view of a server dict (only used for its owner)."""
    now = time.time()
    until = s.get('provisioning_until') or 0
    exp = parse_expiry(s.get('expiry', ''))
    expired = bool(exp and datetime.now() > exp)
    if until > now: state = 'provisioning'
    elif s.get('suspended'): state = 'suspended'
    elif expired: state = 'expired'
    elif s.get('rate_limit_exceeded'): state = 'limited'
    elif s.get('status') == 'running': state = 'running'
    else: state = 'stopped'
    seconds_left = int((exp - datetime.now()).total_seconds()) if exp else None
    view = {
        'server_id': s.get('server_id', ''), 'type': s.get('type', 'python'),
        'ram': s.get('ram', ''), 'disk': s.get('disk', ''), 'cpu_limit': s.get('cpu_limit', 80),
        'state': state, 'plan': s.get('plan', ''), 'created': s.get('created', ''),
        'expiry': (s.get('expiry', '') or '')[:10], 'seconds_left': seconds_left,
        'login_url': s.get('login_url') or f"/{s.get('server_id', '')}/login",
        'provisioning': state == 'provisioning',
        'srv_user': '', 'srv_pass': ''
    }
    if state != 'provisioning':
        view['srv_user'] = s.get('srv_user', '')
        view['srv_pass'] = s.get('srv_pass', '')
    return view

def start_server_session(owner, server):
    """Log somebody into the panel of ONE server."""
    view = server_view(server)
    if view['state'] == 'provisioning':
        return render_template('auth.html', mode='server', server_id=server.get('server_id'), error="This server is still being created. Please try again in a moment.")
    if view['state'] == 'suspended' or (load_users().get(owner) or {}).get('banned'):
        return render_template('error.html', error_type='suspended', server_link=server.get('server_id'))
    if view['state'] == 'expired':
        return render_template('error.html', error_type='expired', server_link=server.get('server_id'))
    session['user'] = owner
    session['role'] = 'user'
    session['current_server_id'] = server.get('server_id')
    return redirect(url_for('server_home', server_id=server.get('server_id')))

def create_node_files(server_dir):
    main_js = os.path.join(server_dir, 'main.js')
    if not os.path.exists(main_js):
        with open(main_js, 'w', encoding='utf-8') as f:
            f.write('''// JUBAYER HOSTING - Default Node.js App
console.log("=".repeat(40));
console.log("Node.js app is running on JUBAYER HOSTING");
console.log("Server is ready!");
console.log("=".repeat(40));

let counter = 0;
setInterval(() => {
    counter++;
    console.log(`[${new Date().toLocaleTimeString()}] Heartbeat #${counter} | Server active`);
}, 10000);
''')
    pkg = os.path.join(server_dir, 'package.json')
    if not os.path.exists(pkg):
        with open(pkg, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'name': 'my-node-app', 'version': '1.0.0', 'main': 'main.js',
                                'scripts': {'start': 'node main.js'}, 'dependencies': {}}, indent=2) + '\n')

def unique_server_credentials(users):
    taken_users = {u.lower() for u in users.keys()}
    taken_srv = set()
    for data in users.values():
        if isinstance(data, dict) and isinstance(data.get('servers'), list):
            for s in data['servers']:
                if isinstance(s, dict) and s.get('srv_user'): taken_srv.add(s['srv_user'].lower())
    alphabet = string.ascii_lowercase + string.digits
    while True:
        srv_user = 'srv_' + ''.join(secrets.choice(alphabet) for _ in range(6))
        if srv_user not in taken_srv and srv_user not in taken_users: break
    pw_chars = string.ascii_letters + string.digits
    srv_pass = ''.join(secrets.choice(pw_chars) for _ in range(12))
    return srv_user, srv_pass

# ---------- register / sign in ----------

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET' and session.get('acct'):
        return redirect(url_for('dashboard'))
    if not load_settings().get('registration_open', True):
        return render_template('auth.html', mode='register', error='Registration is temporarily closed. Please check back later.', form={}, closed=True), 403
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        
        def fail(msg, code=200):
            return render_template('auth.html', mode='register', error=msg, form={'username': username}), code
        
        if request.form.get('website'):
            return fail('Registration failed!')
        if not USERNAME_RE.match(username):
            return fail('Username must be 3-20 characters: letters, numbers or underscore, starting with a letter.')
        if len(password) < 6:
            return fail('Password must be at least 6 characters!')
        if password != confirm:
            return fail('Passwords do not match!')
        if not rate_allow(('register', client_ip()), 5, 3600):
            return fail('Too many accounts were created from this network. Please try again later.', 429)
        
        with ACCOUNT_LOCK:
            users = load_users()
            if username.lower() in RESERVED_USERNAMES or any(k.lower() == username.lower() for k in users.keys()):
                return fail('This username is already taken. Please choose another one.')
            users[username] = {
                'pw_hash': generate_password_hash(password),
                'role': 'user', 'account': True, 'free_claimed': 0,
                'created': str(datetime.now()), 'servers': []
            }
            save_users(users)
        
        session['acct'] = username
        log_activity('register', username, 'New account registered')
        return redirect(url_for('dashboard', new=1))
    return render_template('auth.html', mode='register', error=None, form={})

@app.route('/signin', methods=['GET', 'POST'])
def account_login():
    if request.method == 'GET' and session.get('acct'):
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        key = ('login', client_ip(), username.lower())
        form = {'username': username}
        if login_blocked(key):
            return render_template('auth.html', mode='login', error='Too many failed attempts. Please wait a few minutes and try again.', form=form), 429
        users = load_users()
        name, data = find_account(users, username)
        if name and account_password_ok(data, password):
            if data.get('banned'):
                return render_template('auth.html', mode='login', error='This account has been suspended. Please contact support.', form=form), 403
            login_succeeded(key)
            session['acct'] = name
            return redirect(url_for('dashboard'))
        login_failed(key)
        return render_template('auth.html', mode='login', error='Invalid username or password!', form=form)
    return render_template('auth.html', mode='login', error=None, form={})

@app.route('/server-login', methods=['GET', 'POST'])
def server_login_generic():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        key = ('srvlogin', client_ip())
        if login_blocked(key):
            return render_template('auth.html', mode='server', server_id=None, error='Too many failed attempts. Please wait a few minutes and try again.', form={'username': username}), 429
        users = load_users()
        for uname, data in users.items():
            if uname == 'admin' or not isinstance(data, dict): continue
            servers = data.get('servers', [])
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('srv_user') and safe_eq(s['srv_user'], username) and safe_eq(s.get('srv_pass', ''), password):
                    login_succeeded(key)
                    return start_server_session(uname, s)
        login_failed(key)
        return render_template('auth.html', mode='server', server_id=None, error='Invalid server username or password!', form={'username': username})
    return render_template('auth.html', mode='server', server_id=None, error=None, form={})

# ---------- dashboard ----------

@app.route('/dashboard')
def dashboard():
    name, data = current_account()
    if not name:
        return redirect(url_for('account_login'))
    servers = data.get('servers', [])
    if not isinstance(servers, list): servers = []
    views = [server_view(s) for s in servers if isinstance(s, dict)]
    views.sort(key=lambda v: v['created'], reverse=True)
    is_account = bool(data.get('account'))
    free_left = max(0, trial_cfg()['free_per_account'] - int(data.get('free_claimed', 0))) if is_account else 0
    return render_template('dashboard.html', username=name, servers=views, free_left=free_left,
                           is_account=is_account, trial=trial_cfg(), base=public_base(),
                           open_new=(request.args.get('new') == '1'))

@app.route('/open/<server_id>')
def open_server_panel(server_id):
    name, data = current_account()
    if not name:
        return redirect(url_for('account_login'))
    servers = data.get('servers', [])
    if isinstance(servers, list):
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                return start_server_session(name, s)
    return redirect(url_for('dashboard'))

@app.route('/api/account/create_server', methods=['POST'])
def api_account_create_server():
    name, data = current_account()
    if not name:
        return jsonify({'status': 'error', 'msg': 'Please sign in first!'}), 401
    if not data.get('account'):
        return jsonify({'status': 'error', 'msg': 'Free test servers are only available for newly registered accounts.'}), 403
    body = request.get_json(silent=True) or {}
    server_type = str(body.get('type', 'python')).strip().lower()
    if server_type not in SERVER_TYPES:
        return jsonify({'status': 'error', 'msg': 'Unsupported server type!'}), 400
    if not rate_allow(('create', client_ip()), 10, 3600):
        return jsonify({'status': 'error', 'msg': 'Too many requests. Please try again later.'}), 429
    
    with ACCOUNT_LOCK:
        users = load_users()
        acc = users.get(name)
        if not isinstance(acc, dict):
            return jsonify({'status': 'error', 'msg': 'Account not found!'}), 401
        trial = trial_cfg()
        if int(acc.get('free_claimed', 0)) >= trial['free_per_account']:
            return jsonify({'status': 'error', 'msg': 'You have already used your free test server.'}), 403
        if not isinstance(acc.get('servers'), list):
            acc['servers'] = []
        
        server_id = str(uuid.uuid4())[:8]
        while os.path.exists(os.path.join(BOTS_DIR, server_id)):
            server_id = str(uuid.uuid4())[:8]
        srv_user, srv_pass = unique_server_credentials(users)
        
        server_dir = get_server_dir(server_id)
        if server_type == 'node':
            create_node_files(server_dir)
            main_file, req_file = 'main.js', ''
        elif server_type == 'static':
            create_static_files(server_dir)
            main_file, req_file = 'index.html', ''
        else:
            create_default_files(server_dir)
            main_file, req_file = 'main.py', 'requirements.txt'
        
        now = datetime.now()
        expiry = now + timedelta(days=trial['days'])
        if expiry.microsecond == 0:
            expiry = expiry.replace(microsecond=1)
        duration = round(random.uniform(*PROVISION_SECONDS), 1)
        base = public_base()
        
        acc['servers'].append({
            'server_id': server_id, 'link': server_id,
            'login_url': f"/{server_id}/login", 'dashboard_url': f"/{server_id}/home",
            'full_link': f"{base}/{server_id}/login",
            'type': server_type, 'ram': trial['ram'], 'disk': trial['disk'],
            'status': 'stopped', 'pid': None,
            'created': str(now), 'expiry': str(expiry),
            'main_file': main_file, 'requirements_file': req_file,
            'cpu_limit': trial['cpu_limit'], 'rate_limit_exceeded': False, 'stopped_by_user': False,
            'plan': 'free-test', 'srv_user': srv_user, 'srv_pass': srv_pass,
            'provisioning_started': time.time(), 'provisioning_until': time.time() + duration
        })
        acc['free_claimed'] = int(acc.get('free_claimed', 0)) + 1
        save_users(users)
    
    if server_type == 'node': ensure_runtime_async('node')
    if server_type == 'static': ensure_runtime_async('cloudflared')
    log_activity('server', name, f"Free {server_type} test server {server_id} created")
    return jsonify({'status': 'success', 'server_id': server_id, 'duration': duration})

@app.route('/api/account/server_status/<server_id>')
def api_account_server_status(server_id):
    name, data = current_account()
    if not name:
        return jsonify({'status': 'error', 'msg': 'Please sign in first!'}), 401
    servers = data.get('servers', [])
    if isinstance(servers, list):
        for s in servers:
            if isinstance(s, dict) and s.get('server_id') == server_id:
                view = server_view(s)
                if view['state'] == 'provisioning':
                    total = max(1.0, (s.get('provisioning_until') or 0) - (s.get('provisioning_started') or 0))
                    remaining = max(0.0, (s.get('provisioning_until') or 0) - time.time())
                    return jsonify({'status': 'success', 'state': 'provisioning', 'remaining': round(remaining, 2),
                                    'total': round(total, 2), 'type': view['type']})
                view['full_url'] = public_base() + view['login_url']
                return jsonify({'status': 'success', 'state': 'ready', 'server': view})
    return jsonify({'status': 'error', 'msg': 'Not found'}), 404

# ============================================
# 🔥 ADVANCED FEATURES - runtimes, static sites + Cloudflare link, admin tools
# ============================================

SETTINGS_FILE = 'settings.json'
ACTIVITY_FILE = 'activity.json'
RUNTIME_DIR = os.path.join(os.path.dirname(os.path.abspath(BOTS_DIR)), 'runtime')
NODE_MAJOR = os.environ.get('NODE_MAJOR', '22')
NODE_DIST_BASE = os.environ.get('NODE_DIST_BASE', 'https://nodejs.org/dist')
NODE_MIRROR_BASE = 'https://npmmirror.com/mirrors/node'
CLOUDFLARED_DIST_BASE = os.environ.get('CLOUDFLARED_DIST_BASE', 'https://github.com/cloudflare/cloudflared/releases/latest/download')
APP_START = time.time()
SIZE_RE = re.compile(r'^\d{1,5}(MB|GB)$')

DEFAULT_SETTINGS = {
    'registration_open': True,
    'maintenance': False,
    'announcement': {'enabled': False, 'text': '', 'type': 'info'},
    'trial': {'days': 2, 'ram': '512MB', 'disk': '1GB', 'cpu_limit': 30},
    'free_per_account': 1,
    'max_tunnels': 25
}
SETTINGS_LOCK = threading.Lock()
ACTIVITY_LOCK = threading.Lock()
_SETTINGS_CACHE = {'mtime': None, 'data': None}
_TTL = {}

def _ttl_cache(key, ttl, fn):
    hit = _TTL.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _TTL[key] = (time.time(), val)
    return val

def load_settings():
    data, mtime = {}, None
    try:
        mtime = os.path.getmtime(SETTINGS_FILE)
        if _SETTINGS_CACHE['data'] is not None and _SETTINGS_CACHE['mtime'] == mtime:
            return _SETTINGS_CACHE['data']
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        mtime = None
    except Exception:
        data = {}
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict): merged[k].update(v)
            else: merged[k] = v
    _SETTINGS_CACHE['mtime'], _SETTINGS_CACHE['data'] = mtime, merged
    return merged

def save_settings(data):
    with SETTINGS_LOCK:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        _SETTINGS_CACHE['mtime'], _SETTINGS_CACHE['data'] = None, None

def trial_cfg():
    s = load_settings()
    t = dict(FREE_TRIAL)
    t.update({k: v for k, v in s.get('trial', {}).items() if k in t})
    t['free_per_account'] = int(s.get('free_per_account', FREE_SERVERS_PER_ACCOUNT))
    return t

def log_activity(kind, actor, text):
    try:
        with ACTIVITY_LOCK:
            items = []
            if os.path.exists(ACTIVITY_FILE):
                try:
                    with open(ACTIVITY_FILE, 'r', encoding='utf-8') as f: items = json.load(f)
                except Exception: items = []
            items.append({'t': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'kind': kind, 'actor': actor, 'text': text})
            items = items[-500:]
            with open(ACTIVITY_FILE, 'w', encoding='utf-8') as f:
                json.dump(items, f, ensure_ascii=False)
    except Exception:
        pass

@app.context_processor
def inject_site_settings():
    ann = load_settings().get('announcement', {})
    return {'announcement': ann if ann.get('enabled') and ann.get('text') else None}

@app.before_request
def maintenance_gate():
    if not load_settings().get('maintenance'): return None
    if session.get('role') == 'admin': return None
    p = request.path
    if p in ('/register', '/signin', '/server-login', '/dashboard') or p.startswith('/open/') or re.match(r'^/[0-9a-f]{8}/(login|home)$', p):
        return render_template('error.html', error_type='maintenance', server_link=''), 503
    return None

# ---------- helpers ----------

def size_to_bytes(text):
    m = re.match(r'^(\d+(?:\.\d+)?)\s*(MB|GB|TB)$', str(text).strip().upper())
    if not m: return 1024 ** 3
    return int(float(m.group(1)) * {'MB': 1024 ** 2, 'GB': 1024 ** 3, 'TB': 1024 ** 4}[m.group(2)])

def dir_size(path, limit=None):
    total = 0
    for root, dirs, files in os.walk(path):
        for name in files:
            try: total += os.path.getsize(os.path.join(root, name))
            except OSError: pass
            if limit and total > limit: return total
    return total

def update_server(server_id, fn):
    with ACCOUNT_LOCK:
        users = load_users()
        for uname, data in users.items():
            if uname == 'admin' or not isinstance(data, dict): continue
            servers = data.get('servers')
            if not isinstance(servers, list): continue
            for s in servers:
                if isinstance(s, dict) and s.get('server_id') == server_id:
                    result = fn(s, uname, data)
                    save_users(users)
                    return result
    return None

def panel_guard(server_id):
    """Session check for panel APIs: the owner's panel session or an admin."""
    role = session.get('role')
    allowed = ('user' in session) and (role == 'admin' or (role == 'user' and session.get('current_server_id') == server_id))
    if not allowed:
        return None, None, (jsonify({'status': 'error', 'msg': 'Please sign in to this server first.'}), 403)
    s, owner = get_server_by_id(server_id)
    if not s:
        return None, None, (jsonify({'status': 'error', 'msg': 'Server not found'}), 404)
    state = server_view(s)['state']
    if role != 'admin' and state in ('expired', 'suspended', 'provisioning'):
        return None, None, (jsonify({'status': 'error', 'msg': 'This server is not available (%s).' % state}), 403)
    return s, owner, None

def admin_only(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if 'user' not in session or session.get('role') != 'admin':
            return jsonify({'error': 'Unauthorized'}), 403
        return fn(*args, **kwargs)
    return wrapper

# ---------- runtime manager: Node.js + cloudflared ----------

RUNTIME_JOBS = {}
NODE_INSTALL_LOCK = threading.Lock()
CF_INSTALL_LOCK = threading.Lock()
_NOWIN = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}

def _exe_works(path, arg='--version'):
    try:
        r = subprocess.run([path, arg], capture_output=True, text=True, timeout=10, **_NOWIN)
        out = (r.stdout or r.stderr or '').strip().splitlines()
        return out[0].strip() if (r.returncode == 0 and out) else None
    except Exception:
        return None

def _is_android():
    return hasattr(sys, 'getandroidapilevel') or 'com.termux' in os.environ.get('PREFIX', '') or (bool(os.environ.get('ANDROID_ROOT')) and os.path.exists('/system/bin'))

def node_install_hint():
    if _is_android():
        return "Termux/Android detected. Install Node.js with: pkg install nodejs"
    if sys.platform == 'win32':
        return "Install Node.js from https://nodejs.org (or run: winget install OpenJS.NodeJS.LTS), then restart this app."
    if sys.platform == 'darwin':
        return "Install Node.js with: brew install node"
    return "Install Node.js with: sudo apt install nodejs npm (Debian/Ubuntu), apk add nodejs npm (Alpine) or dnf install nodejs (Fedora), then restart this app."

def _local_node():
    for p in (os.path.join(RUNTIME_DIR, 'node', 'node.exe'), os.path.join(RUNTIME_DIR, 'node', 'bin', 'node')):
        if os.path.isfile(p) and _exe_works(p): return p
    return None

def _system_node():
    cands = []
    env = os.environ.get('NODE_BIN')
    if env: cands.append(env)
    for n in ('node', 'nodejs'):
        p = shutil.which(n)
        if p: cands.append(p)
    prefix = os.environ.get('PREFIX')
    if prefix: cands.append(os.path.join(prefix, 'bin', 'node'))
    cands += ['/usr/local/bin/node', '/usr/bin/node', '/opt/homebrew/bin/node', '/snap/bin/node',
              r'C:\Program Files\nodejs\node.exe', r'C:\Program Files (x86)\nodejs\node.exe',
              os.path.expanduser('~/.volta/bin/node')]
    cands += sorted(glob.glob(os.path.expanduser('~/.nvm/versions/node/*/bin/node')), reverse=True)
    seen = set()
    for p in cands:
        if p and p not in seen:
            seen.add(p)
            if os.path.isfile(p) and _exe_works(p): return p
    return None

def find_node():
    return _system_node() or _local_node()

def find_npm(node_path):
    folder = os.path.dirname(node_path)
    for name in ('npm.cmd', 'npm'):
        p = os.path.join(folder, name)
        if os.path.isfile(p): return p
    return shutil.which('npm')

def node_env(node_path):
    env = os.environ.copy()
    env['PATH'] = os.path.dirname(node_path) + os.pathsep + env.get('PATH', '')
    return env

def _http_open(url, timeout=30):
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'ShadowHosting/1.0'}), timeout=timeout)

def _download(url, dest, log, label, on_pct=None):
    sha = hashlib.sha256()
    with _http_open(url, 60) as r, open(dest, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        done, last = 0, -10
        while True:
            chunk = r.read(262144)
            if not chunk: break
            f.write(chunk); sha.update(chunk); done += len(chunk)
            if total:
                pct = int(done * 100 / total)
                if on_pct: on_pct(pct)
                if pct >= last + 10:
                    last = pct
                    log(f"Downloading {label}... {pct}%")
    return sha.hexdigest()

def _safe_extract_tar(tf, dest):
    try:
        tf.extractall(dest, filter='data')
        return
    except TypeError:
        pass
    base = os.path.realpath(dest)
    for m in tf.getmembers():
        target = os.path.realpath(os.path.join(dest, m.name))
        if not (target == base or target.startswith(base + os.sep)):
            raise RuntimeError('Unsafe path in archive')
    tf.extractall(dest)

def _node_target():
    system, machine = platform.system().lower(), platform.machine().lower()
    arch = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64', 'armv7l': 'armv7l', 'armv8l': 'armv7l'}.get(machine)
    if not arch: return None
    if system == 'linux':
        musl = os.path.exists('/etc/alpine-release') or bool(glob.glob('/lib/ld-musl*'))
        return None if musl else ('linux', arch, 'tar.gz')
    if system == 'darwin' and arch in ('x64', 'arm64'): return ('darwin', arch, 'tar.gz')
    if system == 'windows' and arch in ('x64', 'arm64'): return ('win', arch, 'zip')
    return None

def install_node(log=print, on_pct=None, force=False):
    """Download an official portable Node.js build into ./runtime/node (checksum verified)."""
    with NODE_INSTALL_LOCK:
        if not force:
            existing = find_node()
            if existing: return existing, None
        if _is_android():
            return None, node_install_hint()
        target = _node_target()
        if not target:
            return None, "Automatic install is not available for this system. " + node_install_hint()
        os_tag, arch, ext = target
        bases = [NODE_DIST_BASE] + ([NODE_MIRROR_BASE] if NODE_DIST_BASE == 'https://nodejs.org/dist' else [])
        last_err = 'unknown error'
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        for base in bases:
            tmp_archive = os.path.join(RUNTIME_DIR, 'node-download.' + ext)
            tmp_dir = os.path.join(RUNTIME_DIR, 'node-extract')
            try:
                log(f"Looking up Node.js {NODE_MAJOR}.x on {base} ...")
                sums = _http_open(f"{base}/latest-v{NODE_MAJOR}.x/SHASUMS256.txt", 30).read().decode('utf-8', 'replace')
                m = re.search(r'^([0-9a-f]{64})\s+(node-v[\d.]+-%s-%s\.%s)\s*$' % (os_tag, arch, re.escape(ext)), sums, re.M)
                if not m: raise RuntimeError('no matching Node.js build for this system')
                expected, fname = m.groups()
                got = _download(f"{base}/latest-v{NODE_MAJOR}.x/{fname}", tmp_archive, log, fname, on_pct)
                if got != expected: raise RuntimeError('checksum mismatch, download discarded')
                log("Checksum OK. Extracting...")
                shutil.rmtree(tmp_dir, ignore_errors=True); os.makedirs(tmp_dir)
                if ext == 'zip':
                    with zipfile.ZipFile(tmp_archive) as zf:
                        for n in zf.namelist():
                            t = os.path.realpath(os.path.join(tmp_dir, n))
                            if not t.startswith(os.path.realpath(tmp_dir) + os.sep): raise RuntimeError('unsafe path in archive')
                        zf.extractall(tmp_dir)
                else:
                    with tarfile.open(tmp_archive, 'r:gz') as tf: _safe_extract_tar(tf, tmp_dir)
                inner = [d for d in os.listdir(tmp_dir) if os.path.isdir(os.path.join(tmp_dir, d))]
                if len(inner) != 1: raise RuntimeError('unexpected archive layout')
                node_dir = os.path.join(RUNTIME_DIR, 'node')
                shutil.rmtree(node_dir, ignore_errors=True)
                shutil.move(os.path.join(tmp_dir, inner[0]), node_dir)
                path = _local_node()
                if not path: raise RuntimeError('the downloaded Node.js does not run on this system')
                log(f"Node.js installed: {_exe_works(path)}")
                return path, None
            except Exception as e:
                last_err = str(e)
                log(f"Download source failed: {last_err}")
            finally:
                try: os.remove(tmp_archive)
                except OSError: pass
                shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, f"Could not install Node.js automatically ({last_err}). " + node_install_hint()

def _system_cloudflared():
    cands = []
    env = os.environ.get('CLOUDFLARED_BIN')
    if env: cands.append(env)
    p = shutil.which('cloudflared')
    if p: cands.append(p)
    cands += [os.path.abspath('cloudflared'), os.path.abspath('cloudflared.exe'), '/usr/local/bin/cloudflared', '/usr/bin/cloudflared',
              '/opt/homebrew/bin/cloudflared', r'C:\Program Files (x86)\cloudflared\cloudflared.exe', r'C:\Program Files\cloudflared\cloudflared.exe']
    prefix = os.environ.get('PREFIX')
    if prefix: cands.append(os.path.join(prefix, 'bin', 'cloudflared'))
    seen = set()
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            if os.path.isfile(c) and _exe_works(c): return c
    return None

def _local_cloudflared():
    p = os.path.join(RUNTIME_DIR, 'cloudflared.exe' if sys.platform == 'win32' else 'cloudflared')
    return p if os.path.isfile(p) and _exe_works(p) else None

def find_cloudflared():
    return _system_cloudflared() or _local_cloudflared()

def _cloudflared_asset():
    system, machine = platform.system().lower(), platform.machine().lower()
    if system == 'linux':
        arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64', 'armv7l': 'arm', 'armv8l': 'arm', 'armv6l': 'arm', 'i386': '386', 'i686': '386'}.get(machine)
        return f'cloudflared-linux-{arch}' if arch else None
    if system == 'darwin':
        arch = {'x86_64': 'amd64', 'arm64': 'arm64'}.get(machine)
        return f'cloudflared-darwin-{arch}.tgz' if arch else None
    if system == 'windows':
        arch = {'amd64': 'amd64', 'x86_64': 'amd64', 'x86': '386', 'i386': '386'}.get(machine)
        return f'cloudflared-windows-{arch}.exe' if arch else None
    return None

def install_cloudflared(log=print, on_pct=None, force=False):
    with CF_INSTALL_LOCK:
        if not force:
            existing = find_cloudflared()
            if existing: return existing, None
        asset = _cloudflared_asset()
        if not asset:
            return None, "Automatic cloudflared install is not available for this system. Install it from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        dest = os.path.join(RUNTIME_DIR, 'cloudflared.exe' if sys.platform == 'win32' else 'cloudflared')
        tmp = dest + '.download'
        try:
            _download(f"{CLOUDFLARED_DIST_BASE}/{asset}", tmp, log, asset, on_pct)
            if asset.endswith('.tgz'):
                with tarfile.open(tmp, 'r:gz') as tf:
                    member = next((m for m in tf.getmembers() if os.path.basename(m.name) == 'cloudflared' and m.isfile()), None)
                    if not member: raise RuntimeError('cloudflared binary not found in archive')
                    with tf.extractfile(member) as src, open(dest, 'wb') as out: shutil.copyfileobj(src, out)
                os.remove(tmp)
            else:
                shutil.move(tmp, dest)
            os.chmod(dest, 0o755)
            v = _exe_works(dest)
            if not v: raise RuntimeError('the downloaded cloudflared does not run on this system')
            log(f"cloudflared installed: {v}")
            return dest, None
        except Exception as e:
            try: os.remove(tmp)
            except OSError: pass
            return None, f"Could not install cloudflared automatically ({e})."

def start_runtime_install(name, force=False):
    job = RUNTIME_JOBS.get(name)
    if job and job.get('state') == 'running': return job
    job = {'state': 'running', 'progress': 0, 'log': [], 'error': None, 'started': time.time()}
    RUNTIME_JOBS[name] = job
    def log(msg):
        job['log'].append(msg); del job['log'][:-60]
    def work():
        try:
            fn = install_node if name == 'node' else install_cloudflared
            path, err = fn(log, lambda p: job.__setitem__('progress', p), force)
            job['state'] = 'done' if path else 'error'
            job['error'] = err
            job['progress'] = 100 if path else job['progress']
        except Exception as e:
            job['state'], job['error'] = 'error', str(e)
        _TTL.pop('runtime', None)
        log_activity('runtime', 'system', f"{name} install: {job['state']}")
    threading.Thread(target=work, daemon=True).start()
    return job

def ensure_runtime_async(name):
    job = RUNTIME_JOBS.get(name)
    if job and job.get('state') == 'running': return
    if (name == 'node' and find_node()) or (name == 'cloudflared' and find_cloudflared()): return
    start_runtime_install(name)

def runtime_status():
    def build():
        node, cf = find_node(), find_cloudflared()
        def job_view(n):
            j = RUNTIME_JOBS.get(n)
            return {k: j[k] for k in ('state', 'progress', 'log', 'error')} if j else None
        return {
            'node': {'installed': bool(node), 'path': node, 'version': _exe_works(node) if node else None, 'job': job_view('node'), 'hint': node_install_hint(), 'private_copy': bool(_local_node())},
            'cloudflared': {'installed': bool(cf), 'path': cf, 'version': _exe_works(cf) if cf else None, 'job': job_view('cloudflared')},
            'python': {'installed': True, 'path': sys.executable, 'version': sys.version.split()[0]}
        }
    return _ttl_cache('runtime', 20, build)

# ---------- static sites + Cloudflare quick tunnels ----------

SITES = {}
SITES_LOCK = threading.RLock()
SITE_BLOCKED_EXT = ('.py', '.pyc', '.log', '.env', '.sh', '.db', '.sqlite', '.pem', '.key')
SITE_BLOCKED_NAMES = {'requirements.txt', 'output.log', 'users.json', 'package-lock.json'}
SITE_INDEX_NAMES = ('index.html', 'index.htm')
SITE_PLACEHOLDER = b"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Website not uploaded yet</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#06080f;color:#e8ecf6;font-family:system-ui,sans-serif;text-align:center;padding:24px}h1{font-size:24px;margin:0 0 8px}p{color:#98a2bd;margin:0}</style></head><body><div><h1>No website here yet</h1><p>Upload an index.html (or a zip of your site) in the panel to publish it.</p></div></body></html>"""
_site_monitor_started = False

def find_site_root(server_dir):
    def has_index(d): return any(os.path.isfile(os.path.join(d, n)) for n in SITE_INDEX_NAMES)
    if not os.path.isdir(server_dir): return server_dir
    if has_index(server_dir): return server_dir
    try:
        subs = [d for d in sorted(os.listdir(server_dir)) if os.path.isdir(os.path.join(server_dir, d)) and not d.startswith('.') and d not in ('node_modules', '__pycache__')]
    except OSError:
        return server_dir
    for pref in ('public', 'dist', 'build', 'www', 'site', 'docs', 'html', 'web'):
        if pref in subs and has_index(os.path.join(server_dir, pref)): return os.path.join(server_dir, pref)
    with_index = [d for d in subs if has_index(os.path.join(server_dir, d))]
    if len(with_index) == 1: return os.path.join(server_dir, with_index[0])
    return server_dir

def site_info(server_id):
    server_dir = get_server_dir(server_id)
    root = find_site_root(server_dir)
    count, total = 0, 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'node_modules']
        for n in files:
            if n.startswith('.'): continue
            count += 1
            try: total += os.path.getsize(os.path.join(r, n))
            except OSError: pass
        if count > 20000: break
    rel = os.path.relpath(root, server_dir)
    return {'root': '/' if rel == '.' else '/' + rel.replace('\\', '/'),
            'has_index': any(os.path.isfile(os.path.join(root, n)) for n in SITE_INDEX_NAMES),
            'files': count, 'bytes': total}

class _SiteHandler(SimpleHTTPRequestHandler):
    site_id = None
    server_version = 'ShadowSite'
    sys_version = ''
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=find_site_root(get_server_dir(self.site_id)), **kwargs)
    def log_message(self, *args): pass
    def _blocked(self, fs_path):
        parts = os.path.relpath(fs_path, self.directory).replace('\\', '/').split('/')
        for p in parts:
            if p.startswith('.') and p not in ('.', '..', '.well-known'): return True
            if p == '..' or p == 'node_modules': return True
        base = parts[-1].lower()
        return base in SITE_BLOCKED_NAMES or base.endswith(SITE_BLOCKED_EXT)
    def send_head(self):
        fs = self.translate_path(self.path)
        if self._blocked(fs):
            self.send_error(404); return None
        if self.path.split('?')[0] in ('/', '') and not any(os.path.isfile(os.path.join(self.directory, n)) for n in SITE_INDEX_NAMES):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(SITE_PLACEHOLDER)))
            self.end_headers()
            return io.BytesIO(SITE_PLACEHOLDER)
        return super().send_head()
    def list_directory(self, path):
        self.send_error(404, 'Not found'); return None
    def send_error(self, code, message=None, explain=None):
        if code == 404 and self.command != 'HEAD':
            p = os.path.join(self.directory, '404.html')
            if os.path.isfile(p):
                try:
                    with open(p, 'rb') as f: body = f.read()
                    self.send_response(404)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers(); self.wfile.write(body); return
                except Exception: pass
        super().send_error(code, message, explain)
    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

def _kill_proc(proc):
    try:
        if proc and proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=4)
            except Exception: proc.kill()
    except Exception:
        pass

def _site_cleanup(site):
    _kill_proc(site.get('proc'))
    httpd = site.get('httpd')
    site['httpd'] = None
    if httpd:
        def close():
            try: httpd.shutdown(); httpd.server_close()
            except Exception: pass
        threading.Thread(target=close, daemon=True).start()

def _site_log(site, line):
    site['log'].append(line); del site['log'][:-60]

def _site_worker(server_id, site):
    def fail(msg):
        site['state'], site['error'], site['stage'] = 'error', msg, 'Failed'
        _site_cleanup(site)
    try:
        handler = type('SiteHandler', (_SiteHandler,), {'site_id': server_id})
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        httpd.daemon_threads = True
        site['httpd'], site['port'] = httpd, httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        cf = find_cloudflared()
        if not cf:
            site['stage'] = 'Installing Cloudflare tunnel (one-time)'
            cf, err = install_cloudflared(lambda m: _site_log(site, m))
            if not cf: return fail(err)
        site['stage'] = 'Opening secure tunnel'
        cf_cmd = [cf, 'tunnel', '--no-autoupdate', '--url', f"http://127.0.0.1:{site['port']}"]
        if os.environ.get('CLOUDFLARED_PROTOCOL'):
            cf_cmd[2:2] = ['--protocol', os.environ['CLOUDFLARED_PROTOCOL']]
        proc = subprocess.Popen(cf_cmd,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, **_NOWIN)
        site['proc'] = proc
        url_re = re.compile(r'https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com')
        def reader():
            try:
                for line in iter(proc.stdout.readline, ''):
                    line = line.rstrip()
                    if not line: continue
                    _site_log(site, line)
                    if not site['url']:
                        for found in url_re.findall(line):
                            if not found.startswith('https://api.'):
                                site['url'], site['state'], site['stage'] = found, 'live', 'Live'
                                site['live_since'] = time.time()
                                break
            except Exception:
                pass
            if SITES.get(server_id) is site and site['state'] in ('starting', 'live'):
                if site['state'] == 'starting':
                    site['state'], site['error'] = 'error', 'The Cloudflare tunnel stopped before a link was created. Check the server internet connection.'
                else:
                    site['state'] = 'stopped'
                _site_cleanup(site)
        threading.Thread(target=reader, daemon=True).start()
        deadline = time.time() + 60
        while time.time() < deadline and site['state'] == 'starting':
            time.sleep(0.3)
        if site['state'] == 'starting':
            fail('Cloudflare did not return a link in time. Check the server internet connection and try again.')
    except Exception as e:
        fail(str(e))

def _ensure_site_monitor():
    global _site_monitor_started
    if _site_monitor_started: return
    _site_monitor_started = True
    def loop():
        while True:
            time.sleep(20)
            for sid in list(SITES.keys()):
                try:
                    s, owner = get_server_by_id(sid)
                    users = load_users() if s else {}
                    if not s or server_view(s)['state'] in ('expired', 'suspended') or (users.get(owner) or {}).get('banned'):
                        stop_site(sid); continue
                    site = SITES.get(sid)
                    if site and site['state'] in ('error', 'stopped') and time.time() - site['started'] > 600:
                        SITES.pop(sid, None)
                except Exception:
                    pass
    threading.Thread(target=loop, daemon=True).start()

def start_site(server_id):
    _ensure_site_monitor()
    with SITES_LOCK:
        cur = SITES.get(server_id)
        if cur and cur['state'] in ('starting', 'live'): return cur
        active = sum(1 for x in SITES.values() if x['state'] in ('starting', 'live'))
        if active >= int(load_settings().get('max_tunnels', 25)):
            raise RuntimeError('Too many live links right now. Please try again in a few minutes.')
        site = {'state': 'starting', 'stage': 'Preparing website', 'url': None, 'error': None, 'started': time.time(),
                'httpd': None, 'port': None, 'proc': None, 'log': [], 'live_since': None}
        SITES[server_id] = site
    threading.Thread(target=_site_worker, args=(server_id, site), daemon=True).start()
    return site

def stop_site(server_id):
    with SITES_LOCK:
        site = SITES.pop(server_id, None)
    if not site: return False
    site['state'] = 'stopped'
    _site_cleanup(site)
    return True

def stop_all_sites():
    for sid in list(SITES.keys()):
        stop_site(sid)
atexit.register(stop_all_sites)

def site_status(server_id):
    site = SITES.get(server_id)
    base = {'cloudflared': bool(_ttl_cache('cf_ok', 30, find_cloudflared)), 'info': site_info(server_id)}
    if not site:
        base.update(state='off', stage='', url=None, error=None, log=[], live_since=None)
        return base
    base.update(state=site['state'], stage=site['stage'], url=site['url'], error=site['error'],
                log=site['log'][-12:], live_since=site.get('live_since'))
    return base

def create_static_files(server_dir):
    index = os.path.join(server_dir, 'index.html')
    if not os.path.exists(index):
        with open(index, 'w', encoding='utf-8') as f:
            f.write('''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>My website</title>
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#06080f;color:#e8ecf6;font-family:system-ui,sans-serif;text-align:center;padding:24px}
  .card{max-width:520px;padding:36px;border-radius:24px;background:#0d1220;border:1px solid #1c2438}
  h1{margin:0 0 10px;font-size:28px}p{color:#98a2bd;line-height:1.6;margin:0 0 6px}
  code{background:#161d31;padding:2px 8px;border-radius:6px;color:#8fb0ff}
</style>
</head>
<body>
  <div class="card">
    <h1>Your website is live</h1>
    <p>This is the starter page created by SHADOW HOSTING.</p>
    <p>Upload your own <code>index.html</code> or a zip of your site in the panel to replace it.</p>
  </div>
</body>
</html>
''')

def _clean_rel_path(raw):
    parts = []
    for p in re.split(r'[\\/]+', str(raw)):
        p = re.sub(r'[<>:"|?*\x00-\x1f]', '', p).strip()
        if p in ('', '.', '..'): continue
        parts.append(p[:150])
    return parts

def deploy_site_files(server_id, uploads, replace=False):
    """uploads: list of (relative_path, werkzeug FileStorage). Returns (ok, message)."""
    s, _ = get_server_by_id(server_id)
    server_dir = os.path.abspath(get_server_dir(server_id))
    max_bytes = min(200 * 1024 * 1024, size_to_bytes((s or {}).get('disk', '1GB')))
    max_files = 5000
    zips = [(n, f) for n, f in uploads if n.lower().endswith('.zip')]
    if len(zips) == 1 and len(uploads) == 1:
        name, fs = zips[0]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip'); tmp.close()
        try:
            fs.save(tmp.name)
            try: zf = zipfile.ZipFile(tmp.name)
            except zipfile.BadZipFile: return False, 'That file is not a valid zip archive.'
            with zf:
                entries = []
                declared = 0
                for info in zf.infolist():
                    parts = _clean_rel_path(info.filename)
                    if info.is_dir() or not parts or parts[0] == '__MACOSX' or parts[-1] == '.DS_Store': continue
                    entries.append((info, parts)); declared += info.file_size
                if not entries: return False, 'The zip file is empty.'
                if len(entries) > max_files: return False, f'Too many files in the zip (max {max_files}).'
                if declared > max_bytes: return False, f'The website is too large (max {max_bytes // (1024 * 1024)} MB uncompressed).'
                if replace: _wipe_dir(server_dir)
                written = 0
                for info, parts in entries:
                    target = os.path.abspath(os.path.join(server_dir, *parts))
                    if not target.startswith(server_dir + os.sep): continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, 'wb') as out:
                        while True:
                            chunk = src.read(262144)
                            if not chunk: break
                            written += len(chunk)
                            if written > max_bytes: raise RuntimeError('too large')
                            out.write(chunk)
        except RuntimeError:
            return False, 'The website is too large for this server.'
        finally:
            try: os.remove(tmp.name)
            except OSError: pass
        return True, f'Extracted {len(entries)} files.'
    if replace: _wipe_dir(server_dir)
    total, count = 0, 0
    for rel, fs in uploads:
        parts = _clean_rel_path(rel)
        if not parts: continue
        target = os.path.abspath(os.path.join(server_dir, *parts))
        if not target.startswith(server_dir + os.sep): continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fs.save(target)
        count += 1; total += os.path.getsize(target)
        if total > max_bytes or count > max_files:
            return False, 'The upload is too large for this server.'
    return True, f'Uploaded {count} file{"s" if count != 1 else ""}.'

def _wipe_dir(path):
    for item in os.listdir(path):
        p = os.path.join(path, item)
        try:
            if os.path.isdir(p) and not os.path.islink(p): shutil.rmtree(p)
            else: os.remove(p)
        except OSError: pass

@app.route('/api/site/status/<server_id>')
def api_site_status(server_id):
    s, owner, err = panel_guard(server_id)
    if err: return err
    return jsonify(dict(site_status(server_id), status='success', is_static=(s.get('type') == 'static')))

@app.route('/api/site/start/<server_id>', methods=['POST'])
def api_site_start(server_id):
    s, owner, err = panel_guard(server_id)
    if err: return err
    if s.get('type') != 'static':
        return jsonify({'status': 'error', 'msg': 'Only static website servers can publish a Cloudflare link.'}), 400
    if not rate_allow(('site', server_id), 12, 3600):
        return jsonify({'status': 'error', 'msg': 'Too many link requests. Please wait a few minutes.'}), 429
    try:
        start_site(server_id)
    except RuntimeError as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 503
    log_activity('site', owner, f"Link requested for static server {server_id}")
    return jsonify(dict(site_status(server_id), status='success'))

@app.route('/api/site/stop/<server_id>', methods=['POST'])
def api_site_stop(server_id):
    s, owner, err = panel_guard(server_id)
    if err: return err
    stop_site(server_id)
    log_activity('site', owner, f"Link stopped for static server {server_id}")
    return jsonify(dict(site_status(server_id), status='success'))

@app.route('/api/site/upload/<server_id>', methods=['POST'])
def api_site_upload(server_id):
    s, owner, err = panel_guard(server_id)
    if err: return err
    if s.get('type') != 'static':
        return jsonify({'status': 'error', 'msg': 'Only static website servers can use this upload.'}), 400
    files = request.files.getlist('files')
    paths = request.form.getlist('paths')
    if not files:
        return jsonify({'status': 'error', 'msg': 'No files received.'}), 400
    uploads = [((paths[i] if i < len(paths) and paths[i] else f.filename or ''), f) for i, f in enumerate(files)]
    ok, msg = deploy_site_files(server_id, uploads, replace=request.form.get('replace') == '1')
    if not ok:
        return jsonify({'status': 'error', 'msg': msg}), 400
    log_activity('site', owner, f"Website files uploaded to {server_id}")
    return jsonify({'status': 'success', 'msg': msg, 'info': site_info(server_id)})

@app.route('/api/site/starter/<server_id>', methods=['POST'])
def api_site_starter(server_id):
    s, owner, err = panel_guard(server_id)
    if err: return err
    create_static_files(get_server_dir(server_id))
    return jsonify({'status': 'success', 'info': site_info(server_id)})

# ---------- environment variables + backup (panel) ----------

ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
ENV_BLOCKED = {'PATH', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONHOME', 'PYTHONPATH', 'NODE_OPTIONS', 'DYLD_INSERT_LIBRARIES'}

@app.route('/api/env/<server_id>', methods=['GET', 'POST'])
def api_env(server_id):
    s, owner, err = panel_guard(server_id)
    if err: return err
    if request.method == 'GET':
        return jsonify({'status': 'success', 'env': s.get('env_vars') or {}})
    body = request.get_json(silent=True) or {}
    env = body.get('env')
    if not isinstance(env, dict) or len(env) > 50:
        return jsonify({'status': 'error', 'msg': 'You can save up to 50 variables.'}), 400
    clean = {}
    for k, v in env.items():
        k = str(k).strip()
        if not ENV_KEY_RE.match(k) or k.upper() in ENV_BLOCKED:
            return jsonify({'status': 'error', 'msg': f'"{k}" is not an allowed variable name.'}), 400
        v = str(v)
        if len(v) > 2000:
            return jsonify({'status': 'error', 'msg': f'Value of {k} is too long.'}), 400
        clean[k] = v
    update_server(server_id, lambda srv, u, d: srv.__setitem__('env_vars', clean))
    return jsonify({'status': 'success', 'count': len(clean)})

@app.route('/api/backup/<server_id>')
def api_backup(server_id):
    s, owner, err = panel_guard(server_id)
    if err: return err
    server_dir = os.path.abspath(get_server_dir(server_id))
    if dir_size(server_dir, 300 * 1024 * 1024) > 300 * 1024 * 1024:
        return jsonify({'status': 'error', 'msg': 'Backup is too large to download (max 300 MB).'}), 413
    tmp = tempfile.TemporaryFile()
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(server_dir):
            dirs[:] = [d for d in dirs if d not in ('node_modules', '__pycache__', '.git')]
            for name in files:
                if name == 'output.log': continue
                full = os.path.join(root, name)
                zf.write(full, os.path.relpath(full, server_dir))
    tmp.seek(0)
    log_activity('backup', owner, f"Backup downloaded for {server_id}")
    return send_file(tmp, mimetype='application/zip', as_attachment=True, download_name=f'{server_id}-backup.zip')

# ---------- admin API ----------

def system_stats():
    try: du = psutil.disk_usage(os.path.abspath(BOTS_DIR))
    except Exception: du = psutil.disk_usage(os.path.abspath('.'))
    vm, net = psutil.virtual_memory(), psutil.net_io_counters()
    try: load = list(os.getloadavg())
    except Exception: load = None
    return {'cpu': psutil.cpu_percent(interval=None), 'cpu_count': psutil.cpu_count(), 'ram_percent': vm.percent,
            'ram_used': vm.used, 'ram_total': vm.total, 'disk_percent': du.percent, 'disk_used': du.used, 'disk_total': du.total,
            'net_sent': net.bytes_sent, 'net_recv': net.bytes_recv, 'uptime': time.time() - psutil.boot_time(),
            'app_uptime': time.time() - APP_START, 'os': platform.platform(), 'python': sys.version.split()[0],
            'processes': len(psutil.pids()), 'load': load}
psutil.cpu_percent(interval=None)

def admin_server_view(s, owner):
    v = server_view(s)
    v.update({'owner': owner, 'suspended': bool(s.get('suspended')), 'note': s.get('note', ''), 'pid': s.get('pid'),
              'srv_user': s.get('srv_user', ''), 'srv_pass': s.get('srv_pass', ''), 'main_file': s.get('main_file', ''),
              'expiry_full': s.get('expiry', ''), 'env_count': len(s.get('env_vars') or {}),
              'site': (SITES.get(s.get('server_id')) or {}).get('state')})
    return v

@app.route('/admin/api/overview')
@admin_only
def admin_api_overview():
    users = load_users()
    counts = {'accounts': 0, 'legacy': 0, 'banned': 0, 'servers': 0, 'running': 0, 'stopped': 0, 'limited': 0, 'suspended': 0, 'expired': 0, 'provisioning': 0}
    types = {}
    for uname, data in users.items():
        if uname == 'admin' or not isinstance(data, dict): continue
        counts['accounts' if data.get('account') else 'legacy'] += 1
        if data.get('banned'): counts['banned'] += 1
        for s in (data.get('servers') if isinstance(data.get('servers'), list) else []):
            if not isinstance(s, dict): continue
            counts['servers'] += 1
            st = server_view(s)['state']
            counts[st if st in counts else 'stopped'] += 1
            types[s.get('type', 'python')] = types.get(s.get('type', 'python'), 0) + 1
    sites = [{'server_id': sid, 'owner': (get_server_by_id(sid)[1] or '?'), 'state': x['state'], 'url': x['url'], 'live_since': x.get('live_since'), 'started': x['started']}
             for sid, x in SITES.items()]
    return jsonify({'system': system_stats(), 'counts': counts, 'types': types, 'sites': sites,
                    'runtime': runtime_status(), 'default_admin_password': users.get('admin', {}).get('password') == 'admin123',
                    'settings': load_settings()})

@app.route('/admin/api/server/<server_id>')
@admin_only
def admin_api_server(server_id):
    s, owner = get_server_by_id(server_id)
    if not s: return jsonify({'error': 'Not found'}), 404
    server_dir = get_server_dir(server_id)
    tail = ''
    try:
        with open(os.path.join(server_dir, 'output.log'), 'rb') as f:
            f.seek(0, os.SEEK_END); size = f.tell(); f.seek(max(0, size - 60000))
            tail = f.read().decode('utf-8', 'replace')
    except Exception: pass
    view = admin_server_view(s, owner)
    view['disk_bytes'] = dir_size(server_dir)
    view['site_status'] = site_status(server_id) if s.get('type') == 'static' else None
    return jsonify({'server': view, 'logs': tail})

def _new_expiry(dt):
    return str(dt.replace(microsecond=dt.microsecond or 1))

@app.route('/admin/api/server/<server_id>/action', methods=['POST'])
@admin_only
def admin_api_server_action(server_id):
    body = request.get_json(silent=True) or {}
    action = body.get('action')
    s0, owner0 = get_server_by_id(server_id)
    if not s0: return jsonify({'error': 'Not found'}), 404
    def apply(s, owner, acc):
        if action == 'extend':
            days = int(body.get('days', 0))
            if not 1 <= days <= 3650: raise ValueError('Days must be between 1 and 3650')
            exp = parse_expiry(s.get('expiry', '')) or datetime.now()
            s['expiry'] = _new_expiry(max(exp, datetime.now()) + timedelta(days=days))
            return f'Extended by {days} day(s)'
        if action == 'set_expiry':
            try: d = datetime.strptime(str(body.get('date', '')), '%Y-%m-%d')
            except ValueError: raise ValueError('Use the date format YYYY-MM-DD')
            s['expiry'] = _new_expiry(d.replace(hour=23, minute=59, second=59))
            return f"Expiry set to {body.get('date')}"
        if action == 'suspend':
            s['suspended'] = True
            if s.get('pid'): stop_bot_process(s['pid'])
            s['status'], s['pid'], s['stopped_by_user'] = 'stopped', None, True
            stop_site(server_id)
            return 'Server suspended'
        if action == 'unsuspend':
            s.pop('suspended', None)
            return 'Server unsuspended'
        if action == 'regen_creds':
            users = load_users()
            new_user, new_pass = unique_server_credentials(users)
            custom_user = str(body.get('username') or '').strip()
            custom_pass = str(body.get('password') or '')
            if custom_user:
                if not re.match(r'^[A-Za-z0-9_]{3,30}$', custom_user): raise ValueError('Username: 3-30 letters, numbers or underscore')
                taken = {u.lower() for u in users.keys()} | {x.get('srv_user', '').lower() for d in users.values() if isinstance(d, dict) and isinstance(d.get('servers'), list) for x in d['servers'] if isinstance(x, dict) and x.get('server_id') != server_id}
                if custom_user.lower() in taken: raise ValueError('That username is already in use')
                new_user = custom_user
            if custom_pass:
                if len(custom_pass) < 6: raise ValueError('Password must be at least 6 characters')
                new_pass = custom_pass
            s['srv_user'], s['srv_pass'] = new_user, new_pass
            return 'Server login details updated'
        if action == 'set_specs':
            ram, disk, cpu = str(body.get('ram', s.get('ram'))), str(body.get('disk', s.get('disk'))), int(body.get('cpu_limit', s.get('cpu_limit', 80)))
            if not SIZE_RE.match(ram) or not SIZE_RE.match(disk): raise ValueError('RAM and disk must look like 512MB or 2GB')
            if not 5 <= cpu <= 100: raise ValueError('CPU limit must be between 5 and 100')
            s['ram'], s['disk'], s['cpu_limit'] = ram, disk, cpu
            if 'plan' in body: s['plan'] = str(body.get('plan'))[:30]
            return 'Resources updated'
        if action == 'note':
            s['note'] = str(body.get('text', ''))[:500]
            return 'Note saved'
        raise ValueError('Unknown action')
    try:
        msg = update_server(server_id, apply)
    except (ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400
    log_activity('admin', 'admin', f"{action} on {server_id} ({owner0})")
    s, owner = get_server_by_id(server_id)
    return jsonify({'success': True, 'msg': msg, 'server': admin_server_view(s, owner)})

@app.route('/admin/api/accounts')
@admin_only
def admin_api_accounts():
    out = []
    for uname, data in load_users().items():
        if uname == 'admin' or not isinstance(data, dict): continue
        servers = data.get('servers') if isinstance(data.get('servers'), list) else []
        out.append({'username': uname, 'kind': 'registered' if data.get('account') else 'admin-created',
                    'created': data.get('created', ''), 'free_claimed': int(data.get('free_claimed', 0)),
                    'banned': bool(data.get('banned')), 'servers': len(servers),
                    'running': sum(1 for x in servers if isinstance(x, dict) and x.get('status') == 'running')})
    out.sort(key=lambda a: a['created'], reverse=True)
    return jsonify({'accounts': out})

@app.route('/admin/api/account/<username>', methods=['POST'])
@admin_only
def admin_api_account_action(username):
    body = request.get_json(silent=True) or {}
    action = body.get('action')
    with ACCOUNT_LOCK:
        users = load_users()
        data = users.get(username)
        if username == 'admin' or not isinstance(data, dict): return jsonify({'error': 'Not found'}), 404
        result = {}
        if action == 'reset_password' or action == 'set_password':
            new_pw = str(body.get('password') or '') if action == 'set_password' else generate_random_password(10)
            if len(new_pw) < 6: return jsonify({'error': 'Password must be at least 6 characters'}), 400
            if data.get('account'): data['pw_hash'] = generate_password_hash(new_pw)
            else: data['password'] = new_pw
            result['password'] = new_pw
        elif action == 'ban':
            data['banned'] = True
            for s in (data.get('servers') if isinstance(data.get('servers'), list) else []):
                if isinstance(s, dict):
                    if s.get('pid'): stop_bot_process(s['pid'])
                    s['status'], s['pid'], s['stopped_by_user'] = 'stopped', None, True
                    stop_site(s.get('server_id'))
        elif action == 'unban':
            data.pop('banned', None)
        elif action == 'grant_free':
            data['free_claimed'] = max(0, int(data.get('free_claimed', 0)) - 1)
        elif action == 'delete':
            for s in (data.get('servers') if isinstance(data.get('servers'), list) else []):
                if isinstance(s, dict):
                    if s.get('pid'): stop_bot_process(s['pid'])
                    stop_site(s.get('server_id'))
                    try: shutil.rmtree(get_server_dir(s.get('server_id')))
                    except Exception: pass
            del users[username]
        else:
            return jsonify({'error': 'Unknown action'}), 400
        save_users(users)
    log_activity('admin', 'admin', f"{action} on account {username}")
    return jsonify(dict(success=True, **result))

@app.route('/admin/api/settings', methods=['GET', 'POST'])
@admin_only
def admin_api_settings():
    if request.method == 'GET':
        return jsonify(load_settings())
    b = request.get_json(silent=True) or {}
    s = load_settings()
    try:
        if 'registration_open' in b: s['registration_open'] = bool(b['registration_open'])
        if 'maintenance' in b: s['maintenance'] = bool(b['maintenance'])
        if isinstance(b.get('announcement'), dict):
            a = b['announcement']
            s['announcement'] = {'enabled': bool(a.get('enabled')), 'text': str(a.get('text', ''))[:300],
                                 'type': a.get('type') if a.get('type') in ('info', 'success', 'warning', 'danger') else 'info'}
        if isinstance(b.get('trial'), dict):
            t = b['trial']
            days, cpu = int(t.get('days', 2)), int(t.get('cpu_limit', 30))
            ram, disk = str(t.get('ram', '512MB')), str(t.get('disk', '1GB'))
            if not (1 <= days <= 365 and 5 <= cpu <= 100 and SIZE_RE.match(ram) and SIZE_RE.match(disk)): raise ValueError('Invalid trial settings')
            s['trial'] = {'days': days, 'ram': ram, 'disk': disk, 'cpu_limit': cpu}
        if 'free_per_account' in b:
            v = int(b['free_per_account'])
            if not 0 <= v <= 20: raise ValueError('Free servers per account must be 0-20')
            s['free_per_account'] = v
        if 'max_tunnels' in b:
            v = int(b['max_tunnels'])
            if not 1 <= v <= 500: raise ValueError('Max live links must be 1-500')
            s['max_tunnels'] = v
    except (ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400
    save_settings(s)
    log_activity('settings', 'admin', 'Settings updated')
    return jsonify({'success': True, 'settings': s})

@app.route('/admin/api/activity')
@admin_only
def admin_api_activity():
    items = []
    try:
        with open(ACTIVITY_FILE, 'r', encoding='utf-8') as f: items = json.load(f)
    except Exception: pass
    return jsonify({'items': list(reversed(items))[:int(request.args.get('limit', 200))]})

@app.route('/admin/api/runtime')
@admin_only
def admin_api_runtime():
    _TTL.pop('runtime', None); _TTL.pop('cf_ok', None)
    return jsonify(runtime_status())

@app.route('/admin/api/runtime/install', methods=['POST'])
@admin_only
def admin_api_runtime_install():
    b = request.get_json(silent=True) or {}
    name = b.get('name')
    if name not in ('node', 'cloudflared'): return jsonify({'error': 'Unknown runtime'}), 400
    start_runtime_install(name, force=bool(b.get('force')))
    log_activity('runtime', 'admin', f'Install requested: {name}')
    _TTL.pop('runtime', None)
    return jsonify({'success': True})

@app.route('/admin/api/tunnel/<server_id>/stop', methods=['POST'])
@admin_only
def admin_api_tunnel_stop(server_id):
    stopped = stop_site(server_id)
    log_activity('site', 'admin', f'Link stopped for {server_id}')
    return jsonify({'success': True, 'stopped': stopped})

@app.route('/admin/api/password', methods=['POST'])
@admin_only
def admin_api_password():
    b = request.get_json(silent=True) or {}
    with ACCOUNT_LOCK:
        users = load_users()
        if not safe_eq(users['admin'].get('password', ''), b.get('current', '')):
            return jsonify({'error': 'Current password is wrong'}), 400
        new = str(b.get('new', ''))
        if len(new) < 8: return jsonify({'error': 'New password must be at least 8 characters'}), 400
        users['admin']['password'] = new
        save_users(users)
    log_activity('admin', 'admin', 'Admin password changed')
    return jsonify({'success': True})

@app.route('/admin/backup')
@admin_only
def admin_backup():
    tmp = tempfile.TemporaryFile()
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name in (USERS_FILE, SETTINGS_FILE, ACTIVITY_FILE):
            if os.path.exists(name): zf.write(name, os.path.basename(name))
    tmp.seek(0)
    log_activity('admin', 'admin', 'Backup downloaded')
    return send_file(tmp, mimetype='application/zip', as_attachment=True, download_name='hosting-backup-%s.zip' % datetime.now().strftime('%Y%m%d-%H%M'))

# ============================================
# 🔥 DEPLOYMENT (Railway / Docker) - everything here is switched on by environment variables
# ============================================

def _apply_admin_password_env():
    """ADMIN_PASSWORD replaces the default admin123 password (only while it is still the default)."""
    pw = os.environ.get('ADMIN_PASSWORD', '')
    if len(pw) < 8: return
    try:
        with ACCOUNT_LOCK:
            users = load_users()
            if users.get('admin', {}).get('password') == 'admin123':
                users['admin']['password'] = pw
                save_users(users)
    except Exception:
        pass

def recover_servers_on_boot():
    """After a restart or redeploy the old process ids are gone: bring back the servers that were running."""
    time.sleep(5)
    try:
        users = load_users()
    except Exception:
        return
    jobs = []
    for uname, data in users.items():
        if uname == 'admin' or not isinstance(data, dict): continue
        servers = data.get('servers')
        if not isinstance(servers, list): continue
        for s in servers:
            if isinstance(s, dict) and s.get('status') == 'running' and s.get('server_id'):
                jobs.append(dict(s))
    for s in jobs:
        sid, pid = s['server_id'], s.get('pid')
        try:
            server_dir = os.path.abspath(get_server_dir(sid))
            alive = False
            if pid:
                try:
                    p = psutil.Process(pid)
                    alive = p.is_running() and p.status() != psutil.STATUS_ZOMBIE and os.path.abspath(p.cwd()).startswith(server_dir)
                except Exception:
                    alive = False
            if alive:
                threading.Thread(target=monitor_bot, args=(sid, pid), daemon=True).start()
                continue
            exp = parse_expiry(s.get('expiry', ''))
            eligible = not (s.get('suspended') or s.get('stopped_by_user') or s.get('type') == 'static' or (exp and datetime.now() > exp))
            new_pid = None
            if eligible:
                new_pid, _err = run_bot(sid, s.get('main_file', 'main.py'), s.get('requirements_file', 'requirements.txt'))
            def mark(srv, owner, acc, new_pid=new_pid):
                if new_pid:
                    srv['status'], srv['pid'], srv['started_at'] = 'running', new_pid, str(datetime.now())
                else:
                    srv['status'], srv['pid'] = 'stopped', None
            update_server(sid, mark)
            if new_pid:
                threading.Thread(target=monitor_bot, args=(sid, new_pid), daemon=True).start()
            log_activity('server', 'system', f"Server {sid} {'restarted after deploy' if new_pid else 'marked stopped after deploy'}")
            time.sleep(1)
        except Exception:
            pass

_apply_admin_password_env()
if os.environ.get('RECOVER_ON_BOOT') == '1':
    threading.Thread(target=recover_servers_on_boot, daemon=True).start()

# ============================================
# স্টার্ট
# ============================================

if __name__ == '__main__':
    print("\n" + "=" * 50)
    print("🚀 JUBAYER HOSTING - FINAL")
    print("=" * 50)
    print("📍 Landing: http://localhost:5000")
    print("📍 Admin: http://localhost:5000/login")
    print("🔗 API: http://localhost:5000/api/create")
    print("👤 admin / admin123")
    print("=" * 50 + "\n")
    try:
        # keep the auto-reloader away from user files and the downloaded runtimes
        app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), exclude_patterns=['*/runtime/*', '*/bots/*'])
    except TypeError:
        app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))