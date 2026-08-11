"""Admin CLI for multi-tenant token stores (`garmin-mcp-tenant`).

Deleting is a command rather than a web endpoint on purpose. Onboarding's auth
model is possession-of-URL, so a delete endpoint would be an unauthenticated
destructive surface: anyone who ever saw a URL could wipe that person's access.
A command run on the host keeps deletion behind SSH, and "how do I delete this"
becomes a documented, auditable step instead of a button.

    garmin-mcp-tenant list
    garmin-mcp-tenant delete <user-id>
"""
import argparse
import sys
from pathlib import Path

from garmin_mcp import multitenant
from garmin_mcp.onboarding.store import delete_token_store, list_user_ids


def _root(explicit: str | None) -> Path:
    root = Path(explicit) if explicit else multitenant.multi_tenant_root()
    if root is None:
        sys.exit(f"No token-store root: pass --root or set {multitenant.MULTI_TENANT_ROOT_ENV}.")
    return root


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="garmin-mcp-tenant", description="Manage multi-tenant Garmin token stores"
    )
    parser.add_argument(
        "--root",
        default=None,
        help=f"Token-store root (default: ${multitenant.MULTI_TENANT_ROOT_ENV})",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List user IDs that have a token store")
    delete = sub.add_parser("delete", help="Delete one user's token store")
    delete.add_argument("user_id")

    args = parser.parse_args(argv)
    root = _root(args.root)

    if args.command == "list":
        ids = list_user_ids(root)
        for user_id in ids:
            print(user_id)
        print(f"{len(ids)} token store(s) in {root}", file=sys.stderr)
        return

    try:
        removed = delete_token_store(root, args.user_id)
    except ValueError as exc:
        sys.exit(str(exc))
    if not removed:
        sys.exit(f"No token store for {args.user_id} in {root}")
    print(f"Deleted token store for {args.user_id}. Their connector URL now 404s.")


if __name__ == "__main__":
    main()
