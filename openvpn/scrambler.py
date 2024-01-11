#!/bin/python3

from typing import Any
from typing import List
from typing import Optional

import signal
import socket as sock
import sys
import threading as th
import traceback as tb


def noerror(func: Any, *args: Any) -> None:
    try:
        func(*args)
    except Exception:
        pass


class Finalizer:
    def __init__(self) -> None:
        self.items: List[sock.socket] = list()
        self.lock = th.Lock()

    def put(self, s: sock.socket) -> None:
        with self.lock:
            if s not in self.items:
                self.items.append(s)

    def remove(self, s: sock.socket) -> None:
        with self.lock:
            self.items = [i for i in self.items if i != s]

    def fire_all(self) -> None:
        with self.lock:
            for i in self.items:
                noerror(i.shutdown, sock.SHUT_RDWR)
                noerror(i.close)


def report(err: Exception, fmt: str, *args: Any) -> None:
    print("Error: %s" % err, file=sys.stderr)
    tb.print_exc()
    if args:
        print(fmt % args, file=sys.stderr)
    else:
        print(fmt, file=sys.stderr)
    sys.stderr.flush()


class PortAddress:
    def __init__(self, addr: str, port: int) -> None:
        self.addr = addr
        self.port = port

    def open_client(self) -> sock.socket:
        ret = sock.socket(sock.AF_INET, sock.SOCK_STREAM)
        ret.connect((self.addr, self.port))
        return ret

    def open_server(self) -> sock.socket:
        ret = sock.socket(sock.AF_INET, sock.SOCK_STREAM)
        ret.setsockopt(sock.SOL_SOCKET, sock.SO_REUSEADDR, 1)
        ret.setsockopt(sock.SOL_SOCKET, sock.SO_REUSEPORT, 1)
        ret.bind((self.addr, self.port))
        ret.listen(10)
        return ret


class CancelToken:
    def __init__(self, parent: Optional['CancelToken'] = None) -> None:
        self.__parent: Optional[CancelToken] = parent
        self.__is_set = False

    def set(self) -> None:
        self.__is_set = True

    @property
    def is_set(self) -> bool:
        if self.__is_set:
            return True
        if self.__parent is not None:
            if self.__parent.is_set:
                return True
        return False

    @property
    def is_not_set(self) -> bool:
        return not self.is_set

    @property
    def parent(self) -> Optional['CancelToken']:
        return self.__parent


def transform(data: bytes, pattern: bytes, offset: int) -> bytes:
    ret = bytearray(data)
    n = 0
    cnt = len(pattern)
    offset = offset % cnt
    while n < len(data):
        ret[n] = data[n] ^ pattern[offset]
        offset = (offset + 1) % cnt
        n += 1
    return bytes(ret)


def streamcopy(
    finalizer: Finalizer,
    session_controller: CancelToken,
    src: sock.socket, dst: sock.socket,
    pattern: bytes,
    name: str = "any"
) -> None:
    offset = 0
    finalizer.put(src)
    finalizer.put(dst)
    while session_controller.is_not_set:
        try:
            try:
                data = src.recv(1)
            except OSError:
                break
            if data is None or len(data) == 0:
                break
            data = transform(data, pattern, offset)
            dst.send(data)
            offset += 1
            try:
                data = src.recv(8192, sock.MSG_DONTWAIT)
            except BlockingIOError:
                data = None
            if data is not None:
                if len(data) > 0:
                    data = transform(data, pattern, offset)
                    dst.send(data, sock.MSG_WAITALL)
                    offset += len(data)
        except Exception as err:
            report(err, "copy error")
            session_controller.set()
    print("Finished session", flush=True)
    noerror(src.shutdown, sock.SHUT_RDWR)
    noerror(dst.shutdown, sock.SHUT_RDWR)
    noerror(src.close)
    noerror(dst.close)
    finalizer.remove(src)
    finalizer.remove(dst)


def start_session(
    finalizer: Finalizer,
    session_controller: CancelToken,
    src: sock.socket,
    dest_addr: PortAddress,
    pattern: bytes
) -> None:
    try:
        dest = dest_addr.open_client()
    except Exception as err:
        report(err, "Error connectiong to %s", dest_addr)
        session_controller.set()
        src.shutdown(sock.SHUT_RDWR)
        src.close()
        return
    th.Thread(
        target=lambda:\
            streamcopy(finalizer, session_controller, src, dest, pattern)
    ).start()
    th.Thread(
        target=lambda:\
            streamcopy(finalizer, session_controller, dest, src, pattern)
    ).start()


def listener(
    finalizer: Finalizer,
    server_controller: CancelToken,
    listen_on: PortAddress, send_to: PortAddress,
    pattern: bytes
) -> None:
    server_socket = listen_on.open_server()
    finalizer.put(server_socket)
    while server_controller.is_not_set:
        try:
            client, ret_addr = server_socket.accept()
            print("Got client from %s" % (ret_addr,))
        except OSError:
            print("Exiting accept loop")
            server_controller.set()
        except Exception as err:
            report(err, "Error accepting")
            server_controller.set()
        else:
            session_controller = CancelToken(server_controller.parent)
            th.Thread(
                target=lambda:\
                    start_session(finalizer, session_controller, client, send_to, pattern)
            ).start()
    server_controller.set()
    finalizer.remove(server_socket)


def usage() -> None:
    print("Usage:")
    print("")
    print("* scrambler [listen_addr:]listen_port:dst_addr:dst_port [<pattern>]")
    print("")
    print("Exiting")


service_controller = CancelToken()

server_controller = CancelToken(service_controller)

pattern = b'\xFF'

if len(sys.argv) not in [2, 3]:
    usage()
    sys.exit(1)

if len(sys.argv) == 3:
    pattern = sys.argv[2].encode()

tokens = sys.argv[1].split(":")

if len(tokens) not in [3,4]:
    usage()
    sys.exit(1)

try:
    finalizer = Finalizer()

    def stop_handler(*args, **kwargs) -> None:
        finalizer.fire_all()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    listener(
        finalizer,
        server_controller,
        PortAddress("127.0.0.1" if len(tokens) == 3 else tokens[0], int(tokens[-3])),
        PortAddress(tokens[-2], int(tokens[-1])),
        pattern
    )
    sys.exit(0)
except Exception as err:
    print("Finished due event: %s" % err)
    sys.exit(1)
