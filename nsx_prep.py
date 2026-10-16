#!/usr/bin/env python3
"""nsx_prep.py - put the overlay onto clusters that a VCF import left VLAN-only.

A vCenter imported into VCF as a workload domain has its clusters prepared for NSX
VLAN-backed networking and nothing else. No overlay transport zone, so no TEP, no
tunnels, and none of the site's existing overlay segments reach the new cluster.
That is expected behaviour by design - Broadcom KB 444225 - and the remedy it gives
is to edit the transport node profile by hand, in the UI, once per cluster.

Six clusters is six chances to mistype a teaming policy on a live fabric, so this
does it from the configuration that already works instead. Point --reference at a
cluster whose overlay is up; it reads that cluster's overlay transport zone, uplink
profile and TEP pool and puts the same ones on the clusters that are missing them.

Nothing is invented. If the reference cluster does not have an overlay transport
zone, this says so and stops rather than guessing at one.

  python3 nsx_prep.py example > nsx.yaml
  python3 nsx_prep.py build --config nsx.yaml            plan; changes nothing
  python3 nsx_prep.py build --config nsx.yaml --commit   do all of it
      The whole end state in one file: the pools to exist, each cluster's
      transport VLAN and TEP pool. It creates missing pools, sets the VLANs,
      points the clusters at their pools and verifies, in that order, skipping
      whatever is already right. Safe to re-run at any point - it reads the live
      state every time and does only what is left, so a run that failed halfway
      is finished by running it again.

  python3 nsx_prep.py survey --nsx nsx01.example.com
      every cluster NSX knows about and where each one stands. Changes nothing.

  python3 nsx_prep.py apply --nsx nsx01.example.com \
      --reference <cluster-with-overlay> --cluster cluster-a --cluster cluster-b
      what it would change. Add --commit to do it.

      It also lists every uplink profile with its transport VLAN, and every IP
      pool with its subnet, gateway and free count - which is where the TEP VLANs
      and TEP subnets actually live.

  python3 nsx_prep.py verify --nsx nsx01.example.com [--cluster <name>]
      did it work: TEP addresses, tunnel state host to edge, and the pool gateway
      behind them. An applied overlay zone is not a working overlay. Run after
      apply --commit does it for you, and any time afterwards.

  python3 nsx_prep.py vlan --nsx nsx01.example.com --cluster <name> --vlan <n>
      sets the transport VLAN on that cluster's own uplink profile. A cluster
      a VCF import prepared carries 0 - untagged - and its hosts never get a
      usable TEP. Refuses a profile shared with clusters you did not name.

  python3 nsx_prep.py pool --nsx nsx01.example.com --name <name> \
      --cidr <subnet> --range <start-end> --gateway <addr>
      creates a TEP pool. Checks the range and gateway sit inside the subnet,
      that the gateway is not inside the range, and warns when another pool
      already serves that subnet - two managers handing out one /24 is how
      duplicate TEPs happen.

  python3 nsx_prep.py gateway --nsx nsx01.example.com \
      --pool <pool verify named> --gateway <the leaves' address in that subnet>
      puts a gateway on a TEP pool that has none. Add --commit. Without one, when
      the edges sit on another tunnel subnet, the hosts reach no edge at all.

  python3 nsx_prep.py selftest
      no network; checks the merge, the guards and the verification.

The NSX password is asked for by name and never written anywhere. Run output goes
to /tmp/nsx_prep/<timestamp>/console.txt, which holds no credentials.
"""

import argparse
import base64
import getpass
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0"

OK, ADD, CHANGE, BLOCKED, SKIP = "ok", "add", "change", "blocked", "skip"
RANK = {OK: 0, SKIP: 1, ADD: 2, CHANGE: 2, BLOCKED: 3}


def die(msg):
    sys.stderr.write("ERROR: %s\n" % msg)
    sys.exit(1)


class Tee:
    """The screen, and the run's console.txt beside it."""

    def __init__(self, path):
        self.f = open(path, "w")
        self.out = sys.stdout

    def write(self, text):
        self.out.write(text)
        self.f.write(text)

    def flush(self):
        self.out.flush()
        self.f.flush()


class Result:
    def __init__(self, step):
        self.step, self.rows = step, []

    def add(self, state, detail):
        self.rows.append((state, detail))
        return self

    @property
    def state(self):
        return max([r[0] for r in self.rows], key=lambda s: RANK[s]) if self.rows else OK

    @property
    def summary(self):
        worst = self.state
        hits = [d for s, d in self.rows if s == worst]
        return "; ".join(hits[:3]) + (" (+%d more)" % (len(hits) - 3) if len(hits) > 3 else "")


# -------------------------------------------------------------- the config
# Lifted from deploy_vc, where the same lesson was learned: a run with eight
# flags on the command line is eight chances to get one wrong, and every mistake
# is a round trip. Declare the end state once, plan it, commit it.

def parse_yaml(text):
    """The documented subset: nested maps by indent, block and inline lists, lists of maps,
    # comments, optional quotes. Used when PyYAML is not installed."""
    root = {}
    stack = [{"indent": -1, "container": root, "parent": None, "key": None}]

    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()

        if body.startswith("- "):
            while len(stack) > 1 and indent < stack[-1]["indent"]:
                stack.pop()
            frame = stack[-1]
            cont = frame["container"]
            if isinstance(cont, dict):                 # the key's container is really a list
                if cont or frame["parent"] is None:
                    raise ValueError("list item where a mapping was expected: %s" % body)
                cont = []
                frame["parent"][frame["key"]] = cont
                frame["container"] = cont
            item = body[2:].strip()
            if ":" in item and item[0] not in "\"'":
                k, _, v = item.partition(":")
                entry = {}
                if v.strip():
                    entry[k.strip()] = _scalar(v)
                cont.append(entry)
                stack.append({"indent": indent, "container": entry, "parent": None, "key": None})
            else:
                cont.append(_scalar(item))
            continue

        while len(stack) > 1 and indent <= stack[-1]["indent"]:
            stack.pop()
        cont = stack[-1]["container"]
        if not isinstance(cont, dict):
            raise ValueError("mapping key inside a list: %s" % body)
        key, _, val = body.partition(":")
        key = key.strip()
        if val.strip():
            cont[key] = _scalar(val)
        else:
            child = {}
            cont[key] = child
            stack.append({"indent": indent, "container": child, "parent": cont, "key": key})
    return root


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.lower() in ("null", "~", ""):
        return None
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [_scalar(x) for x in inner.split(",")] if inner else []
    try:
        return int(v)
    except ValueError:
        return v


def _strip_comment(line):
    """Drop a trailing # comment, but not a # inside quotes."""
    quote = None
    for i, c in enumerate(line):
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


KNOWN_KEYS = {"nsx", "user", "reference", "pools", "clusters"}
POOL_KEYS = {"cidr", "range", "gateway"}
CLUSTER_KEYS = {"vlan", "pool"}


def load_config(path):
    with open(path) as f:
        text = f.read()
    try:
        import yaml
        cfg = yaml.safe_load(text)
    except ImportError:
        cfg = parse_yaml(text)
    if not isinstance(cfg, dict):
        die("%s did not parse as a mapping" % path)
    for key in cfg:
        if key not in KNOWN_KEYS:
            die("%s: '%s' is not a name this tool knows. Known: %s"
                % (path, key, ", ".join(sorted(KNOWN_KEYS))))
    for name, body in (cfg.get("pools") or {}).items():
        for key in (body or {}):
            if key not in POOL_KEYS:
                die("%s: pools.%s.%s is not known. Known: %s"
                    % (path, name, key, ", ".join(sorted(POOL_KEYS))))
        for need in ("cidr", "range"):
            if not (body or {}).get(need):
                die("%s: pools.%s needs %s" % (path, name, need))
    for name, body in (cfg.get("clusters") or {}).items():
        for key in (body or {}):
            if key not in CLUSTER_KEYS:
                die("%s: clusters.%s.%s is not known. Known: %s"
                    % (path, name, key, ", ".join(sorted(CLUSTER_KEYS))))
    if not cfg.get("clusters"):
        die("%s: no clusters: block, so there is nothing to do" % path)
    if not cfg.get("reference"):
        die("%s: reference: is required - the cluster whose overlay config to copy"
            % path)
    return cfg


CONFIG_EXAMPLE = """# nsx_prep config. Everything the four clusters need, declared once.
#
#   python3 nsx_prep.py build --config this.yaml            plan, changes nothing
#   python3 nsx_prep.py build --config this.yaml --commit   do it
#
# It runs in order: create any missing pool, set each transport VLAN, point each
# cluster at its pool, then verify. Each step is skipped where it is already right,
# so a re-run after a partial failure picks up where it stopped.

nsx: nsx01.example.com
user: admin                    # optional, admin by default

# The cluster whose working overlay config is copied: its transport zone, and
# its uplink profile for any cluster that has none.
reference: cluster-ref

# Pools to create if absent. One per TEP VLAN. Left out, nothing is created.
pools:
  tep-222-leaf03-04:
    cidr: 10.0.22.0/24
    range: 10.0.22.201-10.0.22.250
    gateway: 10.0.22.254       # the address the leaves hold. Never guessed

# What each cluster should end up with.
clusters:
  cluster-a:
    vlan: 221
    pool: tep-221-leaf01-02    # already exists
  cluster-b:
    vlan: 222
    pool: tep-222-leaf03-04
"""


