#!/usr/bin/env python3
"""维护 LX04 localhost:8477 到 Mac 状态服务的本机代理。"""
import argparse
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ADB_PORT = 5555
STATUS_PORT = 8477
INTERVAL = 10
PRODUCT = "mi_lx04"
URL = f"http://127.0.0.1:{STATUS_PORT}/"
STATE = Path.home() / "Library" / "Application Support" / "lx04-status" / "adb-endpoint.json"
PID = "/data/local/tmp/lx04-status-proxy.pid"
TARGET = "/data/local/tmp/lx04-status-proxy.target"
LOG = "/data/local/tmp/lx04-status-proxy.log"
TOYBOX = "/system/bin/toybox"
ADB = next((p for p in ("/opt/homebrew/bin/adb", shutil.which("adb")) if p and Path(p).exists()), None)


def run(*args, timeout=8):
    if not ADB:
        return None
    try:
        return subprocess.run([ADB, *args], text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def devices(text):
    return [line.split()[0] for line in text.splitlines()[1:]
            if len(line.split()) >= 2 and line.split()[1] == "device"]


def is_lx04(serial):
    result = run("-s", serial, "shell", "getprop", "ro.product.device", timeout=3)
    return bool(result and result.returncode == 0 and result.stdout.strip() == PRODUCT)


def connected_lx04():
    result = run("devices", "-l")
    if not result:
        return None
    for serial in devices(result.stdout):
        if is_lx04(serial):
            return serial
    return None


def saved_endpoint():
    try:
        endpoint = json.loads(STATE.read_text()).get("endpoint")
        host, port = endpoint.rsplit(":", 1)
        ipaddress.ip_address(host)
        return endpoint if port == str(ADB_PORT) else None
    except (OSError, ValueError, AttributeError, json.JSONDecodeError):
        return None


def connect(endpoint):
    result = run("connect", endpoint, timeout=5)
    return endpoint if result and is_lx04(endpoint) else None


def mac_ip():
    try:
        configured = json.loads(STATE.read_text()).get("mac_ip")
        address = ipaddress.ip_address(configured) if configured else None
        if address and address.version == 4 and address.is_private:
            return str(address)
    except (OSError, ValueError, AttributeError, json.JSONDecodeError):
        pass
    try:
        result = subprocess.run(["ipconfig", "getifaddr", "en0"], text=True,
                                capture_output=True, timeout=3)
        host = result.stdout.strip()
        return host if result.returncode == 0 and ipaddress.ip_address(host).is_private else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def local_network():
    host = mac_ip()
    return ipaddress.ip_network(f"{host}/24", strict=False) if host else None


def port_open(host):
    try:
        with socket.create_connection((host, ADB_PORT), timeout=0.25):
            return host
    except OSError:
        return None


def scan_lx04():
    network = local_network()
    if not network:
        return None
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(port_open, str(host)) for host in network.hosts()]
        for future in as_completed(futures):
            host = future.result()
            if host:
                endpoint = f"{host}:{ADB_PORT}"
                if connect(endpoint):
                    return endpoint
    return None


def save_endpoint(endpoint):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        config = json.loads(STATE.read_text())
        if not isinstance(config, dict):
            config = {}
    except (OSError, ValueError):
        config = {}
    config["endpoint"] = endpoint
    STATE.write_text(json.dumps(config) + "\n")
    os.chmod(STATE, 0o600)


def proxy_ready(serial, target):
    listening = run("-s", serial, "shell", "ss", "-ltn")
    configured = run("-s", serial, "shell", "cat", TARGET)
    return bool(listening and configured and f"127.0.0.1:{STATUS_PORT}" in listening.stdout
                and configured.stdout.strip() == target)


def install_proxy(serial, target):
    command = (
        f"if [ -f {PID} ]; then kill $(cat {PID}) 2>/dev/null; fi; "
        f"nohup {TOYBOX} nc -s 127.0.0.1 -p {STATUS_PORT} -L "
        f"{TOYBOX} nc {target} {STATUS_PORT} >{LOG} 2>&1 & "
        f"echo $! >{PID}; echo {target} >{TARGET}"
    )
    result = run("-s", serial, "shell", command)
    return bool(result and result.returncode == 0)


def server_alive():
    try:
        with socket.create_connection(("127.0.0.1", STATUS_PORT), timeout=1):
            return True
    except OSError:
        return False


def open_status(serial):
    result = run("-s", serial, "shell", "am", "start", "-a", "android.intent.action.VIEW",
                 "-d", URL, "-p", "mark.via.gp")
    return bool(result and result.returncode == 0)


def reload_status(serial):
    result = run("-s", serial, "shell", "input", "keyevent", "135")
    return bool(result and result.returncode == 0)


def ensure(reload=False):
    serial = connected_lx04()
    recovered = False
    if not serial:
        endpoint = saved_endpoint()
        serial = connect(endpoint) if endpoint else None
        recovered = bool(serial)
    if not serial:
        serial = scan_lx04()
        recovered = bool(serial)
    if not serial:
        return None

    target = mac_ip()
    if not target:
        return None
    if not proxy_ready(serial, target):
        if not install_proxy(serial, target):
            return None
        recovered = True

    if recovered:
        save_endpoint(serial)
        print(f"[adb-bridge] 已连接 LX04：{serial}，目标 Mac：{target}", flush=True)
        if reload and reload_status(serial):
            print("[adb-bridge] 已刷新当前状态页", flush=True)
    return serial


def self_test():
    assert devices("List of devices attached\n1.2.3.4:5555 device\nfoo offline\n") == ["1.2.3.4:5555"]
    assert str(ipaddress.ip_network("192.168.1.42/24", strict=False)) == "192.168.1.0/24"
    print("self_test=ok")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="只检查和修复一次")
    parser.add_argument("--open", action="store_true", help="检查完成后打开状态屏")
    parser.add_argument("--self-test", action="store_true", help="运行不连接设备的自检")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if not ADB:
        print("[adb-bridge] 找不到 adb", file=sys.stderr)
        return 1

    while True:
        serial = ensure(reload=not args.open)
        if args.open and serial and server_alive() and open_status(serial):
            print(f"[adb-bridge] 已打开 {URL}", flush=True)
            return 0
        if args.once:
            return 0 if serial else 1
        time.sleep(INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
