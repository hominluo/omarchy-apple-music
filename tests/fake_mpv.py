"""A stand-in for mpv's JSON IPC, for the player lifecycle tests.

It understands only what engine/player.py sends. Tests put a copy on PATH as
`mpv`, so no real audio process is ever started. Behaviour by file name:
"short" files end after 0.2 s, "corrupt" files fail to open.

Like mpv, loadfile answers before the file has opened: until it has, a seek
is refused and time-pos is unavailable (the old fake took the seek at once,
which hid a player that seeked right after loadfile and never moved). The
start position goes in loadfile's options, `start=<seconds>`, after the
index argument mpv 0.38 added; FAKE_MPV_LEGACY makes it an older mpv, which
has no index argument and takes the options one place earlier.

FAKE_MPV_DELAY       seconds to wait before opening the socket
FAKE_MPV_OPEN_DELAY  seconds a loaded file takes to open (default 0.05)
FAKE_MPV_LEGACY      set to 1 for the loadfile of mpv before 0.38
FAKE_MPV_LOG         file that gets one JSON line per command received
"""

import asyncio
import json
import os
import sys


def main() -> None:
    socket_path = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--input-ipc-server="):
            socket_path = arg.split("=", 1)[1]
    if not socket_path:
        sys.exit(2)
    for arg in sys.argv[1:]:
        if arg.startswith("--volume="):
            volume = float(arg.split("=", 1)[1])
            if volume < -1 or volume > 1000:
                sys.exit(1)  # what real mpv does with an out-of-range volume
    asyncio.run(serve(socket_path))


def _parse_loadfile(command: list, legacy: bool):
    """The start position loadfile asks for, or None if mpv would refuse the
    arguments: [loadfile, url, flags, index, options] from 0.38 on, [loadfile,
    url, flags, options] before."""
    if legacy:
        if len(command) > 4:
            return None
        options = command[3] if len(command) > 3 else ""
    else:
        if len(command) > 3:
            index = command[3]
            try:
                int(index)
            except (TypeError, ValueError):
                return None  # "start=…" where 0.38 expects the index
        options = command[4] if len(command) > 4 else ""
    start = 0.0
    for option in str(options or "").split(","):
        key, _, value = option.partition("=")
        if key == "start":
            try:
                start = float(value)
            except ValueError:
                return None
    return start


async def serve(socket_path: str) -> None:
    delay = float(os.environ.get("FAKE_MPV_DELAY") or 0)
    open_delay = float(os.environ.get("FAKE_MPV_OPEN_DELAY") or 0.05)
    legacy = os.environ.get("FAKE_MPV_LEGACY") == "1"
    log_path = os.environ.get("FAKE_MPV_LOG") or ""
    if delay:
        await asyncio.sleep(delay)
    state = {"idle-active": True, "pause": False, "duration": 0.0, "volume": 100.0,
             "media-title": ""}
    # Whether the loaded file has opened, and where it is.
    playback = {"opened": False, "pos": 0.0, "load": 0}
    observed: set[str] = set()
    done = asyncio.Event()

    async def client(reader, writer):
        def send(payload):
            writer.write((json.dumps(payload) + "\n").encode())

        def change(name, value):
            state[name] = value
            if name in observed:
                send({"event": "property-change", "name": name, "data": value})

        async def open_soon(load: int, short: bool):
            await asyncio.sleep(open_delay)
            if playback["load"] != load or state["idle-active"]:
                return
            playback["opened"] = True
            if short:
                await asyncio.sleep(0.2)
                if playback["load"] != load:
                    return
                playback["opened"] = False
                send({"event": "end-file", "reason": "eof"})
                change("idle-active", True)
                await writer.drain()

        while True:
            line = await reader.readline()
            if not line:
                break
            request = json.loads(line)
            command = request.get("command") or []
            if log_path:
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(command) + "\n")
            reply = {"request_id": request.get("request_id"), "error": "success"}
            name = command[0] if command else ""
            if name == "observe_property":
                observed.add(command[2])
                send(reply)
                change(command[2], state.get(command[2]))
            elif name == "set_property":
                send(reply)
                change(command[1], command[2])
            elif name == "get_property":
                if command[1] == "time-pos":
                    if state["idle-active"] or not playback["opened"]:
                        send({**reply, "error": "property unavailable"})
                    else:
                        send({**reply, "data": playback["pos"]})
                else:
                    send({**reply, "data": state.get(command[1])})
            elif name == "loadfile":
                start = _parse_loadfile(command, legacy)
                if start is None:
                    send({**reply, "error": "invalid parameter"})
                    await writer.drain()
                    continue
                send(reply)
                path = command[1]
                playback.update({"opened": False, "pos": start, "load": playback["load"] + 1})
                if "corrupt" in os.path.basename(path):
                    send({"event": "end-file", "reason": "error", "file_error": "unrecognized file format"})
                else:
                    change("idle-active", False)
                    asyncio.ensure_future(open_soon(playback["load"], "short" in os.path.basename(path)))
            elif name == "seek":
                if state["idle-active"] or not playback["opened"]:
                    send({**reply, "error": "error running command"})
                else:
                    playback["pos"] = float(command[1])
                    send(reply)
            elif name == "stop":
                send(reply)
                playback.update({"opened": False, "pos": 0.0, "load": playback["load"] + 1})
                change("idle-active", True)
            else:
                send(reply)
            await writer.drain()
        done.set()

    server = await asyncio.start_unix_server(client, path=socket_path)
    async with server:
        await asyncio.Event().wait()


if __name__ == "__main__":
    main()
