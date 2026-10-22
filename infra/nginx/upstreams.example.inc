# One line per app machine: its private address and the API port it publishes in host mode.
# Delivered to Nginx as the Swarm config nginx_upstreams (scripts/swarm/create-secrets.sh).
server 10.0.0.11:8000 max_fails=3 fail_timeout=10s;    # machine B
server 10.0.0.12:8000 max_fails=3 fail_timeout=10s;    # machine C
