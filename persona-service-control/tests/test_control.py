"""No service is started: PID 1 is replaced by a fake backend."""

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from support import example_data, example_lookup, load_control


@unittest.skipUnless(sys.platform == "linux", "Linux peer credentials required")
class TestControl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = load_control()

    def binding(self):
        return self.control.parse_binding(example_data(), example_lookup)

    def connection(self, uid=12345, chunks=(b"ENSURE\n", b"")):
        connection = Mock(family=socket.AF_UNIX)
        connection.getsockopt.side_effect = [
            socket.SOCK_STREAM,
            struct.pack("3i", 99, uid, uid),
        ]
        connection.recv.side_effect = chunks
        return connection

    def test_bindings_reject_unknown_fields_uid_drift_and_command_injection(self):
        changes = [
            {"version": True},
            {"version": 2},
            {"uid": 0},
            {"uid": True},
            {"uid": 12346},
            {"uid": -1},
            {"uid": 2**32},
            {"account": "root"},
            {"id": "../other"},
            {"id": "other\nUser=root"},
            {"unit": "--user"},
            {"unit": "a.service;id"},
            {"unit": "a.socket"},
            {"unit": "a@.service"},
            {"unit": "a.service\nExecStart=id"},
            {"port": 18802},
            {"command": ["id"]},
            {"socket_path": "/tmp/other"},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(self.control.Rejected):
                self.control.parse_binding({**example_data(), **change}, example_lookup)
        with self.assertRaises(self.control.Rejected):
            self.control.parse_binding([], example_lookup)
        self.assertEqual(
            self.binding().socket_path,
            "/run/persona-service-control/example-browser.sock",
        )

    def test_json_duplicate_and_nonfinite_values_are_rejected(self):
        for raw in ('{"version":1,"version":1}', '{"uid":NaN}'):
            with self.assertRaises(self.control.Rejected):
                json.loads(
                    raw,
                    object_pairs_hook=self.control._unique_object,
                    parse_constant=self.control._invalid_constant,
                )

    def test_cross_identity_peer_denied_before_reading_request(self):
        connection = self.connection(uid=12346)
        start = Mock()
        self.assertFalse(
            self.control.handle_connection(connection, self.binding(), start)
        )
        connection.recv.assert_not_called()
        start.assert_not_called()
        for uid in (0, 12345):
            self.assertTrue(
                self.control.handle_connection(
                    self.connection(uid=uid), self.binding(), lambda binding: True
                )
            )

    def test_exact_framed_operation_and_failures(self):
        start = Mock(return_value=True)
        connection = self.connection(chunks=(b"EN", b"SU", b"RE\n", b""))
        self.assertTrue(
            self.control.handle_connection(connection, self.binding(), start)
        )
        start.assert_called_once_with(self.binding())
        connection.sendall.assert_called_once_with(b"STARTED\n")
        for data in (
            b"",
            b"ENSURE",
            b"ENSURE\nX",
            b"ENSURE\nENSURE\n",
            b"chrome-other.service\n",
        ):
            connection = self.connection()
            remaining = bytearray(data)

            def receive(limit, remaining=remaining):
                chunk = bytes(remaining[:limit])
                del remaining[:limit]
                return chunk

            connection.recv.side_effect = receive
            start = Mock()
            self.assertFalse(
                self.control.handle_connection(connection, self.binding(), start)
            )
            start.assert_not_called()
        connection = self.connection()
        self.assertFalse(
            self.control.handle_connection(
                connection, self.binding(), lambda binding: False
            )
        )
        connection.sendall.assert_called_once_with(b"ERROR\n")

    def test_absolute_deadline_does_not_reset_for_fragments(self):
        start = Mock()
        with patch.object(self.control.time, "monotonic", side_effect=[0, 1, 3]):
            self.assertFalse(
                self.control.handle_connection(
                    self.connection(chunks=(b"EN",)), self.binding(), start
                )
            )
        start.assert_not_called()

    def test_fixed_subprocess_boundary_and_timeout(self):
        with patch.object(self.control.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertTrue(self.control.start_service(self.binding()))
            args, kwargs = run.call_args
            self.assertEqual(
                args,
                (
                    (
                        "/usr/bin/systemctl",
                        "--no-ask-password",
                        "start",
                        "chrome-example.service",
                    ),
                ),
            )
            self.assertNotIn("shell", kwargs)
            self.assertEqual(
                kwargs["env"], {"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": "/"}
            )
            self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(kwargs["timeout"], 25)
            self.assertTrue(kwargs["close_fds"])
            for failure in (OSError(), subprocess.TimeoutExpired("fixed", 25)):
                run.side_effect = failure
                self.assertFalse(self.control.start_service(self.binding()))

    def test_nonunix_connections_and_nonowner_clients_fail_closed(self):
        connection = Mock(family=socket.AF_INET)
        self.assertFalse(
            self.control.handle_connection(connection, self.binding(), Mock())
        )
        with (
            patch.object(self.control.os, "geteuid", return_value=12346),
            patch.object(self.control, "_trusted_directory") as directory,
        ):
            with self.assertRaises(self.control.Rejected):
                self.control.client(self.binding())
            directory.assert_not_called()

    def test_client_requires_full_response_and_does_not_grant_query_access(self):
        for chunks, expected in (
            ([b"STARTED\n", b""], True),
            ([b"ERROR\n", b""], False),
            ([b"STARTED", b""], False),
            ([b"STARTED\nX"], False),
        ):
            connection = Mock()
            connection.recv.side_effect = chunks
            self.assertEqual(self.control.request_start(connection), expected)
            connection.sendall.assert_called_once_with(b"ENSURE\n")
            connection.shutdown.assert_called_once_with(socket.SHUT_WR)

    def test_render_is_only_a_plan_and_contains_no_persona_specific_code(self):
        units = self.control.render_units(self.binding())
        self.assertEqual(
            set(units),
            {
                "persona-service-start-example-browser.socket",
                "persona-service-start-example-browser@.service",
            },
        )
        text = "\n".join(units.values())
        self.assertIn("SocketGroup=23456", text)
        self.assertIn("NoNewPrivileges=yes", text)
        self.assertIn("CapabilityBoundingSet=\n", text)
        self.assertIn(
            "broker --config /etc/persona-service-control/example-browser.json", text
        )
        for private_name in ("alma", "aquaponics", "18802", "chrome-example.service"):
            self.assertNotIn(private_name, text)

    def test_real_unix_exchange_under_no_new_privileges(self):
        module_path = str(Path(self.control.__file__).resolve())
        results = []
        errors = []
        binding = self.control.Binding(
            "example", "unused", os.getuid() or 12345, "example.service", os.getgid()
        )
        with tempfile.TemporaryDirectory() as directory:
            address = str(Path(directory) / "start.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(address)
                listener.listen(1)
                listener.settimeout(10)

                def serve():
                    try:
                        with listener.accept()[0] as connection:
                            results.append(
                                self.control.handle_connection(
                                    connection, binding, lambda binding: True
                                )
                            )
                    except (OSError, ValueError) as error:
                        errors.append(error)

                thread = threading.Thread(target=serve)
                thread.start()
                code = """
import ctypes, importlib.util, socket, sys
libc = ctypes.CDLL(None, use_errno=True)
assert libc.prctl(38, 1, 0, 0, 0) == 0
assert libc.prctl(39, 0, 0, 0, 0) == 1
spec = importlib.util.spec_from_file_location('control', sys.argv[1])
control = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = control
spec.loader.exec_module(control)
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
    connection.connect(sys.argv[2])
    assert control.request_start(connection)
"""
                try:
                    result = subprocess.run(
                        [sys.executable, "-I", "-c", code, module_path, address],
                        capture_output=True,
                        timeout=12,
                        check=False,
                    )
                finally:
                    thread.join(timeout=12)
                self.assertFalse(thread.is_alive())
                self.assertFalse(errors)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(results, [True])


if __name__ == "__main__":
    unittest.main()
