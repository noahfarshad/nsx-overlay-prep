# Overlay for imported clusters

Put working overlay onto clusters a VCF import left VLAN-only: the transport VLAN, the TEP pool, the overlay transport zone, host preparation and the verification, copied from a reference cluster whose overlay already works. Plan by default. Nothing is written without `--commit`.

License: GPL-3.0. Built and proved out for [essential.coach](https://essential.coach).
Full write-up: [Prepared Is Not Working: The Overlay a VCF Import Leaves Out](https://essential.coach/prepared-is-not-working-nsx-overlay-after-vcf-import/)

A vCenter imported into VCF as a workload domain has its clusters prepared for NSX VLAN-backed networking and nothing else: no overlay transport zone, so no TEP, no tunnels, and none of the site's existing overlay segments reach the new cluster. That is expected behaviour ([KB 444225](https://knowledge.broadcom.com/external/article/444225)), and the remedy it gives is to edit the transport node profile by hand, once per cluster. Six clusters is six chances to mistype a teaming policy on a live fabric, so this does it from the configuration that already works instead. Nothing is invented: if the reference cluster has no overlay transport zone, it says so and stops.

## Run

```bash
python3 nsx_prep.py selftest                              # 89 checks, no network
python3 nsx_prep.py example > nsx.yaml                    # the end state: manager, reference cluster, pools, clusters
python3 nsx_prep.py survey --nsx nsx01.example.com        # every cluster NSX knows about and where it stands; changes nothing
python3 nsx_prep.py build --config nsx.yaml               # plan
python3 nsx_prep.py build --config nsx.yaml --commit      # missing pools, the VLANs, each cluster's pool, then verify
```

`build` runs in order and skips whatever is already right, so a run that failed halfway is finished by running it again. There is no state file: every phase reads the live system and plans against it. The other commands do one piece each: `apply` puts the reference's zone and uplink profile on the clusters you name; `verify` reads TEP addresses, tunnel state from host to edge, and the pool gateway behind them, because an applied overlay zone is not a working overlay; `vlan` sets a cluster's transport VLAN and refuses an uplink profile shared with clusters you did not name; `pool` creates a TEP pool and checks the range and gateway sit inside the subnet, that the gateway is outside the range, and warns when another pool already serves that subnet; `gateway` puts a gateway on a pool that has none; `realize` re-runs host preparation on the clusters you name; `diag` dumps what the manager and policy APIs say about the transport nodes, for the next surprise.

The one input never guessed is a TEP pool's gateway: it is the leaves' address in that subnet, and without it the hosts reach no edge on another tunnel subnet while every screen reads healthy. `topology.txt` is the object model and the five places it breaks.

The NSX password is asked for by name and never written anywhere. Run output goes to `/tmp/nsx_prep/<timestamp>/console.txt`, which holds no credentials. Standard library only; PyYAML is used when it is installed.

## Tests

```bash
python3 nsx_prep.py selftest
cd tests && python3 test_nsx_prep_e2e.py
```

89 checks on the planners, the guards and the verification, and 79 end-to-end checks that drive `survey` and the apply orchestration against a stub shaped like the real NSX.
