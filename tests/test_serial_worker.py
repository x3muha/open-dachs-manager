import tempfile
import threading
import time
import unittest
from pathlib import Path

from open_dachs_manager.mapping import PackRepository, WriteAllowlist
from open_dachs_manager.serial_worker import SerialWorkerServer, SerialWorkerSession
from open_dachs_manager.service import DachsService
from open_dachs_manager.transport import BlockResult, Frame, Response


class FakePhysicalSession:
    def __init__(self):
        self.is_open = False
        self.packet = 0
        self.blocks = {(0, 20): bytes(70), (0, 50): bytes(70), (1, 16): bytes(17), (2, 16): bytes(17)}
        self.operations = []

    def __enter__(self):
        self.is_open = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.is_open = False

    def next_packet(self):
        value = self.packet
        self.packet = (self.packet + 1) & 0x0F
        self.operations.append(("next_packet", value))
        return value

    @staticmethod
    def _response(packet, payload=b""):
        ack = Frame("ack", packet, b"", positive=True)
        data = Frame("data", packet, b"", payload=payload) if payload else None
        return Response(b"", ack, data, 1.0)

    def request(self, payload, packet, timeout=0.9, cpu=0):
        self.operations.append(("request", bytes(payload), packet, int(cpu)))
        return self._response(packet, b"\xFE\x05")

    def sync(self, packet=0, timeout=0.9, cpu=0):
        self.operations.append(("sync", packet, int(cpu)))
        return self._response(packet)

    def read_block(self, block, packet=None, timeout=0.9, cpu=0):
        packet = self.next_packet() if packet is None else int(packet)
        target = (int(cpu), int(block))
        payload = self.blocks.setdefault(target, bytes(70))
        self.operations.append(("read", int(cpu), int(block)))
        response = self._response(packet, b"\x00" + payload)
        return BlockResult(int(block), packet, response, 0, payload, int(cpu))

    def write_block(self, block, payload, packet=None, timeout=0.9, cpu=0):
        packet = self.next_packet() if packet is None else int(packet)
        self.blocks[(int(cpu), int(block))] = bytes(payload)
        self.operations.append(("write", int(cpu), int(block), bytes(payload)))
        return self._response(packet)


class SerialWorkerTests(unittest.TestCase):
    def _start(self, directory):
        physical = FakePhysicalSession()
        socket_path = Path(directory) / "serial.sock"
        server = SerialWorkerServer(
            socket_path,
            "/dev/fake",
            client_idle_timeout=2.0,
            session_factory=lambda: physical,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.assertTrue(server.ready_event.wait(2.0))
        return physical, socket_path, server, thread

    def _stop(self, server, thread):
        server.stop()
        thread.join(2.0)
        self.assertFalse(thread.is_alive())

    def test_worker_roundtrips_blocks_and_keeps_write_readback_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            physical, socket_path, server, thread = self._start(directory)
            try:
                pack = PackRepository()
                service = DachsService(
                    "/dev/fake", 19200, 0.2, pack,
                    serial_socket=socket_path, queue_timeout=2.0,
                )
                with service.session() as session:
                    packet = session.next_packet()
                    auth_response = session.request(b"\x7E1234\x04", packet, timeout=0.2)
                    self.assertEqual(auth_response.data.payload, b"\xFE\x05")
                    before_result = service.read_block(session, 50)
                    before = bytes(before_result.payload)
                    after = bytearray(before)
                    pack.encode_value(after, "Hka_Ew.usSollGenerator", "5.2", block=50)
                    audit = service.write_payload(
                        session, 50, before, bytes(after),
                        ["Hka_Ew.usSollGenerator"], WriteAllowlist(), dry_run=False,
                    )
                self.assertTrue(audit.written)
                self.assertTrue(audit.readback_ok)
                self.assertEqual(physical.blocks[(0, 50)][8:10], (5200).to_bytes(2, "little"))
                operation_names = [
                    item[0] for item in physical.operations if item[0] in {"read", "write"}
                ]
                self.assertIn(["read", "write", "read"], [
                    operation_names[index:index + 3]
                    for index in range(len(operation_names) - 2)
                ])

                with service.session() as session:
                    network = service.read_block(session, 16, cpu=2)
                self.assertEqual(network.cpu, 2)
                self.assertIn(("read", 2, 16), physical.operations)
            finally:
                self._stop(server, thread)

    def test_worker_refuses_to_replace_a_non_socket_path(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "serial.sock"
            socket_path.write_text("keep", encoding="utf-8")
            server = SerialWorkerServer(
                socket_path, "/dev/fake", session_factory=FakePhysicalSession
            )
            with self.assertRaisesRegex(RuntimeError, "non-socket"):
                server.serve_forever()
            self.assertEqual(socket_path.read_text(encoding="utf-8"), "keep")

    def test_connections_are_fifo_leases_not_interleaved_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            _physical, socket_path, server, thread = self._start(directory)
            first = SerialWorkerSession(socket_path, queue_timeout=2.0)
            first.__enter__()
            try:
                self.assertEqual(first.ping()["protocol"], 1)
                connected = threading.Event()
                completed = threading.Event()
                result = {}

                def second_client():
                    with SerialWorkerSession(socket_path, queue_timeout=2.0) as second:
                        connected.set()
                        result["block"] = second.read_block(20).block
                    completed.set()

                second_thread = threading.Thread(target=second_client)
                second_thread.start()
                self.assertTrue(connected.wait(1.0))
                deadline = time.monotonic() + 1.0
                while server.jobs.qsize() < 1 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(server.jobs.qsize(), 1)
                self.assertFalse(completed.is_set())
            finally:
                first.__exit__(None, None, None)
            self.assertTrue(completed.wait(2.0))
            second_thread.join(2.0)
            self.assertEqual(result["block"], 20)
            self._stop(server, thread)


if __name__ == "__main__":
    unittest.main()
