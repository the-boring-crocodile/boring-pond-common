"""boring-service-oauth: manage the clients of a service's own issuer.

Run it as the service's account (the store is private to it):

    boring-service-oauth --db STORE client-add --name "VS Code" \\
        --redirect-uri http://127.0.0.1/ --redirect-uri https://vscode.dev/redirect
    boring-service-oauth --db STORE clients
    boring-service-oauth --db STORE client-remove CLIENT_ID
    boring-service-oauth --db STORE client-prune --idle-days 90 [--apply]
    boring-service-oauth --db STORE backup NEW_FILE

`client-add` prints the client id (and a secret, with --auth other than none)
once; give them to the person setting up the client. Claude and ChatGPT need
no entry here: they identify themselves with a metadata document.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .clients import AUTH_METHODS, add_client, list_clients, prune_clients, remove_client
from .store import Transaction, backup, initialize_issuer_store, private_file


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="boring-service-oauth", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, required=True, help="the issuer store (issuer.database in the config)")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("client-add", help="pre-register a client")
    add.add_argument("--name", required=True)
    add.add_argument("--redirect-uri", action="append", required=True)
    add.add_argument("--auth", choices=AUTH_METHODS, default="none",
                     help="token endpoint authentication (default: none, a public client with PKCE)")
    commands.add_parser("clients", help="every client with origin, state and live grants")
    remove = commands.add_parser("client-remove", help="remove a client and everything it was granted")
    remove.add_argument("client_id")
    prune = commands.add_parser("client-prune", help="clients idle this long without a live refresh token")
    prune.add_argument("--idle-days", type=float, default=90)
    prune.add_argument("--apply", action="store_true", help="remove them (default: only list)")
    copy = commands.add_parser("backup", help="consistent copy into a new private file")
    copy.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    database = args.db.resolve()
    try:
        private_file(database)
        with Transaction(database) as db:
            initialize_issuer_store(db)
            if args.command == "client-add":
                result: object = add_client(db, args.name, args.redirect_uri, args.auth)
            elif args.command == "clients":
                result = list_clients(db)
            elif args.command == "client-remove":
                if not remove_client(db, args.client_id):
                    raise ValueError(f"no client {args.client_id}")
                result = {"removed": args.client_id}
            elif args.command == "client-prune":
                result = {"applied": args.apply, "clients": prune_clients(db, args.idle_days, args.apply)}
            else:
                result = None
        if args.command == "backup":
            backup(database, args.destination.resolve())
            result = {"backup": str(args.destination.resolve())}
    except (ValueError, PermissionError, OSError) as error:
        print(f"boring-service-oauth: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
