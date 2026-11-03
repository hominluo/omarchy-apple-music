"""Command line entry points.

The shell only ever runs ``serve``; the other verbs exist so the engine can be
driven and diagnosed from a terminal without the shell running.
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
import time

from . import config


def _setup_logging(verbose: bool = False) -> None:
    handlers = [logging.FileHandler(config.cache_dir() / "engine.log", encoding="utf-8")]
    if verbose:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def _request(payload: dict, timeout: float = 30.0) -> dict:
    """One request, one answer, over the daemon's socket.

    The daemon broadcasts state events to every connected client, including
    the one that just sent a command, so the answer is the first line that
    carries an id rather than simply the first line.
    """
    import socket

    path = config.socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(min(timeout, 5.0))
        connection.connect(str(path))
        connection.sendall((json.dumps({"id": 1, "cmd": payload.pop("cmd"), "params": payload}) + "\n").encode())
        buffered = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b"\n" in buffered:
                line, buffered = buffered.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if "event" in message:
                    continue
                return message
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffered += chunk
    return {"ok": False, "error": {"message": "no answer"}}


def cmd_serve(args) -> int:
    from .daemon import Daemon

    _setup_logging(args.verbose)
    daemon = Daemon()

    async def run():
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        # A shell restart sends SIGTERM. Without this the daemon dies inside
        # its event loop and mpv is reparented instead of stopped.
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        try:
            await daemon.serve(stop)
        finally:
            await daemon.shutdown()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_status(args) -> int:
    print(json.dumps(_request({"cmd": "status"}), indent=2, ensure_ascii=False))
    return 0


def cmd_call(args) -> int:
    payload = {"cmd": args.command}
    if args.params:
        payload.update(json.loads(args.params))
    answer = _request(payload, timeout=args.timeout)
    print(json.dumps(answer, indent=2, ensure_ascii=False))
    return 0 if answer.get("ok") else 1


def cmd_signin(args) -> int:
    from . import auth

    result = auth.launch_login(config.load_settings().get("storefront") or "us")
    print(json.dumps(result, indent=2))
    print("Sign in in the window that just opened; the token is picked up automatically.")
    token = auth.wait_for_token(timeout=args.timeout)
    if not token:
        print("No session found.", file=sys.stderr)
        return 1
    auth.save(token, source=str(config.login_profile()),
              storefront=config.load_settings().get("storefront") or "us")
    print("Stored a music user token (%d chars)." % len(token))
    return 0


def cmd_token(args) -> int:
    from . import auth

    record = auth.load()
    token = record.get("mediaUserToken") or ""
    print(token if token else "(no token)")
    return 0 if token else 1


def cmd_probe(args) -> int:
    """Resolve, license, download and decrypt one track, then report."""
    from .apple import ApiError
    from .stream import StreamBuilder

    _setup_logging(args.verbose)
    from . import auth
    from .apple import AppleApi

    async def run():
        record = auth.load()
        api = AppleApi(
            media_user_token=record.get("mediaUserToken") or "",
            storefront=record.get("storefront") or config.load_settings().get("storefront") or "us",
        )
        if not api.media_user_token:
            print("No stored token; run `apple-musicd.py signin` first.", file=sys.stderr)
            return 1
        await api.fetch_account()
        song = await api.song(args.song_id, args.library)
        print(json.dumps(song, indent=2))
        builder = StreamBuilder(api, notify=lambda name, data: print("  build:", data))

        async def progress(name, data):
            print("  build:", data)

        builder.notify = progress
        started = time.monotonic()
        path = await builder.build(song, codec=args.codec, force=True)
        elapsed = time.monotonic() - started
        size = path.stat().st_size
        print(json.dumps({
            "path": str(path), "bytes": size,
            "seconds": round(elapsed, 2),
            "kbps": round(size * 8 / 1000 / max(elapsed, 0.001)),
            "audio": await asyncio.to_thread(_describe, path),
        }, indent=2))
        if args.play:
            import subprocess
            subprocess.run(["mpv", "--no-video", "--really-quiet", "--force-window=no", str(path)])
        return 0

    try:
        return asyncio.run(run())
    except ApiError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 1


def _describe(path) -> dict:
    import subprocess

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return {"error": result.stderr.strip()}
    data = json.loads(result.stdout)
    stream = (data.get("streams") or [{}])[0]
    container = data.get("format") or {}
    return {
        "codec": stream.get("codec_name"),
        "channels": stream.get("channels"),
        "sampleRate": stream.get("sample_rate"),
        "bitrate": container.get("bit_rate"),
        "duration": container.get("duration"),
        "tags": {k: v for k, v in (container.get("tags") or {}).items()
                 if k.lower() in ("title", "artist", "album", "track", "date")},
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="apple-musicd", description="Apple Music engine for the Omarchy shell")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--timeout", type=float, default=300.0)
    sub = parser.add_subparsers(dest="verb")

    serve = sub.add_parser("serve", help="run the daemon (default)")
    serve.set_defaults(func=cmd_serve)

    status = sub.add_parser("status", help="print the daemon snapshot")
    status.set_defaults(func=cmd_status)

    call = sub.add_parser("call", help="send one command to the daemon")
    call.add_argument("command")
    call.add_argument("params", nargs="?")
    call.set_defaults(func=cmd_call)

    signin = sub.add_parser("signin", help="open the sign-in window and store the token")
    signin.set_defaults(func=cmd_signin)

    token = sub.add_parser("token", help="print the stored music user token")
    token.set_defaults(func=cmd_token)

    probe = sub.add_parser("probe", help="build and decrypt one track end to end")
    probe.add_argument("song_id")
    probe.add_argument("--library", action="store_true")
    probe.add_argument("--codec", default="aac-web")
    probe.add_argument("--play", action="store_true")
    probe.set_defaults(func=cmd_probe)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args([*argv, "serve"])
    _setup_logging(args.verbose)
    return args.func(args)
