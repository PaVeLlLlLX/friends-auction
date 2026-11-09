#!/usr/bin/env python3
"""Run a time-limited, non-demo Telegram trial from this Mac."""
import json
from collections import deque
import http.client
import os
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from auction.telegram import call, TelegramError
from auction.i18n import translate

CONFIG = ROOT / 'data' / 'testing-config.json'
STATUS = ROOT / 'data' / 'testing-status.json'
PORT = 8090


def free_port(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', port))
        except OSError:
            return False
        return True


def read_url(process, messages, diagnostics):
    for line in iter(process.stderr.readline, ''):
        if ' ERR ' in line or 'Registered tunnel connection' in line:
            diagnostics.append(line.strip())
        match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
        if match:
            messages.put(match.group(0))


def stop(processes):
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    for process in reversed(processes):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def verify_public_url(url):
    """Verify the endpoint with TLS, without caching an unready name in local DNS."""
    host = urllib.parse.urlsplit(url).hostname
    if not host or not re.fullmatch(r'[a-z0-9-]+\.trycloudflare\.com', host):
        return False
    query = 'https://cloudflare-dns.com/dns-query?' + urllib.parse.urlencode({'name':host,'type':'A'})
    request = urllib.request.Request(query, headers={'Accept':'application/dns-json'})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            dns = json.load(response)
        addresses = [answer['data'] for answer in dns.get('Answer', []) if answer.get('type') == 1]
        for address in addresses:
            context = ssl.create_default_context()
            connection = http.client.HTTPSConnection(host, timeout=6, context=context)
            try:
                raw = socket.create_connection((address, 443), timeout=6)
                connection.sock = context.wrap_socket(raw, server_hostname=host)
                connection.request('GET', '/api/lots', headers={'Host':host})
                response = connection.getresponse()
                if response.status == 200 and isinstance(json.load(response).get('lots'), list):
                    return True
            except (OSError, ValueError, http.client.HTTPException):
                pass
            finally:
                connection.close()
    except (OSError, urllib.error.URLError, ValueError, KeyError):
        # If DNS-over-HTTPS itself is unavailable, use the system resolver.
        try:
            with urllib.request.urlopen(url + '/api/lots', timeout=6) as response:
                return response.status == 200 and isinstance(json.load(response).get('lots'), list)
        except (OSError, urllib.error.URLError, ValueError):
            return False
    return False


def main():
    os.umask(0o077)
    if not CONFIG.is_file():
        print(translate('launcher_main_01'))
        return 1
    tunnel_bin = ROOT / '.tools' / 'cloudflared'
    server_bin = ROOT / '.venv' / 'bin' / 'gunicorn'
    if not tunnel_bin.is_file() or not server_bin.is_file():
        print(translate('launcher_main_02'))
        return 1
    try:
        with CONFIG.open(encoding='utf-8') as file:
            config = json.load(file)
        token, admin_id, username = config['token'], config['admin_id'], config['username']
        if type(admin_id) is not int or admin_id < 1 or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        print(translate('launcher_main_03'))
        return 1
    if not free_port(PORT):
        print(translate('launcher_main_04', PORT))
        return 1
    STATUS.unlink(missing_ok=True)
    env = os.environ.copy()
    env.update({'AUCTION_DEMO':'0', 'AUCTION_TRIAL':'1', 'TELEGRAM_BOT_TOKEN':token,
                'TELEGRAM_ADMIN_ID':str(admin_id), 'AUCTION_DB':str(ROOT/'data'/'testing.sqlite3'),
                'AUCTION_UPLOADS':str(ROOT/'data'/'testing-photos')})
    processes = []
    status_written = False
    try:
        print(translate('launcher_main_05'), flush=True)
        server = subprocess.Popen([str(server_bin),'--bind',f'127.0.0.1:{PORT}','--workers','1','--threads','4','run:app'],
                                  cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(server)
        local_url = f'http://127.0.0.1:{PORT}/api/lots'
        ready = False
        for _ in range(40):
            if server.poll() is not None:
                raise RuntimeError(translate('launcher_main_06'))
            try:
                with urllib.request.urlopen(local_url, timeout=1) as response:
                    ready = response.status == 200
                if ready:
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(.25)
        if not ready:
            raise RuntimeError(translate('launcher_main_07'))
        print(translate('launcher_main_08'), flush=True)
        tunnel = subprocess.Popen([str(tunnel_bin),'tunnel','--no-autoupdate','--protocol','http2','--url',f'http://127.0.0.1:{PORT}'],
                                  cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)
        processes.append(tunnel)
        messages = queue.Queue()
        diagnostics = deque(maxlen=8)
        threading.Thread(target=read_url,args=(tunnel,messages,diagnostics),daemon=True).start()
        try:
            url = messages.get(timeout=60)
        except queue.Empty:
            raise RuntimeError(translate('launcher_main_09'))
        print(translate('launcher_main_10', url), flush=True)
        reached = False
        for _ in range(15):
            reached = verify_public_url(url)
            if reached:
                break
            time.sleep(2)
        if not reached:
            if diagnostics:
                print(translate('launcher_main_11'), '\n'.join(diagnostics), flush=True)
            raise RuntimeError(translate('launcher_main_12'))
        print(translate('launcher_main_13'), flush=True)
        try:
            call(token, 'setChatMenuButton', {'menu_button':{'type':'web_app','text':translate('launcher_main_14'),'web_app':{'url':url}}})
        except TelegramError:
            raise RuntimeError(translate('launcher_main_15')) from None
        print(translate('launcher_main_16'), flush=True)
        worker_env = dict(env, AUCTION_BOT_POLL='1', AUCTION_APP_URL=url)
        worker = subprocess.Popen([str(ROOT/'.venv'/'bin'/'python'),'-m','auction.worker'], cwd=ROOT, env=worker_env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(worker)
        time.sleep(.5)
        if worker.poll() is not None:
            raise RuntimeError(translate('launcher_main_17'))
        fd = os.open(STATUS, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as status:
            json.dump({'pid':os.getpid(), 'bot_username':username, 'url':url}, status)
        status_written = True
        print(translate('launcher_main_18'))
        print(translate('launcher_main_19', username))
        print(translate('launcher_main_20'))
        print(translate('launcher_main_21'))
        print(translate('launcher_main_22'))
        print(translate('launcher_main_23'))
        print(translate('launcher_main_24'), flush=True)
        while True:
            failed = next((p for p in processes if p.poll() is not None), None)
            if failed:
                raise RuntimeError(translate('launcher_main_25'))
            time.sleep(2)
    except (RuntimeError, OSError) as error:
        print('\n' + str(error))
        return 1
    except KeyboardInterrupt:
        print(translate('launcher_main_26'))
        return 0
    finally:
        if status_written:
            STATUS.unlink(missing_ok=True)
        stop(processes)


if __name__ == '__main__':
    raise SystemExit(main())
