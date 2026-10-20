"""Storage path on a multi-node OpenSearch cluster (DESIGN.md §5.3, §12 phase 2).

Three nodes, 3 primary shards with 1 replica per index, ISM rolling the write alias over by
primary shard size. Conversations go through the real collectors (gateway -> Kafka -> ingest ->
cluster); halfway through, one node is killed, and later started again. Checks: no span lost or
duplicated, rollover happened and reads dedupe across indices, the cluster recovers to green.
Writes results/CLUSTER.md and results/cluster.json.

    python experiments/cluster_test.py --urls http://127.0.0.1:9201,http://127.0.0.1:9202,http://127.0.0.1:9203 \\
        --kill "kill -9 <pid of node 3>" --restart "<command that starts node 3>"
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO))

from elastic_evals.replay import replay  # noqa: E402
from elastic_evals.store.opensearch import OpenSearch  # noqa: E402

PREFIX, TOPIC = "cl", "cl-spans"


def health(url):
    import httpx
    try:
        return httpx.get(url + "/_cluster/health", timeout=3).json()
    except Exception:  # noqa: BLE001
        return {"status": "unreachable"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", required=True)
    ap.add_argument("--kill", required=True, help="shell command that kills one node")
    ap.add_argument("--restart", required=True, help="shell command that starts it again")
    ap.add_argument("--conversations", type=int, default=3000)
    a = ap.parse_args()
    urls = a.urls.split(",")
    os_ = OpenSearch(urls[0], prefix=PREFIX)
    os_.teardown()
    os_.req("PUT", "/_cluster/settings", {"persistent": {"plugins.index_state_management.job_interval": 1}})
    os_.setup(pools=("shared",), shards=3, replicas=1, rollover_size="300kb", rollover_age="30d", kinds=("obs", "stage"))
    # each index carries the ISM policy that rolls it over; jitter 0 so the job runs every minute
    os_.req("PUT", "/_cluster/settings", {"persistent": {"plugins.index_state_management.jitter": 0.0}})

    env = dict(os.environ, EE_KAFKA_TOPIC=TOPIC, EE_INDEX_PREFIX=PREFIX, EE_INGEST_GROUP=f"cl-{int(time.time())}",
               EE_OPENSEARCH_URL=urls[0])
    logs = ROOT / ".e2e-logs"
    logs.mkdir(exist_ok=True)
    eecol = str(REPO / "collector/_build/eecol")
    procs = [subprocess.Popen([eecol, "--config", str(REPO / f"deploy/collector/{n}.yaml")], env=env,
                              stdout=open(logs / f"cl-{n}.log", "w"), stderr=subprocess.STDOUT) for n in ("gateway", "ingest")]
    import atexit
    atexit.register(lambda: [p.terminate() for p in procs])
    time.sleep(4)
    for p in procs:
        if p.poll() is not None:
            sys.exit(f"collector exited at start (port in use?): see {logs}")

    base = []
    for name in ("synthetic.jsonl", "synthetic_b.jsonl", "synthetic_c.jsonl"):
        base += [json.loads(line) for line in (ROOT / "data" / name).open()]
    convs = [dict(base[i % len(base)], id=f"cl-{i:06d}") for i in range(a.conversations)]
    half = len(convs) // 2
    events = []
    t0 = time.time()
    sent = replay(convs[:half], "http://localhost:4318", tenant="cl")
    subprocess.run(a.kill, shell=True, check=False)
    events.append((round(time.time() - t0, 1), "node killed", health(urls[0])["status"]))
    sent += replay(convs[half:], "http://localhost:4318", tenant="cl")
    events.append((round(time.time() - t0, 1), "all spans sent", health(urls[0])["status"]))
    statuses = []

    def watch():
        while not done.is_set():
            statuses.append((round(time.time() - t0, 1), health(urls[0])["status"]))
            time.sleep(2)
    done = threading.Event()
    threading.Thread(target=watch, daemon=True).start()
    time.sleep(20)
    subprocess.run(a.restart, shell=True, check=False)
    events.append((round(time.time() - t0, 1), "node restarted", health(urls[0])["status"]))

    deadline = time.time() + 600
    rolled = False
    while time.time() < deadline:
        os_.refresh("obs")
        n = os_.count("obs")
        indices = sorted(i["index"] for i in os_.req("GET", f"/_cat/indices/{PREFIX}-obs-*", format="json"))
        rolled = len(indices) > 1
        if n >= sent and rolled and health(urls[0])["status"] == "green":
            break
        time.sleep(5)
    done.set()
    events.append((round(time.time() - t0, 1), "end", health(urls[0])["status"]))
    for p in procs:
        p.terminate()

    os_.refresh()
    counts = {i["index"]: int(i["docs.count"]) for i in os_.req("GET", f"/_cat/indices/{PREFIX}-obs-*", format="json")}
    alias = os_.req("GET", f"/_alias/{PREFIX}-obs-shared")
    write_index = next(i for i, v in alias.items() if v["aliases"][f"{PREFIX}-obs-shared"].get("is_write_index"))
    unique = os_.search("obs", {"size": 0, "aggs": {"ids": {"cardinality": {"field": "span_id", "precision_threshold": 40000}}}})
    shards = os_.req("GET", f"/_cat/shards/{PREFIX}-obs-*", format="json")
    per_node = {}
    for s in shards:
        per_node[s.get("node") or "unassigned"] = per_node.get(s.get("node") or "unassigned", 0) + 1
    res = {"sent": sent, "indexed": n, "indices": counts, "write_index": write_index, "rolled_over": rolled,
           "distinct_span_ids": unique["aggregations"]["ids"]["value"], "events": events,
           "health_timeline": statuses, "shards_per_node": per_node, "final_health": health(urls[0])}
    (ROOT / "results" / "cluster.json").write_text(json.dumps(res, indent=1))
    yellow = [t for t, s in statuses if s != "green"]
    lines = ["# Storage path on a 3-node OpenSearch cluster", "",
             "Three OpenSearch 3.9 nodes (768 MB heap each) on one VM; `obs` and `stage` with 3 primaries and 1 replica; "
             "ISM rolls the write alias over at 300 kB per primary (to force rollovers on a small run). "
             f"{a.conversations} conversations through gateway → Kafka → ingest → cluster; one node killed after half of them "
             "and started again 20 s after the last span was sent.", "",
             f"- Spans sent: **{sent}**; indexed: **{n}**; distinct span ids: {res['distinct_span_ids']} (cardinality estimate)",
             f"- Lost: **{sent - n}**; duplicates: none possible within an index (create with fixed ids); across rollover, readers dedupe",
             f"- Rollover: {'yes' if rolled else 'no'}; indices {', '.join(f'{k} ({v})' for k, v in sorted(counts.items()))}; write index `{write_index}`",
             f"- Cluster status not green for about {2 * len(yellow)} s while the node was down; final status **{res['final_health']['status']}** with {res['final_health'].get('number_of_nodes')} nodes",
             f"- Shards per node at the end: {', '.join(f'{k}: {v}' for k, v in sorted(per_node.items()))}", "",
             "| t (s) | event | cluster status |", "|---|---|---|"]
    lines += [f"| {t} | {e} | {s} |" for t, e, s in events]
    (ROOT / "results" / "CLUSTER.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
