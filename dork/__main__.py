"""Run Dork:  python -m dork                 start the server
             python -m dork set-key         set / change the master key (revokes all devices)
             python -m dork set-api-key [gemini|groq|anthropic]   store a brain's API key and switch to it"""

import argparse
import getpass
import ipaddress
import logging
import os
import socket
import sys

from . import VERSION, auth, config, netfix
from .config import CFG, CONFIG_PATH


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def tailscale_ip() -> str | None:
    """This PC's Tailscale address (100.64.0.0/10), if Tailscale is running."""
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        return None
    return next((ip for ip in addrs if ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")), None)


def print_qr(url: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except Exception:  # noqa: BLE001 — QR is a convenience only
        pass


def set_key() -> None:
    key = getpass.getpass("New master key (min 8 chars, leave empty to generate one): ").strip()
    if not key:
        key = auth.generate_master_key()
        print(f"\n  Your master key:  {key}\n  Write it down — it is never shown again.\n")
    elif len(key) < 8:
        sys.exit("Master key must be at least 8 characters.")
    elif key != getpass.getpass("Repeat it: ").strip():
        sys.exit("Keys didn't match.")
    auth.set_master_key(key)
    print("Master key set. All previously paired devices were revoked.")


KEY_SOURCES = {
    "gemini": "aistudio.google.com",
    "groq": "console.groq.com",
    "anthropic": "console.anthropic.com",
}


def set_api_key(provider: str) -> None:
    if provider not in KEY_SOURCES:
        sys.exit(f"Usage: python -m dork set-api-key [{'|'.join(KEY_SOURCES)}]")
    key = getpass.getpass(f"{provider} API key (from {KEY_SOURCES[provider]}, input hidden): ").strip()
    if len(key) < 20:
        sys.exit("That key looks too short. Nothing saved.")
    if provider == "anthropic" and not key.startswith("sk-ant-"):
        sys.exit("That doesn't look like an Anthropic API key (should start with sk-ant-).")
    config.save(**{f"{provider}_api_key": key, "provider": provider})
    print(f"Saved to {CONFIG_PATH} (stays on this PC). Dork now uses {provider}. Restart Dork to apply.")


def _already_running(port: int) -> bool:
    """Is a Dork already listening on this port? (Two launchers at login must not start two Dorks.)"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def serve(port: int | None) -> None:
    import uvicorn

    from .server import app

    if not auth.has_master_key():
        key = auth.generate_master_key()
        auth.set_master_key(key)
        print("\n" + "=" * 60)
        print("  FIRST RUN — your MASTER KEY (ownership key):\n")
        print(f"      {key}\n")
        print("  Write it down. It is shown only once and never stored in")
        print("  plain text. Change it any time:  python -m dork set-key")
        print("=" * 60)

    port = port or CFG["port"]
    if _already_running(port):
        print(f"Dork is already running on port {port}; not starting a second copy.")
        return
    if CFG["provider"] == "ollama" or CFG.get("local_engine") == "llamacpp":
        from . import oskit  # the local brain's backup server, on any OS
        oskit.ensure_ollama(CFG["ollama_url"], CFG.get("ollama_models", ""))
    url = f"http://{lan_ip()}:{port}"
    ts = tailscale_ip()
    print(f"\n  DORK {VERSION.upper()} online")
    print(f"  This PC:        http://localhost:{port}")
    print(f"  Same Wi-Fi:     {url}")
    if ts:
        url = f"http://{ts}:{port}"
        print(f"  Anywhere:       {url}   (phone needs Tailscale on, same account)")
    provider = CFG["provider"]
    model = {"anthropic": "claude_model", "ollama": "ollama_model"}.get(provider, f"{provider}_model")
    print(f"  Brain:          {CFG[model]} via {provider}")
    print(f"  Config:         {CONFIG_PATH}\n")
    env_key = {"anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}.get(provider)
    if env_key and not (CFG.get(f"{provider}_api_key") or os.environ.get(env_key)):
        print(f"  ! No {provider} API key yet. Run:  python -m dork set-api-key {provider}\n")
    print_qr(url)
    uvicorn.run(app, host=CFG["host"], port=port, log_level="warning")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is just noise
    netfix.install()
    parser = argparse.ArgumentParser(prog="dork")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "set-key", "set-api-key"])
    parser.add_argument("provider", nargs="?", default="gemini", help="for set-api-key: gemini | groq | anthropic")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.command == "set-key":
        set_key()
    elif args.command == "set-api-key":
        set_api_key(args.provider)
    else:
        serve(args.port)


if __name__ == "__main__":
    main()
