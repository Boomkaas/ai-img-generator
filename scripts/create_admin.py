"""Create an admin account, or promote an existing user to admin.

    python -m scripts.create_admin admin@example.com

A new account asks for its password twice (hidden input); an existing one keeps its password.
"""

import argparse
import getpass
import sys

from api.security import email_looks_valid, hash_password, normalise_email
from common import db
from common.config import get_settings


def ask_password() -> str:
    minimum = get_settings().MIN_PASSWORD_LEN
    while True:
        password = getpass.getpass("Password: ")
        if len(password) < minimum:
            print(f"Choose a password of at least {minimum} characters.")
        elif password != getpass.getpass("Repeat the password: "):
            print("The passwords don't match.")
        else:
            return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("email")
    email = normalise_email(parser.parse_args(argv).email)
    if not email_looks_valid(email):
        print(f"{email!r} doesn't look like an email address.", file=sys.stderr)
        return 1

    user = db.get_user_by_email(email)
    if user is None:
        db.create_user(email, hash_password(ask_password()), role="admin")
        print(f"Created admin {email}.")
    elif user.role == "admin":
        print(f"{email} is already an admin.")
    else:
        db.set_role(user.id, "admin")
        print(f"Promoted {email} to admin; the password is unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
