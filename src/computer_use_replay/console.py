"""Cancellable POSIX terminal/pipe input; never leave an executor thread on stdin."""

import asyncio
import os
import sys

from computer_use_replay.policy import Stop


async def console_input():
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    data = bytearray()
    try:
        fd = sys.stdin.fileno()

        def ready():
            try:
                byte = os.read(fd, 1)
                if not byte:
                    raise Stop("operator_disconnected")
                if byte == b"\n":
                    future.set_result(data.decode("utf-8", errors="replace"))
                elif len(data) >= 256:
                    raise Stop("operator_input_too_long")
                else:
                    data.extend(byte)
            except (OSError, Stop) as exc:
                future.set_exception(exc)
            if future.done():
                loop.remove_reader(fd)

        loop.add_reader(fd, ready)
    except (OSError, ValueError, NotImplementedError):
        raise Stop("console_unavailable") from None
    try:
        return await future
    finally:
        loop.remove_reader(fd)
