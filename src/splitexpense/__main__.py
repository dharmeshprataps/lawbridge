"""Run the local split-expense REST demo."""
import argparse

from .server import create_server
from .store import ExpenseStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--db", default=".lawbridge/splitexpense.sqlite3")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    store = ExpenseStore(args.db)
    try:
        server = create_server(store, args.host, args.port)
        try:
            host, port = server.server_address[:2]
            print(f"Split expense REST service: http://{host}:{port} (SQLite: {args.db})", flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