# ----------------------------------------------------------------- the API
class Nsx:
    """NSX Manager over its REST API.

    Both the policy and the manager API are used: policy for the transport node
    profiles, which is where NSX 4.1 and later keep them, and manager for the
    compute collections and transport node state, which policy does not expose.
    Where a policy path is missing on an older build the manager path is tried,
    so this works against 4.x and 9.x without being told which.
    """

    def __init__(self, host, user, password, timeout=60):
        self.host = host
        self.timeout = timeout
        self.ctx = ssl._create_unverified_context()
        self.auth = "Basic " + base64.b64encode(
            ("%s:%s" % (user, password)).encode()).decode()
        self.get("/api/v1/node")          # fails now, with a clear message, not later

    def _request(self, method, path, body=None, timeout=None):
        url = "https://%s%s" % (self.host, path)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, method=method, data=data)
        req.add_header("Authorization", self.auth)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(
                req, context=self.ctx, timeout=timeout or self.timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else None

    def call(self, method, path, body=None, timeout=None):
        try:
            return self._request(method, path, body, timeout)
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode()
            except Exception:
                pass
            nice = ""
            try:
                doc = json.loads(raw)
                nice = doc.get("error_message") or doc.get("message") or ""
                for rel in (doc.get("related_errors") or []):
                    extra = rel.get("error_message") or ""
                    if extra:
                        nice += " | " + extra
            except Exception:
                pass
            if e.code == 401:
                raise RuntimeError("NSX refused the credentials (401). Check the user "
                                   "and password for %s" % self.host)
            raise RuntimeError("%s %s: %s %s" % (method, path, e.code,
                                                 (nice or raw)[:600]))
        except urllib.error.URLError as e:
            raise RuntimeError("cannot reach %s: %s" % (self.host, e.reason))

    def get(self, path, timeout=None):
        return self.call("GET", path, timeout=timeout)

    def first(self, paths):
        """The first of these paths that answers. NSX moved several of these."""
        last = None
        for p in paths:
            try:
                return self.get(p), p
            except RuntimeError as e:
                last = e
                if "404" not in str(e) and "405" not in str(e):
                    raise
        raise last

    # -- inventory ---------------------------------------------------------
    def clusters(self):
        """Compute collections: the vCenter clusters NSX can see."""
        doc = self.get("/api/v1/fabric/compute-collections") or {}
        return doc.get("results") or []

    def collections(self):
        """Transport node collections: which profile is applied to which cluster."""
        doc = self.get("/api/v1/transport-node-collections") or {}
        return doc.get("results") or []

    def profiles(self):
        doc, used = self.first(["/policy/api/v1/infra/host-transport-node-profiles",
                                "/api/v1/transport-node-profiles"])
        out = (doc or {}).get("results") or []
        # Remember which API answered. A profile read from the manager API cannot be
        # written back to the policy one, and the 404 would arrive only on --commit.
        for prof in out:
            prof["__api"] = "policy" if used.startswith("/policy") else "manager"
        return out

    def transport_zones(self):
        doc, _p = self.first([
            "/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones",
            "/api/v1/transport-zones"])
        return (doc or {}).get("results") or []

    def ip_pools(self):
        """IP pools, with their subnets filled in.

        The policy API lists a pool without its subnets - those are a child
        resource. Listing alone therefore shows every pool with no CIDR, no
        gateway and no size, which is every fact worth having about a TEP pool.
        """
        doc, used = self.first(["/policy/api/v1/infra/ip-pools",
                                "/api/v1/pools/ip-pools"])
        out = (doc or {}).get("results") or []
        if not used.startswith("/policy"):
            return out                       # manager API returns them inline
        for pool in out:
            if pool.get("subnets"):
                continue
            pid = pool.get("id") or str(pool.get("path", "")).rsplit("/", 1)[-1]
            if not pid:
                continue
            try:
                kids = self.get("/policy/api/v1/infra/ip-pools/%s/ip-subnets"
                                % urllib.parse.quote(pid), timeout=30) or {}
            except RuntimeError:
                continue
            subs = []
            for kid in (kids.get("results") or []):
                subs.append({
                    "cidr": kid.get("cidr"),
                    "gateway_ip": kid.get("gateway_ip"),
                    "allocation_ranges": kid.get("allocation_ranges") or [],
                })
            if subs:
                pool["subnets"] = subs
        return out

    def uplink_profiles(self):
        doc, _p = self.first(["/policy/api/v1/infra/host-switch-profiles",
                              "/api/v1/host-switch-profiles"])
        return (doc or {}).get("results") or []

    def transport_nodes(self):
        """Host transport nodes, from whichever path this build answers on.

        The manager path alone came back empty against a 9.1.1 manager, which made
        every cluster read 0/0 - sixteen hosts with no TEPs, rather than a list that
        was never fetched. Both are tried, and an empty result is reported as empty
        rather than treated as fact.
        """
        for path in ("/api/v1/transport-nodes?node_types=HostNode",
                     "/policy/api/v1/infra/sites/default/enforcement-points/"
                     "default/host-transport-nodes",
                     "/api/v1/transport-nodes"):
            try:
                doc = self.get(path) or {}
            except RuntimeError:
                continue
            got = doc.get("results") or []
            if path.endswith("/transport-nodes"):
                got = [g for g in got
                       if "Host" in str(g.get("node_deployment_info", {})
                                        .get("resource_type", "HostNode"))
                       or g.get("compute_collection_id")]
            if got:
                return got
        return []

    def node_state(self, node_id):
        try:
            return self.get("/api/v1/transport-nodes/%s/state"
                            % urllib.parse.quote(node_id), timeout=30) or {}
        except RuntimeError:
            return {}

    def pool_allocations(self, pool):
        """How many addresses of this pool are already handed out."""
        pid = pool.get("id") or pool.get("_path", "").rsplit("/", 1)[-1]
        for path in ("/policy/api/v1/infra/ip-pools/%s/ip-allocations" % pid,
                     "/api/v1/pools/ip-pools/%s/allocations" % pid):
            try:
                doc = self.get(path) or {}
                return len(doc.get("results") or [])
            except RuntimeError:
                continue
        return None

    def nodes_in(self, collection_id):
        """Host transport nodes of one compute collection, matched by NSX.

        Matching them here needs the node to carry compute_collection_id, and the
        policy listing does not - so every cluster read as "no transport nodes"
        while the list itself was full. Ask NSX for the ones in this collection
        and the question does not arise.
        """
        if not collection_id:
            return None
        try:
            doc = self.get("/api/v1/transport-nodes?node_types=HostNode"
                           "&compute_collection_id=%s"
                           % urllib.parse.quote(str(collection_id))) or {}
        except Exception:
            # Broad on purpose: this is a shortcut with a working fallback behind
            # it, so nothing it can throw should be the thing that fails a run.
            return None
        if doc.get("result_count") is None and not doc.get("results"):
            return None
        return doc.get("results") or []

    def collection_state(self, tnc_id):
        """How host preparation is going on this cluster, as NSX reports it.

        "Preparation Failed" is the answer to "why are there no TEPs", and going
        quiet about it sends people to look at networking that is already right.
        """
        if not tnc_id:
            return None
        for path in ("/api/v1/transport-node-collections/%s/state"
                     % urllib.parse.quote(tnc_id),
                     "/policy/api/v1/infra/sites/default/enforcement-points/default"
                     "/host-transport-node-collections/%s/state"
                     % urllib.parse.quote(tnc_id)):
            try:
                doc = self.get(path, timeout=30)
            except Exception:
                continue
            if doc:
                return doc
        return None

    def retry_realization(self, tnc_id):
        """Reapply a transport node profile to its cluster - the UI's Resolve.

        Config alone does not re-prepare hosts NSX previously skipped. This is the
        documented re-trigger, and the dialog behind Resolve calls the same thing.
        """
        last = None
        for path in ("/policy/api/v1/infra/sites/default/enforcement-points/default"
                     "/host-transport-node-collections/%s"
                     "?action=retry_profile_realization" % urllib.parse.quote(tnc_id),
                     "/api/v1/transport-node-collections/%s"
                     "?action=retry_profile_realization" % urllib.parse.quote(tnc_id)):
            try:
                return self.call("POST", path)
            except RuntimeError as e:
                last = e
                if "404" not in str(e) and "405" not in str(e):
                    raise
        raise last

    def edge_nodes(self):
        doc = self.get("/api/v1/transport-nodes?node_types=EdgeNode") or {}
        return doc.get("results") or []

    def tunnels(self, node_id):
        """Every geneve tunnel this transport node holds, with its two endpoints."""
        try:
            doc = self.get("/api/v1/transport-nodes/%s/tunnels"
                           % urllib.parse.quote(node_id), timeout=30) or {}
            return doc.get("tunnels") or []
        except RuntimeError:
            return []

    def put_pool(self, pool):
        body = {k: v for k, v in pool.items() if k != "__api"}
        pid = body.get("id") or str(body.get("path", "")).rsplit("/", 1)[-1]
        for path in ("/policy/api/v1/infra/ip-pools/%s" % urllib.parse.quote(pid),
                     "/api/v1/pools/ip-pools/%s" % urllib.parse.quote(pid)):
            try:
                return self.call("PUT", path, body)
            except RuntimeError as e:
                if "404" not in str(e) and "405" not in str(e):
                    raise
                last = e
        raise last

    def put_uplink_profile(self, prof):
        body = {k: v for k, v in prof.items() if k != "__api"}
        pid = body.get("id") or str(body.get("path", "")).rsplit("/", 1)[-1]
        last = None
        for path in ("/policy/api/v1/infra/host-switch-profiles/%s"
                     % urllib.parse.quote(pid),
                     "/api/v1/host-switch-profiles/%s" % urllib.parse.quote(pid)):
            try:
                return self.call("PUT", path, body)
            except RuntimeError as e:
                last = e
                if "404" not in str(e) and "405" not in str(e):
                    raise
        raise last

    def create_pool(self, pool_id, display_name, cidr, start, end, gateway):
        """A policy IP pool plus its one static subnet, which is a child object."""
        self.call("PATCH", "/policy/api/v1/infra/ip-pools/%s"
                  % urllib.parse.quote(pool_id),
                  {"display_name": display_name})
        sub = {"resource_type": "IpAddressPoolStaticSubnet",
               "cidr": cidr,
               "allocation_ranges": [{"start": start, "end": end}]}
        if gateway:
            sub["gateway_ip"] = gateway
        return self.call("PATCH", "/policy/api/v1/infra/ip-pools/%s/ip-subnets/%s"
                         % (urllib.parse.quote(pool_id),
                            urllib.parse.quote(pool_id + "-subnet")), sub)

    def put_profile(self, profile):
        """Write a profile back to whichever API it came from.

        The policy API carries its own address in 'path' and rejects a body holding
        fields it did not issue, so __api goes back out before the write.
        """
        body = {k: v for k, v in profile.items() if k != "__api"}
        pid = body.get("id") or str(body.get("path", "")).rsplit("/", 1)[-1]
        if profile.get("__api") == "manager":
            return self.call("PUT", "/api/v1/transport-node-profiles/%s"
                             % urllib.parse.quote(pid), body)
        path = body.get("path") or ("/infra/host-transport-node-profiles/%s" % pid)
        if not path.startswith("/"):
            path = "/" + path
        return self.call("PUT", "/policy/api/v1" + path, body)

    def cluster_hosts(self, coll):
        """How many ESX hosts are in this compute collection.

        host_count is not a field compute collections carry, so asking for one and
        getting None quietly turned the TEP pool capacity check off. Ask NSX.
        """
        ident = str(coll.get("external_id") or coll.get("id") or "")
        if not ident:
            return None
        try:
            doc = self.get("/api/v1/fabric/compute-collections/%s/member-summary"
                           % urllib.parse.quote(ident), timeout=30) or {}
            total = doc.get("total_host_count")
            if total is not None:
                return int(total)
        except RuntimeError:
            pass
        try:
            doc = self.get("/api/v1/fabric/discovered-nodes?parent_compute_collection=%s"
                           % urllib.parse.quote(ident), timeout=30) or {}
            got = doc.get("result_count")
            if got is not None:
                return int(got)
        except RuntimeError:
            pass
        return None


# --------------------------------------------------------- reading the shapes
# NSX renamed several of these between the manager and policy APIs and again
# between 4.x and 9.x. Everything below reads either spelling rather than
# pinning the tool to one version of one API.

def tz_kind(tz):
    """OVERLAY, VLAN, or ? - whichever field this build uses to say so."""
    raw = str(tz.get("tz_type") or tz.get("transport_type") or "").upper()
    if "OVERLAY" in raw:
        return "OVERLAY"
    if "VLAN" in raw:
        return "VLAN"
    return "?"


def refs(obj):
    """Every way this object can be referred to: its id and its policy path."""
    out = set()
    for key in ("id", "path", "_path", "unique_id", "realization_id"):
        val = obj.get(key)
        if val:
            out.add(str(val))
            out.add(str(val).rsplit("/", 1)[-1])
    return out


def name_of(obj):
    return obj.get("display_name") or obj.get("id") or "?"


def switches(profile):
    spec = profile.get("host_switch_spec") or {}
    return spec.get("host_switches") or []


def sw_tz_refs(sw):
    """Transport zone references on a host switch, under either field name."""
    out = []
    for ep in (sw.get("transport_zone_endpoints") or []):
        val = ep.get("transport_zone_id") or ep.get("transport_zone_path")
        if val:
            out.append(str(val))
    for val in (sw.get("transport_zone_paths") or []):
        out.append(str(val))
    return out


def sw_uplink_profile(sw):
    for entry in (sw.get("host_switch_profile_ids") or []):
        if str(entry.get("key", "")).lower().startswith("uplink"):
            return entry.get("value")
    return sw.get("uplink_profile_path")


def sw_pool(sw):
    spec = sw.get("ip_assignment_spec") or {}
    return (spec.get("ip_pool_id") or spec.get("ip_pool_path")
            or spec.get("ip_pool") or None)


def sw_assignment(sw):
    """How this switch gets TEP addresses: ('pool', ref) | ('dhcp', None) |
    ('none', None) | ('other', resource_type).

    A switch set to DHCP has no pool id, so reading only the pool made "DHCP on a
    VLAN with no DHCP server" and "nothing configured at all" print the same word.
    They need opposite fixes.
    """
    spec = sw.get("ip_assignment_spec") or {}
    kind = str(spec.get("resource_type") or "")
    ref = sw_pool(sw)
    if ref:
        return "pool", ref
    if not spec:
        return "none", None
    if "Dhcp" in kind or "DHCP" in kind.upper():
        return "dhcp", None
    return "other", kind or "unrecognised"


def sw_kinds(sw, tz_by_ref):
    """Which transport zone kinds this switch carries."""
    kinds = []
    for ref in sw_tz_refs(sw):
        tz = tz_by_ref.get(ref) or tz_by_ref.get(ref.rsplit("/", 1)[-1])
        kinds.append(tz_kind(tz) if tz else "?")
    return kinds


def has_overlay(profile, tz_by_ref):
    return any("OVERLAY" in sw_kinds(sw, tz_by_ref) for sw in switches(profile))


def overlay_switch(profile, tz_by_ref):
    for sw in switches(profile):
        if "OVERLAY" in sw_kinds(sw, tz_by_ref):
            return sw
    return None


def overlay_tz_ref(sw, tz_by_ref):
    for ref in sw_tz_refs(sw):
        tz = tz_by_ref.get(ref) or tz_by_ref.get(ref.rsplit("/", 1)[-1])
        if tz and tz_kind(tz) == "OVERLAY":
            return ref
    return None


def index_by_refs(items):
    out = {}
    for item in items:
        for ref in refs(item):
            out[ref] = item
    return out


def cluster_profiles(nsx):
    """cluster display name -> (compute collection, transport node collection, profile).

    A cluster NSX has never been told about has no collection; one prepared without
    a profile has a collection and no profile. Both are reported rather than skipped,
    because 'this cluster is not in the list' is the answer to somebody's question.
    """
    cc = nsx.clusters()
    tnc = nsx.collections()
    profs = nsx.profiles()
    prof_by_ref = index_by_refs(profs)

    tnc_by_cc = {}
    for c in tnc:
        key = c.get("compute_collection_id") or c.get("compute_collection_path")
        if key:
            tnc_by_cc[str(key)] = c
            tnc_by_cc[str(key).rsplit("/", 1)[-1]] = c

    out = {}
    for coll in cc:
        name = coll.get("display_name") or coll.get("cm_local_id") or "?"
        ident = str(coll.get("external_id") or coll.get("id") or "")
        got = tnc_by_cc.get(ident) or tnc_by_cc.get(ident.rsplit("/", 1)[-1])
        prof = None
        if got:
            pref = (got.get("transport_node_profile_id")
                    or got.get("transport_node_profile_path") or "")
            prof = (prof_by_ref.get(str(pref))
                    or prof_by_ref.get(str(pref).rsplit("/", 1)[-1]))
        out[name] = (coll, got, prof)
    return out


def node_cluster(node):
    """Which compute collection this node belongs to, under either field name."""
    for key in ("compute_collection_id", "compute_collection_path",
                "discovered_node_id"):
        val = node.get(key)
        if val:
            return str(val)
    return ""


def same_collection(a, b):
    """Compute collection ids are spelled 'domain-c9:uuid' in one API and bare in
    another. Compare on the parts rather than demanding they be identical."""
    a, b = str(a or ""), str(b or "")
    if not a or not b:
        return False
    if a == b:
        return True
    pa = set(x for x in a.replace("/", ":").split(":") if x)
    pb = set(x for x in b.replace("/", ":").split(":") if x)
    return bool(pa & pb)


def tep_counts(nsx, cluster_ident, nodes):
    """(transport nodes in this cluster, how many hold a TEP address).

    "TEP IP Address: Not Set" in the UI is the symptom that sends people looking,
    so the survey has to show it rather than leave it a click away. nodes is passed
    in because fetching every transport node once per cluster is minutes on a real
    estate.
    """
    ident = str(cluster_ident or "")
    mine = nsx.nodes_in(ident) if nsx is not None else None
    if mine is None:
        if not nodes:
            return None, None             # not read, which is not the same as none
        mine = [nd for nd in nodes if same_collection(ident, node_cluster(nd))]
    hosts = teps = 0
    for node in mine:
        hosts += 1
        state = nsx.node_state(node.get("id") or "")
        for hs in (state.get("host_switch_states") or []):
            if any(ep.get("ip") for ep in (hs.get("endpoints") or [])):
                teps += 1
                break
    return hosts, teps


def profile_users(found):
    """profile name -> the clusters it is applied to.

    One profile can serve several clusters. Editing it then changes all of them,
    which is fine when intended and a surprise when not, so it is said out loud.
    """
    out = {}
    for cluster, (_c, _t, prof) in found.items():
        if prof is not None:
            out.setdefault(name_of(prof), []).append(cluster)
    return out


# ------------------------------------------------------------------- survey
def survey(nsx, verbose=True, found=None):
    tz_by_ref = index_by_refs(nsx.transport_zones())
    pools = {r: p for p in nsx.ip_pools() for r in refs(p)}
    uplinks = {r: u for u in nsx.uplink_profiles() for r in refs(u)}
    found = found if found is not None else cluster_profiles(nsx)
    shared = {k: v for k, v in profile_users(found).items() if len(v) > 1}
    nodes = nsx.transport_nodes()
    rows = []
    unread = not nodes
    for name, (coll, tnc, prof) in sorted(found.items()):
        if prof is None:
            rows.append({"cluster": name, "profile": None, "overlay": False,
                         "note": "no transport node profile applied"
                                 if tnc else "not prepared by NSX"})
            continue
        sw = overlay_switch(prof, tz_by_ref)
        kinds = []
        for s in switches(prof):
            kinds += sw_kinds(s, tz_by_ref)
        base = switches(prof)[0] if switches(prof) else {}
        how, how_ref = sw_assignment(sw or base)
        kind_of_assignment = {"pool": "pool", "dhcp": "DHCP",
                              "none": "NOT SET"}.get(how, str(how_ref or how))
        pool_ref = sw_pool(sw or base)
        up_ref = sw_uplink_profile(sw or base)
        hosts, teps = tep_counts(
            nsx, coll.get("external_id") or coll.get("id"), nodes)
        rows.append({
            "cluster": name,
            "profile": name_of(prof),
            "profile_obj": prof,
            "overlay": sw is not None,
            "zones": sorted(set(kinds)),
            "vds": (sw or base).get("host_switch_name"),
            "uplinks": len((sw or base).get("uplinks") or []),
            "hosts": hosts,
            "teps": teps,
            "uplink_profile": name_of(uplinks[str(up_ref)]) if up_ref and str(up_ref) in uplinks else up_ref,
            "assign": kind_of_assignment,
            "vlan": transport_vlan(uplinks.get(str(up_ref))) if up_ref else None,
            "pool": name_of(pools[str(pool_ref)]) if pool_ref and str(pool_ref) in pools else pool_ref,
            "note": "",
        })
    if verbose:
        print("==== clusters NSX knows about")
        width = max([len(r["cluster"]) for r in rows] + [8])
        print("%-*s  %-7s  %-13s  %-18s  %-4s  %-7s  %-6s  %s"
              % (width, "CLUSTER", "OVERLAY", "ZONES", "VDS", "UPLK", "TEPVLAN",
                 "TEPs", "TEP POOL"))
        for r in rows:
            if r["profile"] is None:
                print("%-*s  %-7s  %s" % (width, r["cluster"], "-", r["note"]))
                continue
            print("%-*s  %-7s  %-13s  %-18s  %-4s  %-7s  %-6s  %s"
                  % (width, r["cluster"], "yes" if r["overlay"] else "NO",
                     ",".join(r["zones"]) or "?", r["vds"] or "?",
                     r["uplinks"] or "?",
                     "?" if r["vlan"] is None else r["vlan"],
                     "-" if r["hosts"] is None else "%d/%d" % (r["teps"], r["hosts"]),
                     r["pool"] or r["assign"]))
        print()
        odd = [r for r in rows
               if r["profile"] and r["overlay"] and r["vlan"] == 0]
        if odd:
            print("  NOTE  %s %s an overlay transport zone and transport VLAN 0 "
                  "(untagged). Every working overlay cluster here carries a real "
                  "TEP VLAN, so these were prepared with defaults and never "
                  "configured: %s"
                  % (len(odd), "has" if len(odd) == 1 else "have",
                     ", ".join(r["cluster"] for r in odd)))
            print()
        if unread:
            print("  NOTE  NSX returned no host transport nodes, so the TEPs column "
                  "is '-' rather than zero. Nothing here is a verdict on TEPs")
            print()
        if shared:
            for prof_name, users in sorted(shared.items()):
                print("  NOTE  profile '%s' is applied to %d clusters: %s. Changing "
                      "it changes all of them" % (prof_name, len(users),
                                                  ", ".join(sorted(users))))
            print()
    return rows


def pool_numbers(nsx, pool):
    """(total, used, free) for a pool, or (total, None, None) if allocations
    cannot be read. Free is what decides whether a cluster fits."""
    total = 0
    for sn in pool_subnets(pool):
        for rng in (sn.get("allocation_ranges") or []):
            total += ip_span(rng.get("start"), rng.get("end"))
    used = nsx.pool_allocations(pool)
    if used is None or not total:
        return total, used, None
    return total, used, total - used


def fabric(nsx, found=None, verbose=True):
    """Every uplink profile and every IP pool NSX holds.

    "Where do I find the TEP VLANs" is a three-screen hunt in the UI - the VLAN
    is on the uplink profile, the addressing is on the pool, and which clusters
    use what is on a third page. All of it is in the API, so print it together.
    """
    found = found if found is not None else cluster_profiles(nsx)
    ups = nsx.uplink_profiles()
    pools = nsx.ip_pools()

    users = {}
    for cluster, (_c, _t, prof) in found.items():
        if prof is None:
            continue
        for sw in switches(prof):
            ref = sw_uplink_profile(sw)
            if ref:
                users.setdefault(str(ref), []).append(cluster)
                users.setdefault(str(ref).rsplit("/", 1)[-1], []).append(cluster)

    if verbose:
        print("==== uplink profiles - the transport VLAN lives here")
        width = max([len(name_of(u)) for u in ups] + [4])
        print("%-*s  %-5s  %-5s  %-4s  %s"
              % (width, "NAME", "VLAN", "MTU", "UPLK", "USED BY"))
        for u in sorted(ups, key=name_of):
            who = []
            for ref in refs(u):
                who += users.get(ref, [])
            vlan = transport_vlan(u)
            print("%-*s  %-5s  %-5s  %-4s  %s"
                  % (width, name_of(u),
                     "?" if vlan is None else vlan,
                     u.get("mtu") or "-",
                     teaming_width(u) or "-",
                     ", ".join(sorted(set(who))) or "-"))
        print()

        print("==== IP address pools - the TEP subnets")
        width = max([len(name_of(p)) for p in pools] + [4])
        print("%-*s  %-18s  %-15s  %-7s  %s"
              % (width, "NAME", "SUBNET", "GATEWAY", "FREE", "RANGE"))
        for pl in sorted(pools, key=name_of):
            subs = pool_subnets(pl)
            cidr = ", ".join(str(sn.get("cidr")) for sn in subs) or "-"
            gw = ", ".join(str(sn.get("gateway_ip")) for sn in subs
                           if sn.get("gateway_ip")) or "NONE"
            rng = ", ".join("%s-%s" % (r.get("start"), r.get("end"))
                            for sn in subs
                            for r in (sn.get("allocation_ranges") or [])) or "-"
            total, used, free = pool_numbers(nsx, pl)
            print("%-*s  %-18s  %-15s  %-7s  %s"
                  % (width, name_of(pl), cidr, gw,
                     "?" if free is None else "%d/%d" % (free, total), rng))
        print()
    return ups, pools


# -------------------------------------------------------------------- apply
def transport_vlan(uplink_profile):
    """The VLAN this uplink profile tags TEP traffic with.

    This is the answer to 'which TEP VLAN' - it was decided when the cluster was
    prepared and it is sitting in NSX. It also decides which pool is correct: a
    pool's subnet belongs to one transport VLAN, so a pool from a cluster on a
    different VLAN is the wrong pool however convenient it looks.
    """
    if not isinstance(uplink_profile, dict):
        return None
    for key in ("transport_vlan", "transport_vlan_id", "vlan"):
        val = uplink_profile.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                return None
    return None


def teaming_width(uplink_profile):
    """How many uplinks this profile's teaming policy expects."""
    if not isinstance(uplink_profile, dict):
        return None
    best = 0
    teamings = [uplink_profile.get("teaming")] + list(
        uplink_profile.get("named_teamings") or [])
    for t in teamings:
        if not t:
            continue
        best = max(best, len(t.get("active_list") or []) + len(t.get("standby_list") or []))
    return best or None


_UNKNOWN = object()


def plan_cluster(name, target, ref_sw, ref_tz, ref_pool, ref_uplink,
                 tz_by_ref, uplink_obj, free_addresses, hosts, also_used_by=(),
                 target_uplink=None, pool_obj=None, pool_vlan=_UNKNOWN):
    """What would change on one cluster, and what must not be changed blindly.

    Returns (rows, new_profile or None). The profile is only returned when every
    guard passed - a half-applied overlay on a live cluster is worse than none.
    """
    rows = []
    if target is None:
        return [(BLOCKED, "%s: no transport node profile applied - NSX has not "
                          "prepared this cluster at all" % name)], None
    prof = json.loads(json.dumps(target))      # never edit the fetched object
    sws = switches(prof)
    if not sws:
        return [(BLOCKED, "%s: its profile has no host switch to add a zone to"
                 % name)], None
    attached = overlay_switch(prof, tz_by_ref)
    if attached is not None and sw_pool(attached):
        return [(OK, "%s: overlay transport zone and a TEP pool, nothing to add"
                 % name)], None

    # One profile can serve several clusters. Editing it reaches all of them, so
    # that is a refusal to be overridden deliberately, not a line in the output.
    if also_used_by:
        return [(BLOCKED,
                 "%s: its profile is shared with %s. Editing it would change those "
                 "clusters too - give this cluster its own profile first, or name "
                 "them all with --cluster"
                 % (name, ", ".join(sorted(also_used_by))))], None

    # Work on the switch that already carries the overlay when there is one; the
    # zone is attached and only the address assignment is missing, which is a host
    # with "TEP IP Address: Not Set" and no pool to draw one from.
    sw = attached if attached is not None else sws[0]
    if attached is None and len(sws) > 1:
        rows.append((SKIP, "%s: %d host switches, using the first (%s) - check this "
                           "is the one you mean" % (name, len(sws),
                                                    sw.get("host_switch_name"))))

    # The uplink trap: a two-uplink teaming policy on a switch wired with one
    # leaves the cluster half configured, and it is live when you find out.
    # A pool's subnet belongs to one transport VLAN. The target already has an
    # uplink profile saying which VLAN it tags TEP traffic with, so a pool from a
    # cluster on another VLAN allocates addresses that cannot pass traffic - the
    # failure that reads as success.
    mine_vlan = transport_vlan(target_uplink)
    # Which VLAN does the pool we are about to use actually serve? When a pool is
    # named deliberately that is the pool's own VLAN, not the reference's -
    # comparing against the reference there blocks the very case --pool exists for.
    if pool_vlan is _UNKNOWN:
        serves, whose = transport_vlan(uplink_obj), "the reference's pool"
    else:
        serves, whose = pool_vlan, "the pool named"
    if mine_vlan is not None and serves is not None and mine_vlan != serves:
        return [(BLOCKED,
                 "%s tags TEP traffic on VLAN %s but %s serves VLAN %s. Its "
                 "addresses would never pass traffic. Name the pool for VLAN %s "
                 "with --pool, or create one"
                 % (name, mine_vlan, whose, serves, mine_vlan))], None
    if pool_vlan is None and mine_vlan not in (None, 0):
        rows.append((SKIP, "%s: nothing else uses this pool yet, so its VLAN could "
                           "not be confirmed against this cluster's %s"
                     % (name, mine_vlan)))
    if mine_vlan == 0:
        rows.append((SKIP, "%s: its uplink profile tags TEP on VLAN 0 (untagged) - "
                           "check that is deliberate" % name))

    want = teaming_width(uplink_obj)
    have = len(sw.get("uplinks") or [])
    if want and have and want > have:
        return [(BLOCKED,
                 "%s: the reference uplink profile '%s' expects %d uplinks but this "
                 "switch (%s) has %d. Cable the second nic or build a single-uplink "
                 "profile - do not copy this one"
                 % (name, name_of(uplink_obj) if isinstance(uplink_obj, dict)
                    else ref_uplink, want, sw.get("host_switch_name"), have))], None

    # If a zone on this switch could not be classified, has_overlay said no and we
    # would be appending a second copy of a zone already there. Check the reference
    # itself rather than trusting the classification.
    present = set(sw_tz_refs(sw))
    present |= {r.rsplit("/", 1)[-1] for r in present}
    # Already there is not a reason to stop - only a reason not to add it twice.
    # Stopping here is what made a cluster with the zone and no TEP pool read as
    # finished while its hosts had no address.
    already = (ref_tz in present
               or str(ref_tz).rsplit("/", 1)[-1] in present)
    if "?" in sw_kinds(sw, tz_by_ref):
        rows.append((SKIP, "%s: a transport zone on this switch could not be "
                           "identified - check the result rather than trusting it"
                     % name))

    if attached is None and not already:
        eps = sw.setdefault("transport_zone_endpoints", [])
        eps.append({"transport_zone_id": ref_tz})
        rows.append((ADD, "%s: add overlay transport zone to %s"
                     % (name, sw.get("host_switch_name"))))
    else:
        rows.append((OK, "%s: overlay transport zone already on %s"
                     % (name, sw.get("host_switch_name"))))

    if not sw_pool(sw) and not ref_pool:
        return [(BLOCKED, "%s: it needs a TEP pool and the reference has none to "
                          "copy. Name one with --tep-pool, or the hosts will never "
                          "get a TEP address" % name)], None
    if ref_pool and not sw_pool(sw):
        sw["ip_assignment_spec"] = json.loads(json.dumps(
            ref_sw.get("ip_assignment_spec")))
        rows.append((ADD, "%s: TEP addresses from the reference pool - this is what "
                          "'TEP IP Address: Not Set' means, there was no pool to "
                          "draw one from" % name))
    elif ref_pool and sw_pool(sw) != ref_pool:
        rows.append((SKIP, "%s: keeps its own TEP pool, not the reference's" % name))

    if ref_uplink and not sw_uplink_profile(sw):
        sw.setdefault("host_switch_profile_ids", []).append(
            {"key": "UplinkHostSwitchProfile", "value": ref_uplink})
        rows.append((ADD, "%s: uplink profile from the reference" % name))

    if free_addresses is not None and hosts:
        if free_addresses < hosts:
            return [(BLOCKED, "%s: the TEP pool has %d address(es) free and this "
                              "cluster needs at least %d. Grow the pool first"
                     % (name, free_addresses, hosts))], None
        rows.append((OK, "%s: TEP pool has %d free for %d host(s)"
                     % (name, free_addresses, hosts)))
    else:
        # Silence here reads as a pass. It is not one.
        rows.append((SKIP, "%s: TEP pool capacity NOT checked (%s) - confirm the "
                           "pool has room before --commit"
                     % (name, "host count unknown" if free_addresses is not None
                        else "pool size or allocations unreadable")))
    if not any(st in (ADD, CHANGE) for st, _d in rows):
        return [(OK, "%s: nothing to add" % name)], None
    return rows, prof


def run_apply(nsx, reference, targets, commit, tep_pool=None,
              assume_pools=None, assume_vlans=None):
    """assume_* let a plan see what the earlier phases of the same plan would do.

    Without them a dry run reports "no such pool" and "VLAN 0 against VLAN 222"
    for work the same plan is about to do two phases earlier, and the operator
    gets a plan that is mostly blocked by itself. Nothing is assumed on --commit:
    by then the earlier phases have really run.
    """
    r = Result("overlay")
    found_first = cluster_profiles(nsx)
    survey(nsx, found=found_first)
    zones = nsx.transport_zones()
    tz_by_ref = index_by_refs(zones)
    uplink_by_ref = index_by_refs(nsx.uplink_profiles())
    pool_by_ref = index_by_refs(nsx.ip_pools())
    found = found_first

    if reference not in found:
        return r.add(BLOCKED, "no cluster called %s in NSX. Run survey to see the "
                              "names it knows" % reference)
    _c, _t, ref_prof = found[reference]
    if ref_prof is None:
        return r.add(BLOCKED, "%s has no transport node profile, so there is nothing "
                              "to copy from" % reference)
    ref_sw = overlay_switch(ref_prof, tz_by_ref)
    if ref_sw is None:
        return r.add(BLOCKED, "%s has no overlay transport zone either - pick a "
                              "cluster whose overlay is already working as the "
                              "reference" % reference)

    ref_tz = overlay_tz_ref(ref_sw, tz_by_ref)
    ref_pool = sw_pool(ref_sw)
    assume_pools = assume_pools or {}
    assume_vlans = assume_vlans or {}
    named_vlan = _UNKNOWN
    if tep_pool:
        named = [p for p in nsx.ip_pools()
                 if name_of(p) == tep_pool or tep_pool in refs(p)]
        if not named and tep_pool in assume_pools:
            named = [{"id": tep_pool, "display_name": tep_pool,
                      "path": "/infra/ip-pools/%s" % tep_pool, "subnets": []}]
            r.add(SKIP, "pool '%s' does not exist yet - planning against the one "
                        "this run would create" % tep_pool)
        if not named:
            return r.add(BLOCKED, "no TEP pool called %s. NSX has: %s"
                         % (tep_pool, ", ".join(sorted(name_of(p)
                                                       for p in nsx.ip_pools()))))
        # The policy API wants a path here, not a bare id - NSX answers
        # "The path=[<name>] is invalid" to an id. Rather than hard-code either,
        # match the spelling the reference's own working spec uses.
        chosen = named[0]
        pid = chosen.get("id") or str(chosen.get("path", "")).rsplit("/", 1)[-1]
        path = chosen.get("path") or ("/infra/ip-pools/%s" % pid)
        if not str(path).startswith("/"):
            path = "/" + str(path)
        was = str((ref_sw.get("ip_assignment_spec") or {}).get("ip_pool_id")
                  or (ref_sw.get("ip_assignment_spec") or {}).get("ip_pool_path")
                  or "")
        ref_pool = pid if (was and not was.startswith("/")) else path
        spec = json.loads(json.dumps(ref_sw.get("ip_assignment_spec") or {}))
        if not spec:
            spec = {"resource_type": "StaticIpPoolSpec"}
        for key in ("ip_pool_id", "ip_pool_path", "ip_pool"):
            spec.pop(key, None)
        spec["ip_pool_id"] = ref_pool
        ref_sw = json.loads(json.dumps(ref_sw))
        ref_sw["ip_assignment_spec"] = spec
        r.add(SKIP, "using TEP pool '%s' as told, not the reference's"
              % name_of(named[0]))
        # A pool serves one VLAN. Which one is told by whoever already draws from
        # it; a pool nothing uses yet has no VLAN to check against, and says so.
        # None, not the sentinel: the pool was named on purpose, so falling back
        # to the reference's VLAN would block the very case --pool is for.
        named_vlan = assume_pools.get(tep_pool)
        for _cl, (_c, _t, _pr) in found.items():
            if _pr is None:
                continue
            for _sw in switches(_pr):
                if str(sw_pool(_sw) or "").rsplit("/", 1)[-1] != pid:
                    continue
                _v = transport_vlan(uplink_by_ref.get(
                    str(sw_uplink_profile(_sw) or "")))
                if _v is not None:
                    named_vlan = _v
        r.add(OK if named_vlan is not None else SKIP,
              "pool '%s' serves VLAN %s" % (name_of(named[0]), named_vlan)
              if named_vlan is not None else
              "pool '%s' is not used by any cluster yet, so its VLAN cannot be "
              "confirmed from NSX" % name_of(named[0]))
    ref_uplink = sw_uplink_profile(ref_sw)
    uplink_obj = uplink_by_ref.get(str(ref_uplink)) if ref_uplink else None
    pool_obj = pool_by_ref.get(str(ref_pool)) if ref_pool else None

    tz_name = name_of(tz_by_ref.get(str(ref_tz), {})) if ref_tz else "?"
    r.add(OK, "reference %s: overlay zone '%s', uplink profile '%s', TEP pool '%s'"
          % (reference, tz_name,
             name_of(uplink_obj) if uplink_obj else ref_uplink or "none",
             name_of(pool_obj) if pool_obj else ref_pool or "none"))
    rvl = transport_vlan(uplink_obj)
    r.add(OK if rvl else SKIP,
          "reference tags TEP traffic on VLAN %s" % (rvl if rvl is not None
                                                     else "unreadable"))
    mtu = (uplink_obj or {}).get("mtu")
    if mtu:
        r.add(OK if int(mtu) >= 1600 else BLOCKED,
              "uplink profile MTU %s%s" % (mtu, "" if int(mtu) >= 1600 else
                                           " - geneve needs 1600 or more, and the "
                                           "physical fabric has to carry it"))

    free = None
    if pool_obj is not None:
        used = nsx.pool_allocations(pool_obj)
        total = 0
        for sub in (pool_obj.get("subnets") or []):
            for rng in (sub.get("allocation_ranges") or []):
                total += ip_span(rng.get("start"), rng.get("end"))
        if used is not None and total:
            free = total - used
            r.add(OK, "TEP pool '%s': %d of %d addresses free"
                  % (name_of(pool_obj), free, total))

    users = profile_users(found)
    for name in targets:
        if name == reference:
            r.add(SKIP, "%s is the reference" % name)
            continue
        if name not in found:
            r.add(BLOCKED, "no cluster called %s in NSX" % name)
            continue
        coll, _tnc, prof = found[name]
        hosts = nsx.cluster_hosts(coll)
        # Clusters sharing this profile that the operator did not name. Ones they
        # did name are being changed on purpose.
        sharers = [c for c in users.get(name_of(prof) if prof else "", [])
                   if c != name and c not in targets]
        mine_sw = overlay_switch(prof, tz_by_ref) or (switches(prof) or [{}])[0]
        mine_up = uplink_by_ref.get(str(sw_uplink_profile(mine_sw) or ""))
        if name in assume_vlans:
            mine_up = dict(mine_up or {}, transport_vlan=assume_vlans[name])
        rows, new = plan_cluster(name, prof, ref_sw, ref_tz, ref_pool, ref_uplink,
                                 tz_by_ref, uplink_obj, free, hosts,
                                 also_used_by=sharers, target_uplink=mine_up,
                                 pool_vlan=named_vlan)
        for state, detail in rows:
            r.add(state, detail)
        if new is None or not commit:
            continue
        try:
            nsx.put_profile(new)
        except RuntimeError as e:
            r.add(BLOCKED, "%s: NSX refused the change: %s" % (name, e))
            continue
        time.sleep(3)
        again = cluster_profiles(nsx).get(name, (None, None, None))[2]
        if again is not None and has_overlay(again, tz_by_ref):
            r.add(CHANGE, "%s: overlay transport zone applied; NSX will now prepare "
                          "the hosts and TEPs should appear" % name)
        else:
            r.add(BLOCKED, "%s: the change returned but the profile still reports no "
                           "overlay zone - look at it before retrying" % name)
    return r


def ip_int(addr):
    parts = str(addr or "").split(".")
    if len(parts) != 4:
        return None
    try:
        vals = [int(x) for x in parts]
    except ValueError:
        return None
    if any(v < 0 or v > 255 for v in vals):
        return None
    return (vals[0] << 24) | (vals[1] << 16) | (vals[2] << 8) | vals[3]


def in_cidr(cidr, addr):
    """Is addr inside cidr. None when either cannot be read, so a caller can tell
    'outside' from 'could not say' - they lead to different advice."""
    try:
        net, bits = str(cidr).split("/")
        bits = int(bits)
    except (ValueError, AttributeError):
        return None
    a, b = ip_int(net), ip_int(addr)
    if a is None or b is None or not 0 <= bits <= 32:
        return None
    mask = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF if bits else 0
    return (a & mask) == (b & mask)


def pool_subnets(pool):
    return (pool or {}).get("subnets") or []


def node_teps(state):
    """TEP addresses a transport node's state reports."""
    out = []
    for hs in (state.get("host_switch_states") or []):
        for ep in (hs.get("endpoints") or []):
            if ep.get("ip"):
                out.append(str(ep["ip"]))
    return out


def ip_span(start, end):
    """How many addresses an allocation range covers. IPv4 only, which TEPs are."""
    def num(addr):
        parts = str(addr or "").split(".")
        if len(parts) != 4:
            return None
        try:
            vals = [int(p) for p in parts]
        except ValueError:
            return None
        if any(v < 0 or v > 255 for v in vals):
            return None
        return (vals[0] << 24) | (vals[1] << 16) | (vals[2] << 8) | vals[3]
    a, b = num(start), num(end)
    if a is None or b is None or b < a:
        return 0
    return b - a + 1


# ------------------------------------------- the transport VLAN and the pool
def plan_vlan(name, prof, uplink, vlan, tz_by_ref, shared_with=()):
    """Set the transport VLAN on this cluster's uplink profile. (rows, new|None).

    The VLAN is what tags TEP traffic. A cluster prepared by a VCF import carries
    0 - untagged - which is why its hosts never get a usable TEP.
    """
    if prof is None:
        return [(BLOCKED, "%s: no transport node profile" % name)], None
    if uplink is None:
        return [(BLOCKED, "%s: its uplink profile could not be read" % name)], None
    have = transport_vlan(uplink)
    if have == vlan:
        return [(OK, "%s: %s already tags TEP on VLAN %s"
                 % (name, name_of(uplink), vlan))], None
    if shared_with:
        return [(BLOCKED,
                 "%s: uplink profile '%s' is also used by %s. Changing its VLAN "
                 "moves their TEP traffic too - give this cluster its own profile"
                 % (name, name_of(uplink), ", ".join(sorted(shared_with))))], None
    out = json.loads(json.dumps(uplink))
    out["transport_vlan"] = vlan
    rows = [(CHANGE, "%s: %s transport VLAN %s -> %s"
             % (name, name_of(uplink), "unset" if have is None else have, vlan))]
    if have not in (0, None):
        rows.append((SKIP, "%s: it was already carrying TEP on VLAN %s - changing "
                           "it moves live tunnels" % (name, have)))
    return rows, out


def overlaps(cidr, pools, skip_name=None):
    """Pools whose subnet is the same network. Two managers handing out addresses
    from one /24 with no shared awareness is how duplicate TEPs happen."""
    hits = []
    for pl in pools:
        if skip_name and name_of(pl) == skip_name:
            continue
        for sn in pool_subnets(pl):
            if not sn.get("cidr"):
                continue
            net = str(sn["cidr"]).split("/")[0]
            if in_cidr(cidr, net) or in_cidr(sn["cidr"], str(cidr).split("/")[0]):
                hits.append("%s (%s)" % (name_of(pl), sn["cidr"]))
    return hits


def plan_pool(display_name, cidr, start, end, gateway, existing):
    """A new TEP pool. (rows, True|None) - everything is checked before writing."""
    rows = []
    if any(name_of(p) == display_name for p in existing):
        return [(OK, "a pool called %s already exists" % display_name)], None
    for label, addr in (("range start", start), ("range end", end)):
        if in_cidr(cidr, addr) is not True:
            return [(BLOCKED, "%s %s is not inside %s" % (label, addr, cidr))], None
    if ip_int(start) > ip_int(end):
        return [(BLOCKED, "the range runs backwards: %s to %s" % (start, end))], None
    if gateway:
        if in_cidr(cidr, gateway) is not True:
            return [(BLOCKED, "gateway %s is not inside %s" % (gateway, cidr))], None
        if ip_int(start) <= ip_int(gateway) <= ip_int(end):
            return [(BLOCKED, "gateway %s is inside the allocation range %s-%s, so "
                              "a host would be handed it as a TEP"
                     % (gateway, start, end))], None
    else:
        rows.append((SKIP, "no gateway given. If the edges are on another tunnel "
                           "subnet the hosts will reach none of them"))
    clash = overlaps(cidr, existing)
    if clash:
        rows.append((SKIP, "%s overlaps %s. Two pools on one subnet must not share "
                           "addresses - check the ranges do not meet"
                     % (cidr, "; ".join(clash))))
    rows.append((ADD, "create pool %s: %s, range %s-%s, gateway %s"
                 % (display_name, cidr, start, end, gateway or "none")))
    return rows, True


def run_realize(nsx, clusters, commit):
    """Re-run host preparation on these clusters. Safe on a healthy one, and the
    only thing that moves a cluster off 'Preparation Failed' or 'Install Skipped'."""
    r = Result("realize")
    found = cluster_profiles(nsx)
    for name in clusters:
        if name not in found:
            r.add(BLOCKED, "no cluster called %s in NSX" % name)
            continue
        _coll, tnc, _prof = found[name]
        if tnc is None:
            r.add(BLOCKED, "%s: NSX has no transport node collection for it, so "
                           "there is no profile to reapply" % name)
            continue
        tnc_id = tnc.get("id") or str(tnc.get("path", "")).rsplit("/", 1)[-1]
        if not commit:
            r.add(CHANGE, "%s: reapply its transport node profile - this re-runs "
                          "host preparation" % name)
            continue
        try:
            nsx.retry_realization(tnc_id)
        except RuntimeError as e:
            r.add(BLOCKED, "%s: NSX refused the retry: %s" % (name, e))
            continue
        r.add(CHANGE, "%s: host preparation re-triggered. It runs for several "
                      "minutes; watch it in NSX or re-run verify" % name)
    return r


def run_vlan(nsx, clusters, vlan, commit):
    r = Result("vlan")
    found = cluster_profiles(nsx)
    tz_by_ref = index_by_refs(nsx.transport_zones())
    ups = nsx.uplink_profiles()
    up_by_ref = index_by_refs(ups)
    users = {}
    for cl, (_c, _t, pr) in found.items():
        if pr is None:
            continue
        for sw in switches(pr):
            ref = sw_uplink_profile(sw)
            if ref:
                users.setdefault(str(ref), set()).add(cl)
                users.setdefault(str(ref).rsplit("/", 1)[-1], set()).add(cl)
    for name in clusters:
        if name not in found:
            r.add(BLOCKED, "no cluster called %s in NSX" % name)
            continue
        _c, _t, prof = found[name]
        sw = (overlay_switch(prof, tz_by_ref) if prof else None) or \
             ((switches(prof) or [{}])[0] if prof else {})
        ref = sw_uplink_profile(sw)
        uplink = up_by_ref.get(str(ref)) if ref else None
        others = (users.get(str(ref), set()) - {name} - set(clusters)) if ref else set()
        rows, new = plan_vlan(name, prof, uplink, vlan, tz_by_ref, others)
        for st, d in rows:
            r.add(st, d)
        if new is None or not commit:
            continue
        try:
            nsx.put_uplink_profile(new)
        except RuntimeError as e:
            r.add(BLOCKED, "%s: NSX refused it: %s" % (name, e))
            continue
        again = index_by_refs(nsx.uplink_profiles()).get(str(ref))
        r.add(CHANGE if transport_vlan(again) == vlan else BLOCKED,
              "%s: now tags TEP on VLAN %s" % (name, vlan)
              if transport_vlan(again) == vlan else
              "%s: the write returned but the profile still reads VLAN %s"
              % (name, transport_vlan(again)))
    return r


def run_pool(nsx, display_name, cidr, rng, gateway, commit):
    r = Result("pool")
    try:
        start, end = str(rng).split("-", 1)
    except ValueError:
        return r.add(BLOCKED, "--range wants start-end, such as 10.0.0.10-10.0.0.49")
    existing = nsx.ip_pools()
    rows, go = plan_pool(display_name, cidr, start.strip(), end.strip(),
                         gateway, existing)
    for st, d in rows:
        r.add(st, d)
    if not go or not commit:
        return r
    pid = display_name
    try:
        nsx.create_pool(pid, display_name, cidr, start.strip(), end.strip(), gateway)
    except RuntimeError as e:
        return r.add(BLOCKED, "NSX refused it: %s" % e)
    time.sleep(2)
    back = [p for p in nsx.ip_pools() if name_of(p) == display_name]
    ok = bool(back) and bool(pool_subnets(back[0]))
    r.add(ADD if ok else BLOCKED,
          "pool %s created with %s" % (display_name, cidr) if ok else
          "the write returned but %s does not read back with a subnet" % display_name)
    return r


# ------------------------------------------------------------------- verify
def gateway_needed(host_tep, edge_teps, pool):
    """(needed, reason). A routed tunnel needs a gateway on the host's pool.

    When the hosts' tunnel subnet and the edges' differ - which is what VCF does,
    a tunnel VLAN per domain - every host-to-edge tunnel is routed, and a pool with
    no gateway means the host reaches no edge at all. The segments realize, the UI
    looks healthy, and the VMs get nowhere past their own segment. Found the hard
    way on this estate once already.
    """
    if not host_tep or not edge_teps:
        return False, "no addresses to compare"
    same = None
    for cidr in [sn.get("cidr") for sn in pool_subnets(pool)]:
        for edge in edge_teps:
            got = in_cidr(cidr, edge)
            if got is None:
                continue
            same = True if got else (False if same is None else same)
    if same is None:
        return False, "could not read the pool's subnet"
    if same:
        return False, "hosts and edges share a tunnel subnet"
    has_gw = any(sn.get("gateway_ip") for sn in pool_subnets(pool))
    return (not has_gw), ("the edges are on another tunnel subnet and this pool has "
                          "no gateway" if not has_gw else
                          "routed, and the pool has a gateway")


def verify(nsx, only=None):
    """Did it actually work: TEPs, tunnels, and the gateway behind them.

    An overlay zone applied is not an overlay working. This is the check that was
    being handed to another tool, and it is the one that catches the failure that
    looks like success.
    """
    r = Result("verify")
    found = cluster_profiles(nsx)
    nodes = nsx.transport_nodes()
    pool_by_ref = index_by_refs(nsx.ip_pools())
    tz_by_ref = index_by_refs(nsx.transport_zones())

    edges = nsx.edge_nodes()
    edge_teps = []
    for e in edges:
        edge_teps += node_teps(nsx.node_state(e.get("id") or ""))
    r.add(OK if edge_teps else SKIP,
          "%d edge node(s), %d tunnel endpoint(s): %s"
          % (len(edges), len(edge_teps), ", ".join(edge_teps[:6]) or "none readable"))

    unmatched, looked = [], 0
    targets = [c for c in sorted(found) if not only or c in only]
    if only:
        for name in only:
            if name not in found:
                r.add(BLOCKED, "no cluster called %s in NSX" % name)

    for name in targets:
        coll, tnc, prof = found[name]
        if prof is None:
            r.add(SKIP, "%s: not prepared by NSX" % name)
            continue
        st = nsx.collection_state(
            (tnc or {}).get("id") or
            str((tnc or {}).get("path", "")).rsplit("/", 1)[-1]) or {}
        raw = str(st.get("state") or "")
        if raw and raw.lower() not in ("success", "in_sync", "stable"):
            detail = ""
            for key in ("details", "aggregate_progress_percentage"):
                if st.get(key) is not None:
                    detail = "; %s=%s" % (key, str(st[key])[:160])
                    break
            r.add(BLOCKED if "fail" in raw.lower() else SKIP,
                  "%s: NSX reports host preparation '%s'%s. Until that clears "
                  "there will be no TEPs however right the config is - "
                  "'nsx_prep realize --cluster %s --commit' re-runs it"
                  % (name, raw, detail, name))
        sw = overlay_switch(prof, tz_by_ref)
        if sw is None:
            r.add(SKIP, "%s: no overlay transport zone, so no TEPs are expected"
                  % name)
            continue
        ident = str(coll.get("external_id") or coll.get("id") or "")
        pool = pool_by_ref.get(str(sw_pool(sw) or ""))
        mine = nsx.nodes_in(ident)
        if mine is None:
            mine = [n for n in nodes
                    if same_collection(ident, node_cluster(n))]
        looked += 1
        if not mine:
            unmatched.append(name)
            continue

        no_tep, down, up_total, first_tep = [], [], 0, None
        for node in mine:
            nid = node.get("id") or ""
            label = node.get("display_name") or nid
            teps = node_teps(nsx.node_state(nid))
            if not teps:
                no_tep.append(label)
                continue
            first_tep = first_tep or teps[0]
            tuns = nsx.tunnels(nid)
            bad = [t for t in tuns if str(t.get("status", "")).upper() != "UP"]
            up_total += len(tuns) - len(bad)
            if not tuns:
                down.append("%s (no tunnels at all)" % label)
            elif bad:
                down.append("%s (%d of %d down, to %s)"
                            % (label, len(bad), len(tuns),
                               ", ".join(sorted({str(t.get("remote_node_display_name")
                                                     or t.get("remote_ip"))
                                                 for t in bad})[:3])))

        if no_tep:
            r.add(BLOCKED, "%s: %d host(s) have no TEP address: %s. The zone is on "
                           "the profile but NSX has not realized it - check the pool "
                           "has free addresses and the hosts are not in maintenance "
                           "mode" % (name, len(no_tep), ", ".join(no_tep[:4])))
        if down:
            r.add(BLOCKED, "%s: tunnels down - %s" % (name, "; ".join(down[:4])))
        if not no_tep and not down:
            r.add(OK, "%s: %d host(s), all with a TEP, %d tunnel(s) up"
                  % (name, len(mine), up_total))

        need, why = gateway_needed(first_tep, edge_teps, pool)
        if need:
            r.add(BLOCKED, "%s: %s. Every host-to-edge tunnel here is routed, so "
                           "without it the hosts reach no edge and VMs get nowhere "
                           "past their own segment. Set the gateway on pool '%s' to "
                           "the address the leaves hold in that subnet"
                  % (name, why, name_of(pool) if pool else sw_pool(sw)))
        elif pool is not None:
            r.add(OK, "%s: TEP pool '%s' - %s" % (name, name_of(pool), why))

    # Every overlay cluster coming back empty is this tool failing to tie nodes to
    # clusters, not an estate with no transport nodes anywhere. Saying it once as a
    # caveat beats four identical findings that are not findings.
    if unmatched and looked and len(unmatched) == looked:
        r.add(SKIP, "could not tie transport nodes to any cluster, so TEP and "
                    "tunnel state is unverified for %s. The configuration above "
                    "still applied; check TEP IP Address in NSX > System > Fabric "
                    "> Hosts > Clusters" % ", ".join(unmatched))
    else:
        for name in unmatched:
            r.add(BLOCKED, "%s: overlay is configured but NSX lists no transport "
                           "nodes for it" % name)
    return r


# ------------------------------------------------------- the pool gateway
def plan_gateway(pool, gateway):
    """Put a gateway on a TEP pool's subnet. (rows, new pool or None).

    verify finds a pool that needs one; without this the operator is sent to the
    UI for a single field, which is how the finding gets lost. The address is the
    one the leaves hold in that subnet - supplied, never guessed, because nothing
    reachable from NSX knows it.
    """
    if pool is None:
        return [(BLOCKED, "no pool by that name")], None
    subs = pool_subnets(pool)
    if not subs:
        return [(BLOCKED, "%s has no subnet to put a gateway on" % name_of(pool))], None
    mine = [sn for sn in subs if in_cidr(sn.get("cidr"), gateway)]
    if not mine:
        return [(BLOCKED, "%s is not inside any subnet of %s (%s)"
                 % (gateway, name_of(pool),
                    ", ".join(str(sn.get("cidr")) for sn in subs)))], None
    if len(mine) > 1:
        return [(BLOCKED, "%s falls in %d subnets of %s - ambiguous"
                 % (gateway, len(mine), name_of(pool)))], None
    sn = mine[0]
    # A gateway inside an allocation range gets handed to a host as a TEP, and
    # then two things own the address.
    g = ip_int(gateway)
    for rng in (sn.get("allocation_ranges") or []):
        lo, hi = ip_int(rng.get("start")), ip_int(rng.get("end"))
        if lo is not None and hi is not None and lo <= g <= hi:
            return [(BLOCKED, "%s is inside the allocation range %s-%s, so a host "
                              "would be given it as a TEP. Use an address outside "
                              "the range" % (gateway, rng.get("start"),
                                             rng.get("end")))], None
    have = sn.get("gateway_ip")
    if have == gateway:
        return [(OK, "%s already has gateway %s on %s"
                 % (name_of(pool), gateway, sn.get("cidr")))], None
    out = json.loads(json.dumps(pool))
    for cand in pool_subnets(out):
        if cand.get("cidr") == sn.get("cidr"):
            cand["gateway_ip"] = gateway
    verb = "change %s to" % have if have else "set"
    return [(CHANGE, "%s: %s gateway %s on %s"
             % (name_of(pool), verb, gateway, sn.get("cidr")))], out


def run_gateway(nsx, pool_name, gateway, commit):
    r = Result("gateway")
    pools = nsx.ip_pools()
    match = [p for p in pools if name_of(p) == pool_name or pool_name in refs(p)]
    if not match:
        return r.add(BLOCKED, "no TEP pool called %s. NSX has: %s"
                     % (pool_name, ", ".join(sorted(name_of(p) for p in pools))))
    if len(match) > 1:
        return r.add(BLOCKED, "%d pools answer to %s" % (len(match), pool_name))
    rows, new = plan_gateway(match[0], gateway)
    for state, detail in rows:
        r.add(state, detail)
    if new is None or not commit:
        return r
    try:
        nsx.put_pool(new)
    except RuntimeError as e:
        return r.add(BLOCKED, "NSX refused it: %s" % e)
    time.sleep(2)
    again = [p for p in nsx.ip_pools() if name_of(p) == name_of(new)]
    ok = any(sn.get("gateway_ip") == gateway
             for p in again for sn in pool_subnets(p))
    r.add(CHANGE if ok else BLOCKED,
          "gateway %s is set on %s" % (gateway, name_of(new)) if ok else
          "the write returned but %s still reports no gateway" % name_of(new))
    if ok:
        r.add(SKIP, "the hosts pick a new gateway up when their transport node is "
                    "re-applied; verify again in a few minutes")
    return r


def diag(nsx, found=None):
    """What each host-transport-node endpoint actually answers on this build.

    Three guesses at the schema is two too many. This asks every endpoint the
    tool might use, prints the status, the count and the field names of the first
    result, and shows one compute collection id to compare against - so the next
    change is made from evidence instead of from a guess.
    """
    found = found if found is not None else cluster_profiles(nsx)
    print("==== compute collections, as the clusters report them")
    for name in sorted(found)[:40]:
        coll, _t, prof = found[name]
        if prof is None:
            continue
        print("  %-16s external_id=%r id=%r"
              % (name, coll.get("external_id"), coll.get("id")))
    print()

    sample = None
    for name in sorted(found):
        coll, _t, prof = found[name]
        if prof is not None:
            sample = str(coll.get("external_id") or coll.get("id") or "")
            break

    paths = [
        "/api/v1/transport-nodes?node_types=HostNode",
        "/api/v1/transport-nodes",
        "/policy/api/v1/infra/sites/default/enforcement-points/default"
        "/host-transport-nodes",
        "/policy/api/v1/infra/sites/default/enforcement-points/default"
        "/transport-node-collections",
        "/api/v1/transport-node-collections",
    ]
    if sample:
        paths.insert(0, "/api/v1/transport-nodes?node_types=HostNode"
                        "&compute_collection_id=%s" % urllib.parse.quote(sample))
    print("==== endpoints (sample compute collection: %r)" % sample)
    for path in paths:
        try:
            doc = nsx.get(path) or {}
        except RuntimeError as e:
            print("  %-78s  ERROR %s" % (path[:78], str(e)[:90]))
            continue
        res = doc.get("results")
        if res is None:
            print("  %-78s  no 'results'; keys=%s"
                  % (path[:78], sorted(doc)[:10]))
            continue
        print("  %-78s  %d result(s)" % (path[:78], len(res)))
        if res:
            first = res[0]
            print("        keys: %s" % ", ".join(sorted(first)[:18]))
            for key in ("id", "display_name", "node_id", "compute_collection_id",
                        "compute_collection_path", "parent_path", "path",
                        "transport_node_profile_id", "resource_type"):
                if key in first:
                    print("        %-26s %r" % (key + ":", first[key]))
    print()
    return 0


# -------------------------------------------------------------------- build
def run_build(nsx, cfg, commit):
    """Everything in the config, in dependency order, skipping what is already so.

    Idempotent by construction rather than by bookkeeping: every phase reads the
    live state and plans against it, and each planner already returns "nothing to
    do" when the end state is reached. So a run that dies halfway, a run against a
    half-configured estate, and a second run after a clean one all behave the same
    - they do what is left. There is no saved state to go stale.
    """
    rows = []
    reference = str(cfg["reference"])
    want = cfg.get("clusters") or {}
    pools_wanted = cfg.get("pools") or {}
    did = {"pool": 0, "vlan": 0, "assign": 0}

    # --- phase 1: the pools, because a cluster cannot be pointed at a missing one
    print("==== pools")
    have = nsx.ip_pools()
    for pname, body in sorted(pools_wanted.items()):
        start, _, end = str(body["range"]).partition("-")
        prows, go = plan_pool(pname, str(body["cidr"]), start.strip(), end.strip(),
                              body.get("gateway"), have)
        for st, d in prows:
            print("  %-7s %s" % (st.upper(), d))
            rows.append((st, d))
        if go and commit:
            try:
                nsx.create_pool(pname, pname, str(body["cidr"]), start.strip(),
                                end.strip(), body.get("gateway"))
                did["pool"] += 1
                print("  %-7s pool %s created" % (ADD.upper(), pname))
                rows.append((ADD, "pool %s created" % pname))
            except RuntimeError as e:
                print("  %-7s pool %s: NSX refused it: %s" % (BLOCKED.upper(), pname, e))
                rows.append((BLOCKED, "pool %s: %s" % (pname, e)))
        elif go:
            did["pool"] += 1
    if commit and did["pool"]:
        time.sleep(2)
        have = nsx.ip_pools()          # phase 3 needs to see what phase 1 made
    print()

    # --- phase 2: the transport VLANs, because the assignment guard compares them
    print("==== transport VLANs")
    by_vlan = {}
    for cname, body in want.items():
        if (body or {}).get("vlan") is not None:
            by_vlan.setdefault(int(body["vlan"]), []).append(cname)
    for vlan, names in sorted(by_vlan.items()):
        res = run_vlan(nsx, sorted(names), vlan, commit)
        for st, d in res.rows:
            print("  %-7s %s" % (st.upper(), d))
            rows.append((st, d))
            if st in (ADD, CHANGE):
                did["vlan"] += 1
    print()

    # --- phase 3: point each cluster at its pool, grouped so one run per pool
    print("==== transport zone and TEP pool")
    by_pool = {}
    for cname, body in want.items():
        by_pool.setdefault((body or {}).get("pool"), []).append(cname)
    # On a plan, let this phase see what phases 1 and 2 would have done.
    assume_pools, assume_vlans = {}, {}
    if not commit:
        for cname, body in want.items():
            if (body or {}).get("vlan") is not None:
                assume_vlans[cname] = int(body["vlan"])
        for pname in pools_wanted:
            vlans = {assume_vlans[c] for c, b in want.items()
                     if (b or {}).get("pool") == pname and c in assume_vlans}
            assume_pools[pname] = vlans.pop() if len(vlans) == 1 else None
    for pool, names in sorted(by_pool.items(), key=lambda kv: str(kv[0])):
        res = run_apply(nsx, reference, sorted(names), commit, tep_pool=pool,
                        assume_pools=assume_pools, assume_vlans=assume_vlans)
        for st, d in res.rows:
            print("  %-7s %s" % (st.upper(), d))
            rows.append((st, d))
            if st in (ADD, CHANGE):
                did["assign"] += 1
    print()

    # --- phase 4: did it actually work
    if commit and (did["vlan"] or did["assign"]):
        print("  waiting 60s for NSX to realize the change on the hosts")
        time.sleep(60)
    print("==== verify")
    vres = verify(nsx, sorted(want))
    for st, d in vres.rows:
        print("  %-7s %s" % (st.upper(), d))
        rows.append((st, d))
    print()

    out = Result("build")
    for st, d in rows:
        out.add(st, d)
    if not commit:
        print("PENDING: %d pool(s), %d transport VLAN(s), %d TEP assignment(s)"
              % (did["pool"], did["vlan"], did["assign"]))
    return out


# ----------------------------------------------------------------- selftest
def selftest():
    fails = []

    def ck(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    OVL = {"id": "tz-ovl", "display_name": "site-overlay-tz", "tz_type": "OVERLAY_STANDARD",
           "path": "/infra/sites/default/enforcement-points/default/transport-zones/tz-ovl"}
    VLN = {"id": "tz-vlan", "display_name": "site-vlan-tz", "transport_type": "VLAN"}
    tzmap = index_by_refs([OVL, VLN])

    ck("transport zone kind is read from either field name",
       tz_kind(OVL) == "OVERLAY" and tz_kind(VLN) == "VLAN")
    ck("an unknown zone is '?' rather than guessed at",
       tz_kind({"display_name": "x"}) == "?")
    ck("a zone is found by id and by policy path",
       tzmap.get("tz-ovl") is OVL and tzmap.get(OVL["path"]) is OVL)

    def switch(name, tzs, uplinks=2, pool="pool-tep", uplink_profile="up-2nic"):
        sw = {"host_switch_name": name, "host_switch_type": "VDS",
              "transport_zone_endpoints": [{"transport_zone_id": t} for t in tzs],
              "uplinks": [{"uplink_name": "uplink-%d" % (i + 1)} for i in range(uplinks)]}
        if pool:
            sw["ip_assignment_spec"] = {"resource_type": "StaticIpPoolSpec",
                                        "ip_pool_id": pool}
        if uplink_profile:
            sw["host_switch_profile_ids"] = [{"key": "UplinkHostSwitchProfile",
                                              "value": uplink_profile}]
        return sw

    def profile(name, sws):
        return {"id": name, "display_name": name, "_path":
                "/infra/host-transport-node-profiles/%s" % name,
                "host_switch_spec": {"host_switches": sws}}

    REF = profile("prof-ref", [switch("vds-ref", ["tz-ovl", "tz-vlan"])])
    VLANONLY = profile("prof-a", [switch("vds-a", ["tz-vlan"],
                                              pool=None, uplink_profile=None)])
    UP2 = {"id": "up-2nic", "display_name": "two-nic-uplink", "mtu": 9000,
           "teaming": {"active_list": [{"uplink_name": "uplink-1"},
                                       {"uplink_name": "uplink-2"}]}}

    ck("a profile carrying an overlay zone is recognised", has_overlay(REF, tzmap))
    ck("a vlan-only profile is not", not has_overlay(VLANONLY, tzmap))
    ck("the overlay zone reference is picked out of a mixed switch",
       overlay_tz_ref(overlay_switch(REF, tzmap), tzmap) == "tz-ovl")
    ck("the pool and uplink profile are read off the reference",
       sw_pool(overlay_switch(REF, tzmap)) == "pool-tep"
       and sw_uplink_profile(overlay_switch(REF, tzmap)) == "up-2nic")
    ck("teaming width counts active and standby",
       teaming_width(UP2) == 2
       and teaming_width({"teaming": {"active_list": [1], "standby_list": [2, 3]}}) == 3)

    ref_sw = overlay_switch(REF, tzmap)
    rows, new = plan_cluster("cluster-a", VLANONLY, ref_sw, "tz-ovl", "pool-tep",
                             "up-2nic", tzmap, UP2, free_addresses=40, hosts=4)
    ck("a vlan-only cluster gains the overlay zone", new is not None
       and has_overlay(new, tzmap))
    ck("and the TEP pool it had none of",
       new is not None and sw_pool(switches(new)[0]) == "pool-tep")
    ck("and the uplink profile",
       new is not None and sw_uplink_profile(switches(new)[0]) == "up-2nic")
    ck("the fetched profile is not edited in place",
       not has_overlay(VLANONLY, tzmap))

    # The single-uplink trap: a config asking for two nics on a switch where only
    # one was ever attached. Found in the field; it would have landed as a
    # half-configured teaming policy on a live cluster.
    ONENIC = profile("prof-1nic", [switch("vds-1nic", ["tz-vlan"], uplinks=1,
                                           pool=None, uplink_profile=None)])
    rows, new = plan_cluster("cluster-d", ONENIC, ref_sw, "tz-ovl", "pool-tep",
                             "up-2nic", tzmap, UP2, free_addresses=40, hosts=8)
    ck("a two-uplink profile is refused on a one-uplink switch", new is None)
    ck("and the refusal says to cable the nic or build a single-uplink profile",
       any(s == BLOCKED and "single-uplink" in d for s, d in rows))

    # Pool capacity, before the change rather than after eight hosts have asked.
    rows, new = plan_cluster("cluster-a", VLANONLY, ref_sw, "tz-ovl", "pool-tep",
                             "up-2nic", tzmap, UP2, free_addresses=3, hosts=8)
    ck("a pool too small for the cluster blocks", new is None)
    ck("and it says how many are free against how many are needed",
       any("3 address(es) free" in d and "at least 8" in d for _s, d in rows))

    rows, new = plan_cluster("prof-ref", REF, ref_sw, "tz-ovl", "pool-tep",
                             "up-2nic", tzmap, UP2, 40, 4)
    ck("a cluster that already has overlay is left alone",
       new is None and rows[0][0] == OK)

    rows, new = plan_cluster("ghost", None, ref_sw, "tz-ovl", "pool-tep",
                             "up-2nic", tzmap, UP2, 40, 4)
    ck("a cluster NSX never prepared is reported, not crashed on",
       new is None and rows[0][0] == BLOCKED)

    # --- the gaps found on the review pass -------------------------------
    # A profile serving several clusters: editing it reaches all of them.
    rows, new = plan_cluster("cluster-a", VLANONLY, ref_sw, "tz-ovl", "pool-tep",
                             "up-2nic", tzmap, UP2, 40, 4,
                             also_used_by=["cluster-b", "cluster-c"])
    ck("a shared profile is refused rather than quietly changing other clusters",
       new is None and any(s == BLOCKED and "shared with" in d for s, d in rows))
    ck("and it names the clusters that would have been caught",
       any("cluster-b" in d and "cluster-c" in d for _s, d in rows))

    # The zone is already there but NSX answered with a spelling we cannot classify.
    # Appending would have given the switch the same zone twice.
    # A zone already on the switch is never added twice - but the run carries on,
    # because the pool may still be missing. Returning early here is what made a
    # cluster with the zone and no TEP pool look finished.
    UNKNOWN = profile("odd", [switch("vds", ["tz-ovl", "tz-mystery"], pool=None,
                                     uplink_profile=None)])
    rows, new = plan_cluster("odd", UNKNOWN, ref_sw, "tz-ovl", "pool-tep",
                             "up-2nic", tzmap, UP2, 40, 4)
    ck("an overlay zone already present is not added a second time",
       new is not None
       and [e["transport_zone_id"] for e in
            switches(new)[0]["transport_zone_endpoints"]].count("tz-ovl") == 1)
    ck("and the run carries on to add the pool it was missing",
       new is not None and sw_pool(switches(new)[0]) == "pool-tep")
    POLICYPATH = profile("pathy", [switch(
        "vds", ["/infra/sites/default/enforcement-points/default/transport-zones/tz-ovl"],
        pool=None, uplink_profile=None)])
    rows, new = plan_cluster("pathy", POLICYPATH, ref_sw, "tz-ovl", "pool-tep",
                             "up-2nic", tzmap, UP2, 40, 4)
    ck("the same zone written as a policy path is recognised, not duplicated",
       new is not None
       and len(switches(new)[0]["transport_zone_endpoints"]) == 1)

    # A capacity check that could not run must not read as a pass.
    rows, new = plan_cluster("cluster-a", VLANONLY, ref_sw, "tz-ovl", "pool-tep",
                             "up-2nic", tzmap, UP2, free_addresses=None, hosts=None)
    ck("an unchecked TEP pool says so instead of staying silent",
       new is not None and any(s == SKIP and "NOT checked" in d for s, d in rows))

    # A profile read from the manager API must not be written to the policy API.
    sent = {}

    class FakePut(Nsx):
        def __init__(self):
            pass

        def call(self, method, path, body=None, timeout=None):
            sent["method"], sent["path"], sent["body"] = method, path, body
            return {}

    mgr = dict(profile("m", [switch("vds", ["tz-vlan"])]), __api="manager")
    FakePut().put_profile(mgr)
    ck("a manager-API profile is written back to the manager API",
       sent["path"] == "/api/v1/transport-node-profiles/m")
    pol = dict(profile("p", [switch("vds", ["tz-vlan"])]), __api="policy",
               path="/infra/host-transport-node-profiles/p")
    FakePut().put_profile(pol)
    ck("a policy profile goes to its own path, read from 'path' not '_path'",
       sent["path"] == "/policy/api/v1/infra/host-transport-node-profiles/p")
    ck("the bookkeeping key is not sent to NSX", "__api" not in sent["body"])

    ck("profile_users maps one profile to every cluster using it",
       profile_users({"a": (None, None, REF), "b": (None, None, REF),
                      "c": (None, None, VLANONLY)}) ==
       {"prof-ref": ["a", "b"], "prof-a": ["c"]})

    # --- verification: the checks that were being handed to another tool ----
    ck("in_cidr places an address inside and outside its network",
       in_cidr("10.0.21.0/24", "10.0.21.11") is True
       and in_cidr("10.0.21.0/24", "10.0.23.11") is False)
    ck("in_cidr says 'cannot tell' rather than guessing on junk",
       in_cidr("not-a-cidr", "10.0.0.1") is None
       and in_cidr("10.0.0.0/24", "nope") is None
       and in_cidr("10.0.0.0/99", "10.0.0.1") is None)
    ck("a /32 and a /0 both behave",
       in_cidr("10.0.0.1/32", "10.0.0.1") is True
       and in_cidr("10.0.0.1/32", "10.0.0.2") is False
       and in_cidr("0.0.0.0/0", "8.8.8.8") is True)

    POOL_NO_GW = {"display_name": "hosts-tep",
                  "subnets": [{"cidr": "10.0.21.0/24",
                               "allocation_ranges": [{"start": "10.0.21.10",
                                                      "end": "10.0.21.49"}]}]}
    POOL_GW = {"display_name": "hosts-tep",
               "subnets": [{"cidr": "10.0.21.0/24",
                            "gateway_ip": "10.0.21.254",
                            "allocation_ranges": [{"start": "10.0.21.10",
                                                   "end": "10.0.21.49"}]}]}
    # The failure this estate already paid for once: hosts on one tunnel subnet,
    # edges on another, and no gateway on the hosts' pool. Everything reports
    # healthy and no host reaches an edge.
    need, why = gateway_needed("10.0.21.11", ["10.0.23.11"], POOL_NO_GW)
    ck("hosts and edges on different tunnel subnets with no pool gateway is caught",
       need is True and "no gateway" in why)
    need, why = gateway_needed("10.0.21.11", ["10.0.23.11"], POOL_GW)
    ck("and is not raised once the pool has one", need is False)
    need, why = gateway_needed("10.0.21.11", ["10.0.21.30"], POOL_NO_GW)
    ck("a shared tunnel subnet needs no gateway", need is False
       and "share a tunnel subnet" in why)
    need, why = gateway_needed("10.0.21.11", [], POOL_NO_GW)
    ck("with no edge endpoints readable it does not invent a verdict",
       need is False)
    need, why = gateway_needed("10.0.21.11", ["10.0.23.11"], {"subnets": []})
    ck("an unreadable pool says so rather than blocking wrongly",
       need is False and "could not read" in why)

    ck("node_teps reads the addresses out of a transport node's state",
       node_teps({"host_switch_states": [
           {"endpoints": [{"ip": "10.0.21.11"}, {}]},
           {"endpoints": [{"ip": "10.0.21.12"}]}]})
       == ["10.0.21.11", "10.0.21.12"])
    ck("and returns nothing for a node that has none - the Not Set case",
       node_teps({"host_switch_states": [{"endpoints": [{}]}]}) == []
       and node_teps({}) == [])

    # --- found live on 9.1.1: the zone is attached and there is no TEP pool.
    # The tool called that "already has overlay" and did nothing, which is the
    # one wrong answer it could give, because the hosts get no TEP address.
    NOPOOL = profile("prof-nopool", [switch("vds-np", ["tz-ovl", "tz-vlan"],
                                            pool=None, uplink_profile=None)])
    rows, new = plan_cluster("cluster-np", NOPOOL, ref_sw, "tz-ovl", "pool-tep",
                             "up-2nic", tzmap, UP2, 40, 4)
    ck("an overlay zone with no TEP pool is not called finished",
       new is not None)
    ck("the pool is added to the switch that already carries the overlay",
       new is not None and sw_pool(switches(new)[0]) == "pool-tep")
    ck("and the zone it already had is not added twice",
       new is not None
       and len(switches(new)[0]["transport_zone_endpoints"]) == 2)
    ck("the output explains what 'TEP IP Address: Not Set' actually meant",
       any("no pool to draw one from" in d for _s, d in rows))

    WITHPOOL = profile("prof-done", [switch("vds-d", ["tz-ovl"], pool="pool-tep",
                                            uplink_profile="up-2nic")])
    rows, new = plan_cluster("cluster-done", WITHPOOL, ref_sw, "tz-ovl", "pool-tep",
                             "up-2nic", tzmap, UP2, 40, 4)
    ck("a cluster with both zone and pool is left alone",
       new is None and rows[0][0] == OK)

    rows, new = plan_cluster("cluster-np", NOPOOL, ref_sw, "tz-ovl", None,
                             "up-2nic", tzmap, UP2, 40, 4)
    ck("no pool anywhere to copy is refused, not silently skipped",
       new is None and any(s == BLOCKED and "--tep-pool" in d or
                           s == BLOCKED and "no TEP pool" in d for s, d in rows))

    # transport nodes that were never read must not read as "none have a TEP"
    ck("an unread node list returns unknown, not zero",
       tep_counts(None, "domain-c1", []) == (None, None))
    ck("a compute collection id matches across its two spellings",
       same_collection("domain-c9:abc-123", "domain-c9")
       and same_collection("domain-c9", "domain-c9:abc-123")
       and not same_collection("domain-c9", "domain-c7")
       and not same_collection("", "domain-c9"))
    ck("node_cluster reads the collection under either field name",
       node_cluster({"compute_collection_id": "domain-c9"}) == "domain-c9"
       and node_cluster({"compute_collection_path": "/x/domain-c9"}) == "/x/domain-c9"
       and node_cluster({}) == "")

    # --- the transport VLAN: NSX already knows which one ------------------
    UP221 = dict(UP2, id="up-221", display_name="tep-221", transport_vlan=221)
    UP999 = dict(UP2, id="up-999", display_name="tep-999", transport_vlan=999)
    ck("the transport VLAN is read off the uplink profile",
       transport_vlan(UP221) == 221 and transport_vlan({"vlan": "17"}) == 17)
    ck("an unreadable or absent VLAN is None, not a guess",
       transport_vlan({}) is None and transport_vlan(None) is None
       and transport_vlan({"transport_vlan": "abc"}) is None)
    ck("VLAN 0 is a real value, not absence",
       transport_vlan({"transport_vlan": 0}) == 0)

    # A pool belongs to one VLAN. Copying one across VLANs allocates addresses
    # that pass no traffic - healthy screens, dead overlay.
    rows, new = plan_cluster("cluster-x", VLANONLY, ref_sw, "tz-ovl", "pool-tep",
                             "up-221", tzmap, UP221, 40, 4, target_uplink=UP999)
    ck("a pool from another transport VLAN is refused",
       new is None and any(s == BLOCKED and "VLAN 999" in d and "VLAN 221" in d
                           for s, d in rows))
    ck("and it says to name the right pool rather than leaving you stuck",
       any("--pool" in d for _s, d in rows))
    rows, new = plan_cluster("cluster-x", VLANONLY, ref_sw, "tz-ovl", "pool-tep",
                             "up-221", tzmap, UP221, 40, 4, target_uplink=UP221)
    ck("the same VLAN on both sides goes ahead", new is not None)
    rows, new = plan_cluster("cluster-x", VLANONLY, ref_sw, "tz-ovl", "pool-tep",
                             "up-221", tzmap, UP221, 40, 4, target_uplink=None)
    ck("an unreadable target VLAN does not block - it is not evidence of a clash",
       new is not None)

    # The policy API lists a pool with no subnets - they are a child resource.
    # Without fetching them every pool read as "no CIDR, no gateway, no size",
    # which is every fact that matters about a TEP pool.
    class _SubPool(Nsx):
        def __init__(self):
            self.calls = []

        def first(self, paths):
            return ({"results": [{"id": "p1", "display_name": "tep",
                                  "path": "/infra/ip-pools/p1"}]}, paths[0])

        def get(self, path, timeout=None):
            self.calls.append(path)
            return {"results": [{"cidr": "10.0.21.0/24",
                                 "gateway_ip": "10.0.21.254",
                                 "allocation_ranges": [
                                     {"start": "10.0.21.10",
                                      "end": "10.0.21.49"}]}]}

    sp = _SubPool()
    got = sp.ip_pools()
    ck("a policy pool has its subnets fetched, not left empty",
       pool_subnets(got[0]) and pool_subnets(got[0])[0]["cidr"] == "10.0.21.0/24")
    ck("and its gateway comes with them",
       pool_subnets(got[0])[0]["gateway_ip"] == "10.0.21.254")
    ck("the child resource is the one asked for",
       any("ip-subnets" in c for c in sp.calls))

    class _InlinePool(_SubPool):
        def first(self, paths):
            return ({"results": [{"id": "p1", "display_name": "tep",
                                  "subnets": [{"cidr": "10.0.9.0/24"}]}]},
                    "/api/v1/pools/ip-pools")

    ip = _InlinePool()
    ck("a manager-API pool is not fetched a second time",
       pool_subnets(ip.ip_pools()[0])[0]["cidr"] == "10.0.9.0/24"
       and ip.calls == [])

    # --- the transport VLAN, and creating the pool for it ------------------
    UPDEF = {"id": "up-default", "display_name": "vcf-generated-profile",
             "path": "/infra/host-switch-profiles/up-default",
             "transport_vlan": 0,
             "teaming": {"active_list": [{"uplink_name": "u1"},
                                         {"uplink_name": "u2"}]}}
    rows, new = plan_vlan("cluster-a", VLANONLY, UPDEF, 222, tzmap)
    ck("a cluster on VLAN 0 is moved to the real transport VLAN",
       new is not None and new["transport_vlan"] == 222)
    ck("and the fetched profile is not edited in place",
       UPDEF["transport_vlan"] == 0)
    rows, new = plan_vlan("cluster-a", VLANONLY, dict(UPDEF, transport_vlan=222),
                          222, tzmap)
    ck("a profile already on that VLAN is left alone",
       new is None and rows[0][0] == OK)
    rows, new = plan_vlan("cluster-a", VLANONLY, dict(UPDEF, transport_vlan=221),
                          222, tzmap)
    ck("moving a profile that already carries live TEP traffic is flagged",
       new is not None and any(s == SKIP and "live tunnels" in d for s, d in rows))
    rows, new = plan_vlan("cluster-a", VLANONLY, UPDEF, 222, tzmap,
                          shared_with=["cluster-b", "cluster-c"])
    ck("a shared uplink profile is refused - it would move their TEP traffic too",
       new is None and any(s == BLOCKED and "cluster-b" in d for s, d in rows))
    rows, new = plan_vlan("cluster-a", VLANONLY, None, 222, tzmap)
    ck("an unreadable uplink profile is reported, not crashed on", new is None)

    EX = [{"display_name": "tep-221", "subnets": [
        {"cidr": "10.0.21.0/24",
         "allocation_ranges": [{"start": "10.0.21.10", "end": "10.0.21.49"}]}]}]
    rows, go = plan_pool("tep-222", "10.0.22.0/24", "10.0.22.10", "10.0.22.49",
                         "10.0.22.254", EX)
    ck("a well-formed pool is planned", go is True)
    rows, go = plan_pool("tep-222", "10.0.22.0/24", "10.0.22.10", "10.0.22.49",
                         "10.0.22.20", EX)
    ck("a gateway inside the range is refused - a host would be handed it",
       go is None and any("inside the allocation range" in d for _s, d in rows))
    rows, go = plan_pool("tep-222", "10.0.22.0/24", "10.0.22.10", "10.0.99.49",
                         "10.0.22.254", EX)
    ck("a range that leaves the subnet is refused", go is None)
    rows, go = plan_pool("tep-222", "10.0.22.0/24", "10.0.22.49", "10.0.22.10",
                         "10.0.22.254", EX)
    ck("a backwards range is refused", go is None)
    rows, go = plan_pool("tep-222", "10.0.22.0/24", "10.0.22.10", "10.0.22.49",
                         "10.0.99.254", EX)
    ck("a gateway outside the subnet is refused", go is None)
    rows, go = plan_pool("tep-221", "10.0.21.0/24", "10.0.21.60", "10.0.21.99",
                         "10.0.21.254", EX)
    ck("a pool name that already exists is not created twice", go is None)
    rows, go = plan_pool("tep-221b", "10.0.21.0/24", "10.0.21.60", "10.0.21.99",
                         "10.0.21.254", EX)
    ck("a second pool on an existing subnet is allowed but warned about",
       go is True and any(s == SKIP and "overlaps" in d for s, d in rows))
    rows, go = plan_pool("tep-222", "10.0.22.0/24", "10.0.22.10", "10.0.22.49",
                         None, EX)
    ck("no gateway is allowed, and says what it costs if the edges are elsewhere",
       go is True and any(s == SKIP and "reach none of them" in d
                          for s, d in rows))
    ck("overlaps finds a pool sharing the subnet and names its cidr",
       overlaps("10.0.21.0/24", EX) and "10.0.21.0/24" in overlaps(
           "10.0.21.0/24", EX)[0])
    ck("and finds none on a different subnet", overlaps("10.0.99.0/24", EX) == [])

    # --- the pool gateway, the fix for what verify finds -------------------
    rows, new = plan_gateway(POOL_NO_GW, "10.0.21.254")
    ck("a gateway outside the allocation range is accepted",
       new is not None and pool_subnets(new)[0]["gateway_ip"] == "10.0.21.254")
    ck("and the fetched pool is not edited in place",
       "gateway_ip" not in pool_subnets(POOL_NO_GW)[0])
    rows, new = plan_gateway(POOL_NO_GW, "10.0.21.20")
    ck("a gateway inside the allocation range is refused - a host would be given it",
       new is None and any(s == BLOCKED and "allocation range" in d for s, d in rows))
    rows, new = plan_gateway(POOL_NO_GW, "10.0.99.254")
    ck("a gateway outside every subnet of the pool is refused",
       new is None and any(s == BLOCKED and "not inside any subnet" in d
                           for s, d in rows))
    rows, new = plan_gateway(POOL_GW, "10.0.21.254")
    ck("a pool that already has that gateway is left alone",
       new is None and rows[0][0] == OK)
    rows, new = plan_gateway(POOL_GW, "10.0.21.253")
    ck("a different gateway is a change, and the old one is named",
       new is not None and any("change 10.0.21.254 to" in d for _s, d in rows))
    rows, new = plan_gateway(None, "10.0.21.254")
    ck("a pool that does not exist is reported, not crashed on", new is None)
    rows, new = plan_gateway({"display_name": "empty", "subnets": []}, "10.0.21.254")
    ck("a pool with no subnets is refused", new is None)

    ck("a switch with a pool reports pool", sw_assignment(
       {"ip_assignment_spec": {"resource_type": "StaticIpPoolSpec",
                               "ip_pool_id": "p1"}}) == ("pool", "p1"))
    ck("a switch set to DHCP is not reported as having nothing",
       sw_assignment({"ip_assignment_spec":
                      {"resource_type": "AssignedByDhcp"}}) == ("dhcp", None))
    ck("a switch with no assignment at all is 'none' - a different fix entirely",
       sw_assignment({}) == ("none", None))
    ck("an assignment kind we do not know is named, not silently dropped",
       sw_assignment({"ip_assignment_spec":
                      {"resource_type": "AssignedByAutoConf"}})
       == ("other", "AssignedByAutoConf"))

    ck("ip_span counts a range inclusively",
       ip_span("10.0.170.10", "10.0.170.19") == 10)
    ck("ip_span is 0 on a reversed or malformed range",
       ip_span("10.0.0.5", "10.0.0.1") == 0 and ip_span("x", "y") == 0
       and ip_span("10.0.0.1", None) == 0)

    ck("result ranks blocked above add",
       Result("x").add(ADD, "a").add(BLOCKED, "b").state == BLOCKED)

    print("\n%d checks, %d failed" % (len(fails) + _passed[0], len(fails)))
    return 1 if fails else 0


_passed = [0]
_real_print = print


def _counting_print(*a, **k):
    if a and isinstance(a[0], str) and a[0].startswith("PASS "):
        _passed[0] += 1
    _real_print(*a, **k)


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description="Put the overlay onto clusters a VCF import left VLAN-only.")
    ap.add_argument("command",
                    choices=["build", "survey", "apply", "verify", "vlan",
                             "pool", "gateway", "realize", "diag", "example",
                             "selftest"])
    ap.add_argument("--nsx", help="NSX Manager FQDN")
    ap.add_argument("--user", default="admin", help="NSX user (default admin)")
    ap.add_argument("--reference", help="a cluster whose overlay already works")
    ap.add_argument("--cluster", action="append", default=[],
                    help="a cluster to give the overlay to; repeatable")
    ap.add_argument("--commit", action="store_true",
                    help="make the change. Without it nothing is written")
    ap.add_argument("--pool", help="gateway: the TEP pool to set it on. "
                                   "apply: use this pool instead of the "
                                   "reference's, for a tunnel VLAN per domain")
    ap.add_argument("--gateway", help="gateway/pool: the address the leaves hold "
                                      "in that subnet. Never guessed")
    ap.add_argument("--vlan", type=int, help="vlan: the transport VLAN to set")
    ap.add_argument("--name", help="pool: the new pool's name")
    ap.add_argument("--cidr", help="pool: the subnet, e.g. 10.0.22.0/24")
    ap.add_argument("--range", dest="range_", help="pool: start-end")
    ap.add_argument("--config", help="build: the yaml holding the end state")
    ap.add_argument("--outdir", default="/tmp/nsx_prep")
    args = ap.parse_args()

    if args.command == "selftest":
        global print
        print = _counting_print
        return selftest()

    if args.command == "example":
        sys.stdout.write(CONFIG_EXAMPLE)
        return 0

    cfg = None
    if args.command == "build":
        if not args.config:
            die("build needs --config. Start from: python3 %s example > nsx.yaml"
                % os.path.basename(__file__))
        if not os.path.exists(args.config):
            die("config not found: %s" % args.config)
        cfg = load_config(args.config)
        args.nsx = args.nsx or cfg.get("nsx")
        if cfg.get("user") and args.user == "admin":
            args.user = str(cfg["user"])
    if not args.nsx:
        die("--nsx is required")
    outdir = os.path.join(args.outdir, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(outdir, exist_ok=True)
    sys.stdout = Tee(os.path.join(outdir, "console.txt"))

    print("nsx_prep.py v%s   %s   %s" % (
        VERSION, args.nsx,
        "COMMIT - changes will be made" if args.commit
        else "PLAN - nothing will be changed"))
    print("evidence: %s" % outdir)
    print()

    # Argument mistakes are found before the password is asked for, not after.
    if args.command == "vlan" and not (args.cluster and args.vlan is not None):
        die("vlan needs --cluster (repeatable) and --vlan")
    if args.command == "pool" and not (args.name and args.cidr and args.range_):
        die("pool needs --name, --cidr and --range. --gateway too, unless the "
            "edges share this tunnel subnet")
    if args.command == "gateway" and not (args.pool and args.gateway):
        die("gateway needs --pool and --gateway. verify names the pool; the "
            "address is the one the leaves hold in that subnet")
    if args.command == "apply":
        if not args.reference:
            die("--reference is required: the cluster whose overlay config to copy. "
                "Run survey to find one")
        if not args.cluster:
            die("--cluster is required at least once")

    password = os.environ.get("NSXPW")
    if not password:
        if not sys.stdin.isatty():
            die("no terminal to ask for the password on. Set NSXPW in the "
                "environment, or run this from a shell")
        try:
            password = getpass.getpass(
                "      password for %s on %s: " % (args.user, args.nsx))
        except (EOFError, KeyboardInterrupt):
            die("no password given")
    try:
        nsx = Nsx(args.nsx, args.user, password)
    except RuntimeError as e:
        die(str(e))

    if args.command == "build":
        res = run_build(nsx, cfg, args.commit)
        print("==== %s" % ("result" if args.commit else "plan"))
        print("  %s" % res.summary)
        print()
        if res.state == BLOCKED:
            print("NEXT: clear the blocked items, then run the same command again - "
                  "it does only what is left")
            return 1
        if not args.commit:
            print("NEXT: python3 %s build --config %s --commit"
                  % (os.path.basename(__file__), args.config))
            return 2
        print("NEXT: the overlay is up. The segments should be in the vCenters' "
              "networking inventory")
        return 0

    if args.command == "realize":
        if not args.cluster:
            die("realize needs --cluster (repeatable)")
        res = run_realize(nsx, args.cluster, args.commit)
        print("==== %s" % ("result" if args.commit else "plan"))
        for state, detail in res.rows:
            print("  %-7s %s" % (state.upper(), detail))
        print()
        if res.state == BLOCKED:
            return 1
        if not args.commit:
            print("NEXT: the same command with --commit")
            return 2
        return 0

    if args.command == "diag":
        return diag(nsx)

    if args.command == "survey":
        snapshot = cluster_profiles(nsx)
        survey(nsx, found=snapshot)
        fabric(nsx, found=snapshot)
        print("NEXT: pick a cluster with overlay=yes as --reference, then")
        print("      python3 nsx_prep.py apply --nsx %s --reference <that> "
              "--cluster <one missing it>" % args.nsx)
        return 0

    if args.command == "vlan":
        res = run_vlan(nsx, args.cluster, args.vlan, args.commit)
        print("==== %s" % ("result" if args.commit else "plan"))
        for state, detail in res.rows:
            print("  %-7s %s" % (state.upper(), detail))
        print()
        if res.state == BLOCKED:
            return 1
        if not args.commit and res.state in (ADD, CHANGE):
            print("NEXT: the same command with --commit")
            return 2
        print("NEXT: point the cluster at the pool for that VLAN with apply --pool")
        return 0

    if args.command == "pool":
        res = run_pool(nsx, args.name, args.cidr, args.range_, args.gateway,
                       args.commit)
        print("==== %s" % ("result" if args.commit else "plan"))
        for state, detail in res.rows:
            print("  %-7s %s" % (state.upper(), detail))
        print()
        if res.state == BLOCKED:
            return 1
        if not args.commit and res.state in (ADD, CHANGE):
            print("NEXT: the same command with --commit")
            return 2
        return 0

    if args.command == "gateway":
        res = run_gateway(nsx, args.pool, args.gateway, args.commit)
        print("==== %s" % ("result" if args.commit else "plan"))
        for state, detail in res.rows:
            print("  %-7s %s" % (state.upper(), detail))
        print()
        if res.state == BLOCKED:
            return 1
        if not args.commit and res.state in (ADD, CHANGE):
            print("NEXT: the same command with --commit")
            return 2
        print("NEXT: python3 nsx_prep.py verify --nsx %s" % args.nsx)
        return 0

    if args.command == "verify":
        res = verify(nsx, args.cluster or None)
        print("==== verify")
        for state, detail in res.rows:
            print("  %-7s %s" % (state.upper(), detail))
        print()
        if res.state == BLOCKED:
            print("NEXT: clear the blocked items above. An overlay zone that is "
                  "applied but has no tunnels carries no traffic")
            return 1
        print("NEXT: the overlay is up. The segments should be in the vCenter's "
              "networking inventory")
        return 0

    res = run_apply(nsx, args.reference, args.cluster, args.commit, args.pool)
    print("==== %s" % ("result" if args.commit else "plan"))
    for state, detail in res.rows:
        print("  %-7s %s" % (state.upper(), detail))
    print()
    if res.state == BLOCKED:
        print("NEXT: clear the blocked items above, then run again")
        return 1
    if not args.commit and res.state in (ADD, CHANGE):
        print("NEXT: the same command with --commit")
        return 2
    if args.commit:
        print("==== verify")
        print("  waiting 30s for NSX to realize the change on the hosts")
        time.sleep(30)
        vres = verify(nsx, args.cluster)
        for state, detail in vres.rows:
            print("  %-7s %s" % (state.upper(), detail))
        print()
        if vres.state == BLOCKED:
            print("NEXT: the profile took but the overlay is not carrying traffic "
                  "yet. Clear the blocked items; TEPs can take a few minutes, so "
                  "re-run: python3 nsx_prep.py verify --nsx %s %s"
                  % (args.nsx, " ".join("--cluster %s" % c for c in args.cluster)))
            return 1
        print("NEXT: overlay up. The segments should now be in the vCenter's "
              "networking inventory")
        return 0
    print("NEXT: watch TEP IP Address populate in NSX > System > Fabric > Hosts > "
          "Clusters, then the segments appear in vCenter")
    return 0


if __name__ == "__main__":
    sys.exit(main())
