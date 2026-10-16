import os

# Settings require a JWT secret; tests never need a real one.
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-0123456789abcdef")
