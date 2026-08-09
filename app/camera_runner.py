"""Credential-isolating ffmpeg launcher used only as a subprocess."""

from __future__ import annotations

import ctypes
import json
import os
import sys
from typing import Any

MAX_PAYLOAD_BYTES = 4096
PR_SET_DUMPABLE = 4


def _read_payload(file_descriptor: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(file_descriptor, min(1024, MAX_PAYLOAD_BYTES + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_PAYLOAD_BYTES:
            raise ValueError("oversized launch payload")
        chunks.append(chunk)


def _disable_process_inspection() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl failed")


def _validated_command(raw: Any) -> tuple[str, list[str]]:
    if not isinstance(raw, dict):
        raise TypeError("invalid launch payload")
    executable = raw.get("executable")
    arguments = raw.get("arguments")
    if (
        not isinstance(executable, str)
        or not executable.startswith("/")
        or not isinstance(arguments, list)
        or len(arguments) > 80
        or not all(isinstance(item, str) and len(item) <= 2048 for item in arguments)
    ):
        raise ValueError("invalid launch payload")
    return executable, arguments


def main() -> None:
    try:
        if len(sys.argv) != 2:
            raise ValueError("missing launch descriptor")
        file_descriptor = int(sys.argv[1])
        payload = _read_payload(file_descriptor)
        os.close(file_descriptor)
        executable, arguments = _validated_command(json.loads(payload))
        _disable_process_inspection()
        os.execv(executable, [executable, *arguments])
    except Exception:  # noqa: BLE001 - errors must never disclose the launch payload
        # Never print the exception: it may contain the credential-bearing URL.
        os._exit(126)


if __name__ == "__main__":
    main()
