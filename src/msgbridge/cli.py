"""msgbridge command line.

    msgbridge index            rebuild the index from chat.db and Contacts
    msgbridge sync             pick up new messages, edits and reactions
    msgbridge status           index counts and freshness (no message text)
    msgbridge serve            run the MCP server over stdio (Claude Desktop starts this)
    msgbridge call TOOL JSON   run one tool locally and print its output
"""
import argparse
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
    call = sub.add_parser("call")
    call.add_argument("tool")
    call.add_argument("args", nargs="?", default="{}")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        from .server import main as serve
        return serve()
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


if __name__ == "__main__":
    main()
