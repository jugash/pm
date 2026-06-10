# Scenario reference

Scenario files are YAML with two top-level keys: `scenarios` (literal list)
and `matrices` (cartesian expansion). `perfbench validate <path>` checks a
file or directory; every error message carries the dotted path of the
offending node.

## Scenario schema

```yaml
scenarios:
  - id: bm-onload-pp            # ^[a-z0-9][a-z0-9_.-]*$ , unique
    description: free text
    platform: baremetal         # baremetal | k8s
    network_path: onload        # kernel | onload | efvi
    nic:
      bond_mode: none           # none | active_backup | lacp_8023ad
      bond_name: bond0          # required when bonded
      vendor: "0x1924"          # expected PCI vendor id (0x1924 = Solarflare)
      ports:                    # >= 1; >= 2 when bonded
        - card: x2522-a         # free-text card label
          numa_node: 0          # NUMA filter for vendor-based resolution
          # name: ens1f0        # optional: pin an explicit per-host name
          # pci: "0000:3b:00.0" # optional: pin an exact slot
    cpu:
      client_cores: [4]         # cores for the client-side tool process
      server_cores: [4]         # cores on the server host
      irq_cores: [2]            # where NIC IRQs are steered (must not overlap client)
      numa_node: 0
      require_isolated: true    # preflight: cores must be in isolcpus
    onload:                     # only when network_path != kernel
      profile: latency          # onload --profile=...  (null to omit)
      env:                      # EF_* environment, stringified
        EF_POLL_USEC: "100000"
        EF_INT_DRIVEN: "0"
    tools:                      # >= 1; shorthand string form allowed
      - name: sockperf
        params: { mode: under-load, msg_size: 64, mps: 100000, duration_s: 30 }
      - iperf3                  # equivalent to {name: iperf3, params: {}}
    repetitions: 5              # >= 1, one RunRecord each
    tags: [core-comparison]
```

**Don't trust interface names.** `ens1f0` on one machine is `enp59s0f0` on
another, and predictable names can drift after BIOS/firmware changes. Two
mechanisms deal with this:

*Resolution* — port `name` is optional. Before a run, the harness resolves
the real interface name **independently on each host** (client and server
may legitimately differ): an exact match when `pci` is declared, otherwise
the first unused interface whose PCI vendor matches `nic.vendor`
(defaulting to Solarflare `0x1924` for onload/efvi scenarios), filtered by
`numa_node` when set. Resolution is deterministic (name-sorted), enriches
the run record with the discovered pci/numa facts, and fails loudly with
the host's NIC inventory when nothing matches. A name-free port must be
resolvable: it needs `pci`, or `nic.vendor`, or a bypass network path.

*Verification* — whether named explicitly or resolved, preflight's
`nic_identity` check confirms the interface's PCI vendor id matches and,
when `pci` is declared, that the name still points at that slot.

Inspect a host's inventory with:

```bash
perfbench nics --transport ssh --client-host lhr-lab-01 --ssh-user jugash

# ssh:jugash@lhr-lab-01
name    driver  vendor  device  pci           numa_node  solarflare
------  ------  ------  ------  ------------  ---------  ----------
ens1f0  sfc     0x1924  0x0b03  0000:3b:00.0  0          yes
ens1f1  sfc     0x1924  0x0b03  0000:3b:00.1  0          yes
eno1    i40e    0x8086  0x37d2  0000:18:00.0  0
```

With vendor-based resolution you usually don't copy anything — the shipped
examples are name-free and portable across host pairs. Copy `pci` when you
must pin exact slots (e.g. dual-card bonds where two ports of one card must
not be picked).

Validation rules worth knowing:

- `network_path: kernel` with an `onload:` block is rejected (meaningless).
- `network_path: onload` without an `onload:` block gets the default
  (`profile: latency`, no env).
- `eflatency` only runs in `network_path: efvi` scenarios.
- Unknown keys anywhere are rejected — typos fail loudly at validate time,
  not silently at 2 a.m. in a benchmark campaign.

## Matrices

```yaml
matrices:
  - id_template: "bm-{network_path}-{nic.bond_mode}"
    base:
      # a full scenario document, minus `id`
      ...
    axes:
      network_path: [kernel, onload]
      nic.bond_mode: [none, active_backup]
      onload.env.EF_INT_DRIVEN: ["0", "1"]
      cpu.client_cores.0: [2, 8]        # integer segments index lists
```

Each combination of axis values is deep-copied onto `base`, the id is
rendered from the template (`{path}` tokens are slugified values), and the
result is validated like any literal scenario.

Rules:

- `id_template` paths must exist in the final document — if you template on
  `nic.bond_mode`, set it explicitly in `base` or in an axis.
- The template must make ids unique across the expansion; duplicates are
  rejected at load time.
- Axis paths use dots; numeric segments index lists
  (`nic.ports.0.numa_node`).

## Labels

Every scenario contributes four canonical labels to all of its measurements
and metrics: `scenario`, `platform`, `network_path`, `bond`. These are the
slicing dimensions in Grafana — design your ids and matrices so these four
tell the story.
