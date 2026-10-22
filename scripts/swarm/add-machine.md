# Adding an app machine

The new machine needs Docker, NTP, and the firewall rules from the runbook: 2377/tcp, 7946/tcp+udp and
4789/udp open to the other machines, and port 8000 reachable from machine A only. In the commands, `<tag>` is
the deployed tag, `<node>` the new machine's hostname, and `<ip>` its private address.

1. **Join.** On machine A, print the join command:

   ```sh
   docker swarm join-token worker
   ```

   Run the printed `docker swarm join --token ... <A-ip>:2377` on the new machine.

2. **Pre-pull.** On the new machine, so the first worker placed there doesn't wait minutes for the image:

   ```sh
   REGISTRY=<registry> scripts/swarm/prepull.sh <tag>
   ```

   (Do this before labelling, so no worker is placed there while the pull is still running.)

3. **Calibrate.** On the new machine:

   ```sh
   docker run --rm --cpus 2 --memory 10g -e STEP_DELAY_SECONDS=2 <registry>/imagegen-worker:<tag> \
       python -m scripts.calibrate
   ```

   If its 768 px time is slower than the configured `MEASURED_MAX_RUNTIME_S`, it is now the slowest machine:
   update the timeouts as the runbook's calibration section describes before going on. Note the number of
   workers that fit.

4. **Label.** On machine A:

   ```sh
   scripts/swarm/label-app-node.sh <node>
   ```

5. **Add it to Nginx.** Swarm configs can't be edited, so create a new version and switch Nginx to it. On
   machine A, with the current list in `upstreams.inc`:

   ```sh
   echo 'server <ip>:8000 max_fails=3 fail_timeout=10s;' >> upstreams.inc
   docker config create nginx_upstreams_v2 upstreams.inc
   docker service update \
       --config-rm nginx_upstreams \
       --config-add source=nginx_upstreams_v2,target=/etc/nginx/upstreams.inc \
       imagegen_nginx
   ```

   Use the name currently in use for `--config-rm` (`docker service inspect imagegen_nginx` shows it), and
   set `NGINX_UPSTREAMS_CONFIG=nginx_upstreams_v2` in `infra/.env.prod`, or the next deploy switches back.
   Nginx restarts in a second or two; open WebSockets reconnect on their own.

6. **Raise the API replicas.** One per app machine:

   ```sh
   docker service scale imagegen_api=<number of app machines>
   ```

   and set `API_REPLICAS` to the same number in `infra/.env.prod`.

7. **Raise MAX_WORKERS** by the number of workers that fit on the new machine, as calibration printed:

   ```sh
   docker service update --env-add MAX_WORKERS=<new total> imagegen_controller
   ```

   and set `MAX_WORKERS` in `infra/.env.prod`.

8. **Check.** `docker node ls` shows the machine `Ready`; `docker service ps imagegen_api` shows a replica
   running there; the admin page lists the machine once its API server reports in, and its workers once the
   autoscaler places one there.
