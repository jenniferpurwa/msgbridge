"""msgbridge command line.

    msgbridge index            rebuild the index from chat.db and Contacts
    msgbridge sync             pick up new messages, edits and reactions
    msgbridge status           index counts and freshness (no message text)
    msgbridge serve            run the MCP server over stdio (Claude Desktop starts this)
    msgbridge serve-http       run the OAuth-protected web server for ChatGPT (on 127.0.0.1)
    msgbridge auth setup       set your sign-in password and authenticator code (run it yourself)
    msgbridge auth password    change only the password, keeping your authenticator entry
    msgbridge auth check       test your password and authenticator code (run it yourself)
    msgbridge auth revoke-all  sign out every connected app
    msgbridge call TOOL JSON   run one tool locally and print its output
"""
import argparse
import getpass
import json
import sys

from .index import Config, Index
from .tools import Tools


def main(argv=None):
    ap = argparse.ArgumentParser(prog="msgbridge", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index")
    sub.add_parser("sync")
    sub.add_parser("status")
    sub.add_parser("serve")
    sub.add_parser("serve-http")
    auth = sub.add_parser("auth")
    auth.add_argument("action", choices=["setup", "password", "check", "revoke-all"])
    call = sub.add_parser("call")
    call.add_argument("tool")
    call.add_argument("args", nargs="?", default="{}")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        from .server import main as serve
        return serve()
    if a.cmd == "serve-http":
        from .server import serve_http
        return serve_http()
    if a.cmd == "auth":
        return auth_command(a.action)
    index = Index(Config.from_env())
    if a.cmd == "index":
        print(json.dumps(index.rebuild()))
    elif a.cmd == "sync":
        print(json.dumps(index.sync()))
    elif a.cmd == "status":
        print(Tools(index, auto_sync=False).get_status())
    elif a.cmd == "call":
        tools = Tools(index)
        fn = getattr(tools, a.tool, None)
        if fn is None or a.tool.startswith("_"):
            sys.exit(f"Unknown tool {a.tool}")
        print(fn(**json.loads(a.args)))



def auth_command(action: str):
    from .auth import Auth, setup_credentials
    from .server import DATA_DIR, PUBLIC_URL
    if action == "revoke-all":
        n = Auth(DATA_DIR / "auth.db", DATA_DIR / "credentials.json", PUBLIC_URL).revoke_all()
        print(f"Revoked {n} token(s). Connected apps must sign in again.")
        return
    if not sys.stdin.isatty():
        sys.exit("Run this yourself in a terminal: it asks for a password.")
    if action == "check":
        auth = Auth(DATA_DIR / "auth.db", DATA_DIR / "credentials.json", PUBLIC_URL)
        pw = getpass.getpass("Password: ")
        if any(m in pw for m in ("\x1b[200~", "\x1b[201~")):
            print("(Your terminal added paste markers to the password; msgbridge now ignores them.)")
        hidden = sum(1 for ch in pw if not ch.isprintable())
        print(f"(Received {len(pw)} characters" + (f", {hidden} of them invisible control characters" if hidden else "")
              + ". Does that length match your password?)")
        result = auth.diagnose(pw, input("Authenticator code: "))
        print("\n".join(result))
        if result[0].endswith("correct") and result[1] == "Code: correct":
            auth.clear_failures()
            print("Both correct: failed attempts cleared. Sign in again from ChatGPT with a fresh code.")
        return
    print("Choose a sign-in password for msgbridge (at least 12 characters, not used anywhere else).")
    while True:
        pw = getpass.getpass("Password: ")
        if len(pw) < 12:
            print("Too short.")
        elif pw != getpass.getpass("Again: "):
            print("Didn't match.")
        else:
            break
    hidden = sum(1 for ch in pw if not ch.isprintable())
    print(f"(Received {len(pw)} characters" + (f", {hidden} of them invisible control characters" if hidden else "") + ")")
    keep = action == "password"
    secret = setup_credentials(DATA_DIR / "credentials.json", pw, keep_secret=keep)
    Auth(DATA_DIR / "auth.db", DATA_DIR / "credentials.json", PUBLIC_URL).revoke_all()
    if keep:
        print("Password changed. Your authenticator entry stays the same. Existing sign-ins were revoked.")
        return
    print("\nAdd this to your authenticator app (Apple Passwords, 1Password, Google Authenticator...).")
    print("Choose 'enter a setup key' and type the key, or open the link on this Mac:")
    print(f"  Setup key: {secret}")
    print(f"  Link: otpauth://totp/msgbridge?secret={secret}&issuer=msgbridge")
    print("\nSaved. Existing sign-ins were revoked. Keep this key private; clear this terminal when done.")


if __name__ == "__main__":
    main()
