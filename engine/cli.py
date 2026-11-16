"""Command line entry points.

The shell runs ``serve``; the other verbs exist so the engine can be driven
and diagnosed from a terminal without the shell running. ``call``, ``status``
and ``token`` must stay standard-library only at import time: apple-musicd.py
runs them on whatever python3 started it, without the venv.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import stat
import sys
import time

from . import config

log = logging.getLogger("apple-musicd")


def _setup_logging(verbose: bool = False) -> None:
    handlers: list[logging.Handler] = []
    try:
        handlers.append(logging.FileHandler(config.cache_dir() / "engine.log", encoding="utf-8"))
    except OSError:
        # An unwritable cache must not stop the daemon; stderr still works.
        verbose = True
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

    payload = dict(payload)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(min(timeout, 5.0))
    try:
        connection.connect(str(config.socket_path()))
    except OSError as error:
        connection.close()
        return {"ok": False, "error": {"code": "disconnected",
                                       "message": f"the daemon is not running ({error.strerror or error})"}}
    with connection:
        try:
            connection.sendall((json.dumps({"id": 1, "cmd": payload.pop("cmd"), "params": payload}) + "\n").encode())
        except OSError:
            # Connected to a daemon that is closing: it took the socket
            # down mid-request, which is no answer rather than no daemon.
            return {"ok": False, "error": {"message": "no answer"}}
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


# ---- startup errors ---------------------------------------------------------
# The shell starts the daemon detached and learns nothing but whether the
# socket answers. A daemon that dies on the way up (mpv missing, the API down
# at startup, a broken install) used to leave the bar on "starting…" forever
# with the traceback lost on a stderr nobody reads. So `serve` writes one line
# about the failure where Service.qml looks for it, and removes it once the
# socket is listening. apple-musicd.py writes the same file for failures
# before the engine can even be imported.

STARTUP_ERROR = "startup-error"
_TOKENISH = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


def _redact(text: str) -> str:
    """Drop anything shaped like a token. Exception text can quote a URL or a
    header, and this line is shown in the bar. Paths survive: they rarely mix
    upper case, lower case and digits in one 40-character run."""
    def scrub(match):
        run = match.group(0)
        mixed = (any(c.isupper() for c in run) and any(c.islower() for c in run)
                 and any(c.isdigit() for c in run))
        return "…" if mixed else run
    return _TOKENISH.sub(scrub, text)


def describe_failure(error: BaseException, limit: int = 300) -> str:
    message = str(error).strip()
    text = f"{type(error).__name__}: {message}" if message else type(error).__name__
    line = _redact(" ".join(text.split()))
    return line if len(line) <= limit else line[: limit - 1] + "…"


def startup_error_path():
    try:
        return config.runtime_dir() / STARTUP_ERROR
    except RuntimeError:
        return None


def write_startup_error(message: str) -> None:
    path = startup_error_path()
    if path is None:
        return
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps({"message": message, "at": int(time.time())}) + "\n", "utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except OSError:
        log.warning("could not write %s", path, exc_info=True)


def clear_startup_error() -> None:
    path = startup_error_path()
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("could not remove %s", path, exc_info=True)


def _listening_since(started: float) -> bool:
    """True once a socket bound by this process is at the daemon's path.

    A socket left by a crashed daemon is older than us; the daemon removes it
    before binding its own, and binding plus listen happen in one call, so a
    socket newer than our start is a listening one.
    """
    try:
        info = os.stat(config.socket_path())
    except (OSError, RuntimeError):
        return False
    return stat.S_ISSOCK(info.st_mode) and info.st_ctime >= started - 1


async def _clear_when_listening(started: float, interval: float = 0.2) -> None:
    while not _listening_since(started):
        await asyncio.sleep(interval)
    clear_startup_error()


def _startup_failed(error: BaseException) -> int:
    message = describe_failure(error)
    log.error("daemon failed: %s", message, exc_info=error)
    write_startup_error(message)
    return 1


def cmd_serve(args) -> int:
    _setup_logging(args.verbose)
    started = time.time()
    try:
        from .daemon import Daemon

        daemon = Daemon()
    except Exception as error:  # noqa: BLE001 - every failure here is a startup failure
        return _startup_failed(error)

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
        watcher = asyncio.create_task(_clear_when_listening(started))
        try:
            await daemon.serve(stop)
        finally:
            watcher.cancel()
            await daemon.shutdown()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception as error:  # noqa: BLE001
        return _startup_failed(error)
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


def _check_token(token: str, storefront: str) -> tuple[str, dict | None]:
    """("ok", account), ("refused", None) or ("unreachable", None): what
    Apple says about a captured token, the same check the daemon makes."""
    from .apple import ApiError, AppleApi

    try:
        account = asyncio.run(AppleApi(media_user_token=token, storefront=storefront).fetch_account())
    except ApiError as error:
        if error.code == "unauthorized":
            return "refused", None
        log.warning("could not check the session: %s", error.message)
        return "unreachable", None
    return "ok", account


def cmd_signin(args) -> int:
    """The daemon's sign-in, from a terminal. A revoked session's cookie is
    still in the plugin profile until it reaches its expiry date, and it was
    read the moment the window opened, saved and the window closed before the
    user could sign in; so the dead token is refused, and every token found
    is checked with Apple before it is kept."""
    from . import auth

    storefront = config.load_settings().get("storefront") or "us"
    record = auth.load()
    old = record.get("mediaUserToken") or ""
    # Only a token Apple has already turned down: a good one being found
    # again is a sign-in that is already done, not one to wait past.
    reject = {old} if old and (record.get("error") or record.get("expired")) else set()
    result = auth.launch_login(storefront)
    print(json.dumps(result, indent=2))
    print("Sign in in the window that just opened; the token is picked up automatically.")
    deadline = time.monotonic() + args.timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            token = auth.wait_for_token(timeout=remaining, reject=reject, close=False) if remaining > 0 else None
            if not token:
                print("No session found.", file=sys.stderr)
                return 1
            verdict, account = _check_token(token, storefront)
            if verdict == "refused":
                reject.add(token)
                if auth.login_running():
                    print("Apple did not accept that session; finish signing in in the window.",
                          file=sys.stderr)
                    continue
                print("Found a session but Apple rejected it (check the subscription).", file=sys.stderr)
                return 1
            from .apple import storefront_from_account

            auth.save(token, source=str(config.login_profile()),
                      storefront=storefront_from_account(account, storefront), account=account)
            if verdict == "ok":
                print("Stored a music user token (%d chars)." % len(token))
            else:
                print("Stored a music user token (%d chars); Apple Music could not be reached "
                      "to check it." % len(token))
            return 0
    finally:
        # The window (and its DevTools port) has done its job either way.
        auth.close_login()


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
        # The cache belongs to the daemon: this process cannot know which
        # tracks it is playing or which build folders are live, so it
        # neither prunes nor sweeps.
        builder = StreamBuilder(api, notify=lambda name, data: print("  build:", data),
                                manage_cache=False)

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
