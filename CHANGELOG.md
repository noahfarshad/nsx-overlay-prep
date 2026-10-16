# Changelog

All notable changes to this project are documented here.

## [1.0.0] — 2026-10-16

### Added

- Initial public release. `build` declares the end state in one file and converges a VCF-imported cluster to a working overlay: the TEP pools, each cluster's transport VLAN and pool, the overlay zone and uplink profile copied from a reference cluster, host preparation, and verification from the host up. `survey`, `apply`, `verify`, `vlan`, `pool`, `gateway`, `realize` and `diag` do one piece each. Plan by default; `--commit` writes.
- `selftest` (89 checks) and an end-to-end suite (79 checks) against a stub shaped like the real NSX.

### Notes

- Every name, address and VLAN in the example and the tests is a placeholder.
- The TEP pool gateway is never guessed; it has to be given.
- The tool's own version string is 1.0.
