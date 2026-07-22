"""
Unit tests for start.py — the clean-slate launcher.

We test the parts that are pure or purely local (no process spawning): the
netstat-output parser that decides which PIDs to kill, and the port-free probe.
The kill/launch paths just shell out to taskkill/lsof/subprocess and aren't
exercised here (they'd touch real processes).
"""

import socket

import start


_NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:5055           0.0.0.0:0              LISTENING       4152
  TCP    127.0.0.1:5056         0.0.0.0:0              LISTENING       6104
  TCP    [::]:5055              [::]:0                 LISTENING       4152
  TCP    192.168.1.20:52344     140.82.112.21:443      ESTABLISHED     9000
  TCP    0.0.0.0:5055           0.0.0.0:0              TIME_WAIT       0
  UDP    0.0.0.0:5353           *:*                                    2200
"""


def test_parse_listening_pids_matches_port_across_bind_forms():
    # Both the 0.0.0.0 and [::] LISTENING rows for 5055 map to PID 4152 (deduped).
    assert start._parse_listening_pids(_NETSTAT, 5055) == {4152}


def test_parse_listening_pids_is_port_specific():
    assert start._parse_listening_pids(_NETSTAT, 5056) == {6104}
    assert start._parse_listening_pids(_NETSTAT, 9999) == set()


def test_parse_listening_pids_ignores_non_listening_and_udp():
    # ESTABLISHED, TIME_WAIT and the UDP row must never be treated as a listener.
    pids = start._parse_listening_pids(_NETSTAT, 443)
    assert pids == set()
    # 5353 is UDP only → no TCP listener.
    assert start._parse_listening_pids(_NETSTAT, 5353) == set()


def test_parse_listening_pids_handles_empty():
    assert start._parse_listening_pids("", 5055) == set()


def test_port_is_free_true_when_nothing_bound():
    # Grab an ephemeral port, close it, and confirm it reads as free.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert start.port_is_free(port) is True


def test_port_is_free_false_while_listener_up():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert start.port_is_free(port) is False
    finally:
        s.close()
    assert start.port_is_free(port) is True


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
    sys.exit(0)
