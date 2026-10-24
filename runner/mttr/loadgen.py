"""Open-loop load generator, run on the host outside the failure domain.

Requests are sent on a fixed schedule whatever the responses do, so an outage
shows up as failed requests rather than as a slower request rate. Every request
is logged with its send time, latency, status (0 = timeout or connection error)
and the release and pod that answered, which is the service-level record of a
trial.

    python -m mttr.loadgen --target http://IP --stream main=canary.mttr.lab:20 --out log.csv
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import time

import aiohttp


async def stream(session, target, name, host, rps, timeout, out, stop):
    period = 1.0 / rps
    start = time.time()
    i = 0
    tasks = set()

    async def one(t_send):
        status, release, pod = 0, "", ""
        t0 = time.perf_counter()
        try:
            async with session.get(target + "/", headers={"Host": host},
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                await r.read()
                status, release, pod = r.status, r.headers.get("X-Release", ""), r.headers.get("X-Pod", "")
        except Exception:
            pass
        lat = (time.perf_counter() - t0) * 1000
        out.write(f"{t_send:.3f},{name},{status},{lat:.1f},{release},{pod}\n")

    while not stop.is_set():
        t_next = start + i * period
        delay = t_next - time.time()
        if delay > 0:
            try:
                await asyncio.wait_for(stop.wait(), delay)
                break
            except asyncio.TimeoutError:
                pass
        task = asyncio.create_task(one(time.time()))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        i += 1
    if tasks:
        await asyncio.wait(tasks, timeout=timeout + 1)


async def main(args):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    out = open(args.out, "a", buffering=1)
    if out.tell() == 0:
        out.write("t,stream,status,latency_ms,release,pod\n")
    conn = aiohttp.TCPConnector(limit=200, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=conn) as session:
        jobs = []
        for spec in args.stream:
            name, rest = spec.split("=", 1)
            host, rps = rest.rsplit(":", 1)
            jobs.append(stream(session, args.target, name, host, float(rps), args.timeout, out, stop))
        await asyncio.gather(*jobs)
    out.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", required=True)
    p.add_argument("--stream", action="append", required=True, help="name=host:rps")
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--out", required=True)
    asyncio.run(main(p.parse_args()))
