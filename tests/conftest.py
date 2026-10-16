import os

# Settings require a JWT secret; tests never need a real one.
os.environ.setdefault("JWT_SECRET", "test-only-secret")
