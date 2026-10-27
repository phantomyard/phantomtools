"""Fixed-operation local service controller; Linux/systemd, standard library only."""

import argparse
import json
import os
import pwd
import re
import socket
import stat
import struct
import subprocess  # nosec B404
import sys
import time
from dataclasses import dataclass

MAX_CONFIG = 16384
REQUEST = b"ENSURE\n"
SOCKET_DIRECTORY = "/run/persona-service-control"
PROGRAM = "/usr/local/libexec/persona_service_control.py"


class Rejected(ValueError):
    """Invalid binding or untrusted filesystem metadata."""


@dataclass(frozen=True)
class Binding:
    id: str
    account: str
    uid: int
    unit: str
    gid: int

    @property
    def socket_path(self):
        return f"{SOCKET_DIRECTORY}/{self.id}.sock"


def parse_binding(data, lookup=pwd.getpwnam):
    if not isinstance(data, dict) or set(data) != {
        "version",
        "id",
        "account",
        "uid",
        "unit",
    }:
        raise Rejected("Unexpected binding fields")
    if type(data["version"]) is not int or data["version"] != 1:
        raise Rejected("Unsupported binding version")
    for key, pattern in (
        ("id", r"[a-z][a-z0-9-]{0,47}"),
        ("account", r"[a-z_][a-z0-9_-]{0,31}"),
        ("unit", r"[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}\.service"),
    ):
        if not isinstance(data[key], str) or not re.fullmatch(pattern, data[key]):
            raise Rejected("Invalid binding identifier")
    if data["unit"].endswith("@.service"):
        raise Rejected("Uninstantiated service template")
    if type(data["uid"]) is not int or not 0 < data["uid"] < 2**32 - 1:
        raise Rejected("Invalid contained UID")
    try:
        account = lookup(data["account"])
    except KeyError as error:
        raise Rejected("Unknown account") from error
    if account.pw_uid != data["uid"]:
        raise Rejected("Account UID drift")
    return Binding(
        data["id"], data["account"], data["uid"], data["unit"], account.pw_gid
    )


def _parts(path):
    # Restrict trusted command-line paths to unambiguous absolute ASCII paths.
    if not isinstance(path, str) or not re.fullmatch(
        r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", path
    ):
        raise Rejected("Invalid absolute path")
    parts = path.split("/")[1:]
    if any(part in (".", "..") for part in parts):
        raise Rejected("Relative path component")
    return parts


