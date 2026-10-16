"""survey() and run_apply() end to end against a stub shaped like the real NSX.

The unit tests only ever called plan_cluster. These two functions do the fetching,
the matching of clusters to profiles and the write-back, and had never run.
"""
import sys, json
sys.path.insert(0, "..")
import nsx_prep as N

fails = []
def ck(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

OVL = {"id": "tz-ovl", "display_name": "site-overlay", "tz_type": "OVERLAY_STANDARD",
       "path": "/infra/sites/default/enforcement-points/default/transport-zones/tz-ovl"}
VLN = {"id": "tz-vlan", "display_name": "site-vlan", "tz_type": "VLAN_BACKED",
       "path": "/infra/sites/default/enforcement-points/default/transport-zones/tz-vlan"}
POOL = {"id": "pool-tep", "display_name": "site-tep-pool",
        "path": "/infra/ip-pools/pool-tep",
        "subnets": [{"allocation_ranges": [
            {"start": "10.0.70.10", "end": "10.0.70.49"}]}]}
UP = {"id": "up-2nic", "display_name": "two-nic", "mtu": 9000,
      "path": "/infra/host-switch-profiles/up-2nic",
      "teaming": {"active_list": [{"uplink_name": "uplink-1"},
                                  {"uplink_name": "uplink-2"}]}}

def sw(vds, tzs, uplinks=2, pool=None, up=None):
    out = {"host_switch_name": vds, "host_switch_type": "VDS",
           "transport_zone_endpoints": [{"transport_zone_id": t} for t in tzs],
           "uplinks": [{"uplink_name": "uplink-%d" % (i+1)} for i in range(uplinks)]}
    if pool:
        out["ip_assignment_spec"] = {"resource_type": "StaticIpPoolSpec",
                                     "ip_pool_id": pool}
    if up:
        out["host_switch_profile_ids"] = [{"key": "UplinkHostSwitchProfile",
                                           "value": up}]
    return out

def prof(name, sws):
    return {"id": name, "display_name": name,
            "path": "/infra/host-transport-node-profiles/%s" % name,
            "_revision": 3, "__api": "policy",
            "host_switch_spec": {"host_switches": sws}}

REF = prof("prof-ref", [sw("vds-ref", ["tz-ovl", "tz-vlan"], 2, "pool-tep", "up-2nic")])
C1 = prof("prof-a", [sw("vds-a", ["tz-vlan"], 2)])
C2 = prof("prof-b", [sw("vds-b", ["tz-vlan"], 2)])
V2 = prof("prof-d", [sw("vds-d", ["tz-vlan"], 1)])   # one uplink

CC = [
    {"display_name": "cluster-ref", "external_id": "domain-c1", "id": "cc1"},
    {"display_name": "cluster-a", "external_id": "domain-c2", "id": "cc2"},
    {"display_name": "cluster-b", "external_id": "domain-c3", "id": "cc3"},
    {"display_name": "cluster-d",  "external_id": "domain-c4", "id": "cc4"},
    {"display_name": "cluster-none", "external_id": "domain-c5", "id": "cc5"},
]
TNC = [
    {"compute_collection_id": "domain-c1", "transport_node_profile_id": "prof-ref"},
    {"compute_collection_id": "domain-c2", "transport_node_profile_id": "prof-a"},
    {"compute_collection_id": "domain-c3", "transport_node_profile_id": "prof-b"},
    {"compute_collection_id": "domain-c4", "transport_node_profile_id": "prof-d"},
]
# Ids are prefixed per cluster and unambiguous: "tn1" was both a reference node
# and the prefix the stub used to mean "comp01", which made the stub lie.
NODES = ([{"id": "ref-%d" % i, "compute_collection_id": "domain-c1"} for i in range(4)]
         + [{"id": "c1-%d" % i, "compute_collection_id": "domain-c2"} for i in range(4)]
         + [{"id": "c3-%d" % i, "compute_collection_id": "domain-c3"} for i in range(4)]
         + [{"id": "v2-%d" % i, "compute_collection_id": "domain-c4"} for i in range(8)])


class Stub(N.Nsx):
    def __init__(self):
        self.puts = []
        self.profs = {p["id"]: json.loads(json.dumps(p)) for p in (REF, C1, C2, V2)}

    def clusters(self): return CC
    def collections(self): return TNC
    def profiles(self): return [json.loads(json.dumps(v)) for v in self.profs.values()]
    def transport_zones(self): return [OVL, VLN]
    def ip_pools(self): return [POOL]
    def uplink_profiles(self): return [UP]
    def transport_nodes(self): return NODES
    def pool_allocations(self, pool): return 4          # 40 total, 4 used, 36 free
    def cluster_hosts(self, coll):
        return {"domain-c1": 4, "domain-c2": 4, "domain-c3": 4, "domain-c4": 8}.get(
            str(coll.get("external_id")))

    def nodes_in(self, collection_id):
        # None = this NSX cannot filter by collection, so the client-side match
        # is used. A separate stub below covers the server-side path.
        return None

    def node_state(self, node_id):
        # Only the reference cluster's hosts have TEPs - exactly the real symptom.
        if node_id.startswith("ref-"):
            return {"host_switch_states": [{"endpoints": [{"ip": "10.0.70.11"}]}]}
        return {"host_switch_states": [{"endpoints": [{}]}]}

    def put_profile(self, profile):
        body = {k: v for k, v in profile.items() if k != "__api"}
        self.puts.append(body)
        self.profs[body["id"]] = json.loads(json.dumps(body))
        return {}


import io, contextlib
s = Stub()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rows = N.survey(s)
out = buf.getvalue()

ck("survey returns a row per cluster NSX knows", len(rows) == 5)
ck("survey marks the reference as having overlay",
   any(r["cluster"] == "cluster-ref" and r["overlay"] for r in rows))
ck("survey marks the imported clusters as not",
   all(not r["overlay"] for r in rows
       if r["profile"] and r["cluster"] != "cluster-ref"))
ck("survey reports the cluster with no profile rather than dropping it",
   any(r["cluster"] == "cluster-none" and r["profile"] is None for r in rows))
ck("survey shows TEP coverage, the symptom people actually see",
   "4/4" in out and "0/4" in out and "0/8" in out)
ck("every cluster with hosts is counted, none silently zero",
   all(r["hosts"] for r in rows if r["profile"]))
ck("survey shows the uplink count, so the one-nic cluster is visible up front",
   any(r["cluster"] == "cluster-d" and r["uplinks"] == 1 for r in rows))

# ---- plan: nothing may be written
s2 = Stub()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.run_apply(s2, "cluster-ref", ["cluster-a", "cluster-b", "cluster-d"], False)
ck("a plan writes nothing at all", s2.puts == [])
ck("the plan reads the reference's zone, uplink profile and pool",
   any("site-overlay" in d and "two-nic" in d and "site-tep-pool" in d
       for _st, d in res.rows))
ck("the plan counts the pool: 40 minus 4 allocated",
   any("36 of 40 addresses free" in d for _st, d in res.rows))
ck("the one-uplink cluster is blocked in the plan",
   any(st == N.BLOCKED and "cluster-d" in d and "uplinks" in d
       for st, d in res.rows))
ck("and the two good clusters are planned as adds",
   sum(1 for st, d in res.rows if st == N.ADD and "overlay transport zone" in d) == 2)
ck("a run holding a blocked cluster reports blocked overall",
   res.state == N.BLOCKED)

# ---- commit
s3 = Stub()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.run_apply(s3, "cluster-ref", ["cluster-a", "cluster-b"], True)
ck("commit writes exactly the two clusters named", len(s3.puts) == 2)
ck("each write carries the overlay zone",
   all(any("tz-ovl" in str(e) for e in
           p["host_switch_spec"]["host_switches"][0]["transport_zone_endpoints"])
       for p in s3.puts))
ck("each write carries the TEP pool and uplink profile",
   all(N.sw_pool(p["host_switch_spec"]["host_switches"][0]) == "pool-tep"
       and N.sw_uplink_profile(p["host_switch_spec"]["host_switches"][0]) == "up-2nic"
       for p in s3.puts))
ck("the revision is carried back so NSX accepts the write",
   all(p.get("_revision") == 3 for p in s3.puts))
ck("the vlan zone the cluster already had is not dropped",
   all(len(p["host_switch_spec"]["host_switches"][0]["transport_zone_endpoints"]) == 2
       for p in s3.puts))
ck("the read-back confirms it rather than trusting the write",
   all(st != N.BLOCKED for st, d in res.rows if "applied" in d)
   and sum(1 for st, d in res.rows if st == N.CHANGE and "applied" in d) == 2)
ck("the reference itself is never written", "prof-ref" not in
   [p["id"] for p in s3.puts])

# ---- re-run after a successful commit is a no-op
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.run_apply(s3, "cluster-ref", ["cluster-a", "cluster-b"], True)
ck("running it again changes nothing - it is idempotent", len(s3.puts) == 2)
ck("and says both clusters have the zone and the pool",
   sum(1 for st, d in res.rows
       if st == N.OK and "overlay transport zone and a TEP pool" in d) == 2)

# ---- a bad reference
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.run_apply(Stub(), "cluster-a", ["cluster-b"], True)
ck("a reference with no overlay is refused before anything is written",
   res.state == N.BLOCKED and any("pick a cluster whose overlay" in d
                                  for _st, d in res.rows))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.run_apply(Stub(), "nope", ["cluster-a"], True)
ck("an unknown reference names the survey command",
   res.state == N.BLOCKED and any("Run survey" in d for _st, d in res.rows))



# ===================== verify(), end to end =========================
EDGES = [{"id": "edge11", "display_name": "edge-01"},
         {"id": "edge12", "display_name": "edge-02"}]

class VerifyStub(Stub):
    """Hosts have TEPs; whether tunnels are up and whether the pool has a
    gateway are the knobs, because those are the two real failure modes."""
    def __init__(self, teps=True, tunnels_up=True, gateway=True,
                 edge_subnet_differs=True):
        Stub.__init__(self)
        self.teps, self.tunnels_up = teps, tunnels_up
        self.gateway, self.differs = gateway, edge_subnet_differs
        # comp01 has the overlay applied already
        sw = self.profs["prof-a"]["host_switch_spec"]["host_switches"][0]
        sw["transport_zone_endpoints"].append({"transport_zone_id": "tz-ovl"})
        sw["ip_assignment_spec"] = {"ip_pool_id": "pool-tep"}

    def edge_nodes(self): return EDGES

    def ip_pools(self):
        sub = {"cidr": "10.0.21.0/24",
               "allocation_ranges": [{"start": "10.0.21.10",
                                      "end": "10.0.21.49"}]}
        if self.gateway:
            sub["gateway_ip"] = "10.0.21.254"
        return [{"id": "pool-tep", "display_name": "tep-pool-hosts",
                 "path": "/infra/ip-pools/pool-tep", "subnets": [sub]}]

    def node_state(self, node_id):
        if node_id.startswith("edge"):
            ip = "10.0.23.11" if self.differs else "10.0.21.30"
            return {"host_switch_states": [{"endpoints": [{"ip": ip}]}]}
        if node_id.startswith("c1-") and self.teps:
            return {"host_switch_states": [{"endpoints": [{"ip": "10.0.21.11"}]}]}
        if node_id.startswith("ref-"):
            return {"host_switch_states": [{"endpoints": [{"ip": "10.0.21.20"}]}]}
        return {"host_switch_states": [{"endpoints": [{}]}]}

    def tunnels(self, node_id):
        st = "UP" if self.tunnels_up else "DOWN"
        return [{"name": "t1", "status": st, "remote_ip": "10.0.23.11",
                 "remote_node_display_name": "edge-01"}]


def run_verify(**kw):
    st = VerifyStub(**kw)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return N.verify(st, ["cluster-a"]), buf.getvalue()

res, _ = run_verify()
ck("verify passes a cluster with TEPs, tunnels up and a pool gateway",
   res.state == N.OK)
ck("and reports the edge tunnel endpoints it compared against",
   any("edge node(s)" in d and "10.0.23.11" in d for _s, d in res.rows))

res, _ = run_verify(teps=False)
ck("a host with no TEP address is blocked, not passed",
   res.state == N.BLOCKED and any("no TEP address" in d for _s, d in res.rows))

res, _ = run_verify(tunnels_up=False)
ck("tunnels down are blocked and the far end is named",
   res.state == N.BLOCKED
   and any("tunnels down" in d and "edge-01" in d for _s, d in res.rows))

# The 1 Oct failure, reproduced: hosts 10.0.21.x, edges 10.0.23.x, no gateway.
res, _ = run_verify(gateway=False)
ck("a routed tunnel subnet with no pool gateway is caught even when tunnels read up",
   res.state == N.BLOCKED
   and any("no gateway" in d and "reach no edge" in d for _s, d in res.rows))
ck("and it names the pool to fix",
   any("tep-pool-hosts" in d for _s, d in res.rows))

res, _ = run_verify(gateway=False, edge_subnet_differs=False)
ck("the same pool with the edges on its own subnet is not blocked",
   res.state == N.OK)

st = VerifyStub()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.verify(st, ["cluster-b"])
ck("a cluster with no overlay yet is skipped, not failed",
   res.state != N.BLOCKED
   and any(s == N.SKIP and "no overlay transport zone" in d for s, d in res.rows))

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.verify(st, ["ghost"])
ck("an unknown cluster name is reported", res.state == N.BLOCKED
   and any("no cluster called ghost" in d for _s, d in res.rows))



# ===================== fabric(): where the TEP VLANs live ============
class FabricStub(Stub):
    def uplink_profiles(self):
        return [dict(UP, transport_vlan=221, mtu=9000),
                {"id": "up-default", "display_name": "nsx-default-uplink-profile",
                 "path": "/infra/host-switch-profiles/up-default",
                 "transport_vlan": 0, "mtu": 1700,
                 "teaming": {"active_list": [{"uplink_name": "u1"},
                                             {"uplink_name": "u2"}]}}]

    def ip_pools(self):
        return [{"id": "pool-tep", "display_name": "tep-pool-hosts",
                 "path": "/infra/ip-pools/pool-tep",
                 "subnets": [{"cidr": "10.0.21.0/24",
                              "gateway_ip": "10.0.21.254",
                              "allocation_ranges": [{"start": "10.0.21.10",
                                                     "end": "10.0.21.49"}]}]},
                {"id": "pool-nogw", "display_name": "tep-pool-nogw",
                 "path": "/infra/ip-pools/pool-nogw",
                 "subnets": [{"cidr": "10.0.23.0/24",
                              "allocation_ranges": [{"start": "10.0.23.10",
                                                     "end": "10.0.23.19"}]}]}]

    def pool_allocations(self, pool):
        return 4


fs = FabricStub()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ups, pools = N.fabric(fs)
out = buf.getvalue()

ck("fabric lists every uplink profile with its transport VLAN",
   "221" in out and "nsx-default-uplink-profile" in out)
ck("a profile on VLAN 0 is shown as 0, not blank",
   any(line.split()[-2:] and " 0 " in line
       for line in out.splitlines() if "default-uplink" in line))
ck("it names which clusters use each profile, so a shared default is visible",
   "cluster-ref" in out)
ck("pools are listed with subnet and gateway",
   "10.0.21.0/24" in out and "10.0.21.254" in out)
ck("a pool with no gateway reads NONE rather than blank",
   "NONE" in out)
ck("free count is total minus allocated",
   "36/40" in out and "6/10" in out)
ck("the allocation range is printed, for sizing the next cluster",
   "10.0.21.10-10.0.21.49" in out)

total, used, free = N.pool_numbers(fs, fs.ip_pools()[0])
ck("pool_numbers returns total, used and free", (total, used, free) == (40, 4, 36))


class NoAlloc(FabricStub):
    def pool_allocations(self, pool):
        return None


t, u, f = N.pool_numbers(NoAlloc(), fs.ip_pools()[0])
ck("an unreadable allocation count gives free=None, not a wrong number",
   t == 40 and u is None and f is None)



# ============ --pool must send a policy path, not a bare id ============
# NSX answered "400 The path=[<name>] is invalid" to a bare id. The reference's
# own working spec uses the path form, so match that rather than guessing.
class PathPool(Stub):
    """Reference carries a path-form pool, as a real policy profile does."""
    def __init__(self):
        Stub.__init__(self)
        ref = self.profs["prof-ref"]["host_switch_spec"]["host_switches"][0]
        ref["ip_assignment_spec"] = {"resource_type": "StaticIpPoolSpec",
                                     "ip_pool_id": "/infra/ip-pools/pool-tep"}

    def ip_pools(self):
        return [{"id": "tep-222", "display_name": "tep-222",
                 "path": "/infra/ip-pools/tep-222",
                 "subnets": [{"cidr": "10.0.22.0/24",
                              "gateway_ip": "10.0.22.254",
                              "allocation_ranges": [{"start": "10.0.22.201",
                                                     "end": "10.0.22.250"}]}]}]


pp = PathPool()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.run_apply(pp, "cluster-ref", ["cluster-a"], True, tep_pool="tep-222")
sent = pp.puts[0]["host_switch_spec"]["host_switches"][0] if pp.puts else {}
ck("a named pool is sent as its policy path, not its bare id",
   N.sw_pool(sent) == "/infra/ip-pools/tep-222")
ck("and the write was accepted rather than refused", len(pp.puts) == 1)


class IdPool(PathPool):
    """A manager-API style reference using a bare id - then a bare id is right."""
    def __init__(self):
        PathPool.__init__(self)
        ref = self.profs["prof-ref"]["host_switch_spec"]["host_switches"][0]
        ref["ip_assignment_spec"] = {"resource_type": "StaticIpPoolSpec",
                                     "ip_pool_id": "pool-tep"}


ip2 = IdPool()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    N.run_apply(ip2, "cluster-ref", ["cluster-a"], True, tep_pool="tep-222")
sent = ip2.puts[0]["host_switch_spec"]["host_switches"][0] if ip2.puts else {}
ck("a bare-id reference keeps the bare-id spelling",
   N.sw_pool(sent) == "tep-222")
ck("the resource_type from the working reference is carried over",
   (sent.get("ip_assignment_spec") or {}).get("resource_type") == "StaticIpPoolSpec")



# ================= build(): one config, resumable from anywhere ==========
CFG = {
    "nsx": "nsx01.example.com",
    "reference": "cluster-ref",
    "pools": {"tep-222": {"cidr": "10.0.22.0/24",
                          "range": "10.0.22.201-10.0.22.250",
                          "gateway": "10.0.22.254"}},
    "clusters": {
        "cluster-a": {"vlan": 221, "pool": "tep-221"},
        "cluster-b": {"vlan": 222, "pool": "tep-222"},
        "cluster-d": {"vlan": 222, "pool": "tep-222"},
    },
}


class BuildStub(Stub):
    """A whole estate: pools that can be created, uplink profiles per cluster,
    profiles that take a pool. Records every write so re-runs can be compared."""

    def __init__(self, with_222=False, vlans=None):
        Stub.__init__(self)
        self.created, self.uplinks_put = [], []
        self.pools = {"tep-221": {
            "id": "tep-221", "display_name": "tep-221",
            "path": "/infra/ip-pools/tep-221",
            "subnets": [{"cidr": "10.0.21.0/24", "gateway_ip": "10.0.21.254",
                         "allocation_ranges": [{"start": "10.0.21.201",
                                                "end": "10.0.21.250"}]}]}}
        if with_222:
            self.pools["tep-222"] = {
                "id": "tep-222", "display_name": "tep-222",
                "path": "/infra/ip-pools/tep-222",
                "subnets": [{"cidr": "10.0.22.0/24", "gateway_ip": "10.0.22.254",
                             "allocation_ranges": [{"start": "10.0.22.201",
                                                    "end": "10.0.22.250"}]}]}
        start = {"cluster-ref": 221, "cluster-a": 0, "cluster-b": 0,
                 "cluster-d": 0, "cluster-none": 0}
        start.update(vlans or {})
        self.ups = {}
        for cl, v in start.items():
            self.ups["up-" + cl] = {
                "id": "up-" + cl, "display_name": "up-" + cl,
                "path": "/infra/host-switch-profiles/up-" + cl,
                "transport_vlan": v, "mtu": 9000,
                "teaming": {"active_list": [{"uplink_name": "u1"},
                                            {"uplink_name": "u2"}]}}
        for cl, pid in (("prof-ref", "cluster-ref"), ("prof-a", "cluster-a"),
                        ("prof-b", "cluster-b"), ("prof-d", "cluster-d")):
            sw = self.profs[cl]["host_switch_spec"]["host_switches"][0]
            sw["uplinks"] = [{"uplink_name": "u1"}, {"uplink_name": "u2"}]
            sw["host_switch_profile_ids"] = [
                {"key": "UplinkHostSwitchProfile", "value": "up-" + pid}]
            if cl == "prof-ref":
                sw["ip_assignment_spec"] = {"resource_type": "StaticIpPoolSpec",
                                            "ip_pool_id": "/infra/ip-pools/tep-221"}
            else:
                sw["ip_assignment_spec"] = {"resource_type": "NoIpv4"}
                sw["transport_zone_endpoints"] = [{"transport_zone_id": "tz-ovl"},
                                                  {"transport_zone_id": "tz-vlan"}]

    def ip_pools(self):
        return [json.loads(json.dumps(v)) for v in self.pools.values()]

    def uplink_profiles(self):
        return [json.loads(json.dumps(v)) for v in self.ups.values()]

    def cluster_hosts(self, coll):
        return 4

    def pool_allocations(self, pool):
        return 0

    def create_pool(self, pid, name, cidr, start, end, gateway):
        self.created.append(name)
        self.pools[pid] = {"id": pid, "display_name": name,
                           "path": "/infra/ip-pools/" + pid,
                           "subnets": [{"cidr": cidr, "gateway_ip": gateway,
                                        "allocation_ranges": [{"start": start,
                                                               "end": end}]}]}

    def put_uplink_profile(self, prof):
        self.uplinks_put.append((prof["id"], prof.get("transport_vlan")))
        self.ups[prof["id"]] = json.loads(json.dumps(prof))

    def nodes_in(self, collection_id):
        return None

    def node_state(self, node_id):
        return {"host_switch_states": [{"endpoints": [{"ip": "10.0.21.11"}]}]}

    def edge_nodes(self):
        return [{"id": "edge11", "display_name": "edge-01"}]

    def tunnels(self, node_id):
        return [{"name": "t1", "status": "UP", "remote_ip": "10.0.21.30",
                 "remote_node_display_name": "edge-01"}]


import time as _clock
_clock.sleep = lambda _s: None      # the build waits on NSX; the stub answers now


def build(st, commit=True):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = N.run_build(st, CFG, commit)
    return res, buf.getvalue()

# --- from nothing
st = BuildStub()
res, out = build(st)
ck("build creates the missing pool and only that one",
   st.created == ["tep-222"])
ck("build sets every transport VLAN that is wrong",
   sorted(st.uplinks_put) == [("up-cluster-a", 221), ("up-cluster-b", 222),
                              ("up-cluster-d", 222)])
ck("build points all three clusters at their pools", len(st.puts) == 3)
ck("cluster-a gets the 221 pool and the others the 222 one",
   {p["id"]: N.sw_pool(p["host_switch_spec"]["host_switches"][0])
    for p in st.puts} ==
   {"prof-a": "/infra/ip-pools/tep-221", "prof-b": "/infra/ip-pools/tep-222",
    "prof-d": "/infra/ip-pools/tep-222"})
ck("and it verifies at the end rather than trusting the writes",
   "==== verify" in out)

# --- the idempotency claim: run it again, nothing happens
before = (len(st.created), len(st.uplinks_put), len(st.puts))
res2, out2 = build(st)
ck("a second run creates no pool, sets no VLAN and writes no profile",
   (len(st.created), len(st.uplinks_put), len(st.puts)) == before)
ck("and it says each cluster already has its zone and pool",
   out2.count("nothing to add") == 3)
ck("a clean re-run is not blocked", res2.state != N.BLOCKED)

# --- resuming from half-done states
half = BuildStub(with_222=True)          # pool already there
res, out = build(half)
ck("a pool that already exists is not created again", half.created == [])
ck("but the rest still runs", len(half.puts) == 3)

vl = BuildStub(vlans={"cluster-a": 221, "cluster-b": 222, "cluster-d": 222})
res, out = build(vl)
ck("VLANs already correct are left alone", vl.uplinks_put == [])
ck("and the pool and assignments still happen",
   vl.created == ["tep-222"] and len(vl.puts) == 3)

# --- a plan changes nothing at all
dry = BuildStub()
res, out = build(dry, commit=False)
ck("a plan creates no pool, sets no VLAN, writes no profile",
   (dry.created, dry.uplinks_put, dry.puts) == ([], [], []))
ck("and it counts what is pending",
   "1 pool(s), 3 transport VLAN(s), 3 TEP assignment(s)" in out)

# --- one cluster failing must not stop the others
class OneBad(BuildStub):
    def put_profile(self, profile):
        if profile["id"] == "prof-b":
            raise RuntimeError("400 something wrong with this one")
        return BuildStub.put_profile(self, profile)

bad = OneBad()
res, out = build(bad)
ck("one cluster refused does not stop the others",
   sorted(p["id"] for p in bad.puts) == ["prof-a", "prof-d"])
ck("the run reports blocked so nobody reads it as success",
   res.state == N.BLOCKED)
bad2 = BuildStub()
bad2.profs = bad.profs
bad2.pools = bad.pools
bad2.ups = bad.ups
res3, out3 = build(bad2)
ck("re-running after a partial failure does only what is left",
   [p["id"] for p in bad2.puts] == ["prof-b"] and bad2.created == []
   and bad2.uplinks_put == [])



# A plan must show the whole job, not block on work the same plan would do first.
dry2 = BuildStub()
res, out = build(dry2, commit=False)
ck("a plan plans the assignments too, not just the pool and VLANs",
   "3 TEP assignment(s)" in out)
ck("it says a pool it would create does not exist yet, rather than failing",
   "does not exist yet - planning against the one this run would create" in out)
ck("and no cluster is blocked by work earlier in the same plan",
   not any(st == N.BLOCKED for st, _d in res.rows))
ck("still nothing written on a plan",
   (dry2.created, dry2.uplinks_put, dry2.puts) == ([], [], []))



# NSX filtering server-side, and the caveat when nothing can be matched at all.
class ServerMatch(VerifyStub):
    def __init__(self):
        VerifyStub.__init__(self)
        self.asked = []

    def nodes_in(self, collection_id):
        self.asked.append(collection_id)
        return [{"id": "c1-%d" % i} for i in range(4)] if collection_id else []


sm = ServerMatch()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.verify(sm, ["cluster-a"])
ck("when NSX can filter by collection, it is asked rather than matched here",
   sm.asked and res.state == N.OK)


class NoMatch(VerifyStub):
    def nodes_in(self, collection_id):
        return []

    def transport_nodes(self):
        return NODES


nm = NoMatch()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.verify(nm, ["cluster-a"])
ck("every cluster unmatched reads as one caveat, not four findings",
   res.state != N.BLOCKED
   and sum(1 for st, d in res.rows if st == N.SKIP and "unverified" in d) == 1)
ck("and it says the configuration still applied",
   any("configuration above still applied" in d for _s, d in res.rows))



# ===== host preparation state, and the retry that clears it =====
class PrepFailed(VerifyStub):
    def __init__(self, state="Preparation Failed"):
        VerifyStub.__init__(self)
        self.state_str, self.retried = state, []

    def collection_state(self, tnc_id):
        return {"state": self.state_str, "aggregate_progress_percentage": 48}

    def retry_realization(self, tnc_id):
        self.retried.append(tnc_id)
        return {}


pf = PrepFailed()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.verify(pf, ["cluster-a"])
ck("verify reports a failed host preparation instead of going quiet",
   res.state == N.BLOCKED
   and any("host preparation 'Preparation Failed'" in d for _s, d in res.rows))
ck("and names the command that re-runs it",
   any("realize --cluster cluster-a --commit" in d for _s, d in res.rows))
ck("the progress NSX reports comes through",
   any("48" in d for _s, d in res.rows))

ip_ = PrepFailed(state="In Progress")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.verify(ip_, ["cluster-a"])
ck("a preparation still running is a caveat, not a failure",
   res.state != N.BLOCKED
   and any(s == N.SKIP and "In Progress" in d for s, d in res.rows))

ok_ = PrepFailed(state="success")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.verify(ok_, ["cluster-a"])
ck("a healthy preparation says nothing about itself",
   not any("host preparation" in d for _s, d in res.rows))

rt = PrepFailed()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.run_realize(rt, ["cluster-a"], True)
ck("realize reapplies the profile to the cluster", len(rt.retried) == 1)
ck("and says it takes minutes rather than implying it is done",
   any("several minutes" in d for _s, d in res.rows))

rp = PrepFailed()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.run_realize(rp, ["cluster-a"], False)
ck("a plan retries nothing", rp.retried == [])

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = N.run_realize(PrepFailed(), ["ghost"], True)
ck("an unknown cluster is reported, not retried", res.state == N.BLOCKED)

print("\n%d failed" % len(fails))
sys.exit(1 if fails else 0)