def _trusted_directory(path):
    parts = _parts(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        for part in [None, *parts]:
            if part is not None:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            info = os.fstat(descriptor)
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                raise Rejected("Untrusted directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Rejected("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise Rejected("Non-finite JSON constant")


def load_binding(path):
    parts = _parts(path)
    if len(parts) < 2 or not parts[-1].endswith(".json"):
        raise Rejected("Invalid configuration path")
    parent = _trusted_directory("/" + "/".join(parts[:-1]))
    try:
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent,
        )
    finally:
        os.close(parent)
    try:
        before = os.fstat(descriptor)
        if not (
            stat.S_ISREG(before.st_mode)
            and before.st_uid == 0
            and stat.S_IMODE(before.st_mode)
            in (0o400, 0o440, 0o444, 0o600, 0o640, 0o644)
            and 0 < before.st_size <= MAX_CONFIG
        ):
            raise Rejected("Untrusted configuration file")
        raw = bytearray()
        while len(raw) <= MAX_CONFIG:
            chunk = os.read(descriptor, min(4096, MAX_CONFIG + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if len(raw) > MAX_CONFIG or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise Rejected("Configuration changed or exceeds size bound")
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
        return parse_binding(data)
    finally:
        os.close(descriptor)


def start_service(binding):
    try:
        # Validated root binding; no shell or request arguments.
        result = subprocess.run(  # nosec B603
            ("/usr/bin/systemctl", "--no-ask-password", "start", binding.unit),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=25,
            check=False,
            close_fds=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": "/"},
            cwd="/",
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _receive(connection, limit, seconds):
    deadline = time.monotonic() + seconds
    data = bytearray()
    while len(data) <= limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Protocol deadline")
        connection.settimeout(remaining)
        chunk = connection.recv(limit + 1 - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def peer_uid(connection):
    if (
        connection.family != socket.AF_UNIX
        or connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
        != socket.SOCK_STREAM
    ):
        raise Rejected("Expected UNIX stream")
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    return struct.unpack("3i", credentials)[1]


def handle_connection(connection, binding, start=start_service):
    try:
        if peer_uid(connection) not in (0, binding.uid):
            return False
        if _receive(connection, len(REQUEST), 2) != REQUEST:
            connection.sendall(b"DENIED\n")
            return False
        started = start(binding)
        connection.sendall(b"STARTED\n" if started else b"ERROR\n")
        return started
    except (OSError, ValueError, struct.error):
        return False


def request_start(connection):
    connection.settimeout(2)
    connection.sendall(REQUEST)
    connection.shutdown(socket.SHUT_WR)
    return _receive(connection, 8, 30) == b"STARTED\n"


def client(binding):
    if os.geteuid() not in (0, binding.uid):
        raise Rejected("Wrong client account")
    directory = _trusted_directory(SOCKET_DIRECTORY)
    try:
        endpoint = os.stat(
            f"{binding.id}.sock", dir_fd=directory, follow_symlinks=False
        )
        if not (
            stat.S_ISSOCK(endpoint.st_mode)
            and endpoint.st_uid == 0
            and endpoint.st_gid == binding.gid
            and stat.S_IMODE(endpoint.st_mode) == 0o660
        ):
            raise Rejected("Untrusted socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(binding.socket_path)
            if peer_uid(connection) != 0:
                raise Rejected("Untrusted controller peer")
            return request_start(connection)
    finally:
        os.close(directory)


def render_units(binding):
    name = f"persona-service-start-{binding.id}"
    configuration = f"/etc/persona-service-control/{binding.id}.json"
    return {
        f"{name}.socket": f"""[Unit]
Description=Fixed service-start control socket for {binding.id}

[Socket]
ListenStream={binding.socket_path}
SocketUser=root
SocketGroup={binding.gid}
SocketMode=0660
DirectoryMode=0755
Accept=yes
MaxConnections=4
MaxConnectionsPerSource=2
RemoveOnStop=yes

[Install]
WantedBy=sockets.target
""",
        f"{name}@.service": f"""[Unit]
Description=Fixed service-start request handler for {binding.id}
CollectMode=inactive-or-failed

[Service]
Type=exec
User=root
Group=root
ExecStart=/usr/bin/python3 -I {PROGRAM} broker --config {configuration}
StandardInput=socket
StandardOutput=null
StandardError=journal
RuntimeMaxSec=35
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
PrivateNetwork=yes
RestrictAddressFamilies=AF_UNIX
RestrictNamespaces=yes
RestrictSUIDSGID=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
LockPersonality=yes
UMask=0077
""",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("validate", "render", "broker", "client"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "broker" and os.geteuid() != 0:
            raise Rejected("Root handler required")
        binding = load_binding(args.config)
        if args.operation == "broker":
            with socket.socket(fileno=os.dup(0)) as connection:
                return 0 if handle_connection(connection, binding) else 1
        if args.operation == "client":
            return 0 if client(binding) else 1
        if args.operation == "render":
            if args.config != f"/etc/persona-service-control/{binding.id}.json":
                raise Rejected("Render requires the canonical configuration path")
            print(
                json.dumps(
                    {
                        "deployable": False,
                        "enforcement_applied": False,
                        "files": render_units(binding),
                    },
                    indent=2,
                )
            )
        else:
            print(
                json.dumps(
                    {"valid": True, "binding": binding.id, "enforcement_applied": False}
                )
            )
        return 0
    except (OSError, ValueError, KeyError):
        print("Service control request rejected.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
