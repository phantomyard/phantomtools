# G1 per-host acceptance record

Create one private record per authorized content host. Do not commit real
hostnames, addresses, personas, human contacts, store paths, or credentials to
this repository. An unreachable host is an explicit exclusion. Partial host
coverage is a partial claim, not a global G1 pass.

| Evidence | Required result |
| --- | --- |
| A0 inventory and owner declaration | Exact digests recorded; every boundary, workload and store on this host assigned or excluded; mixed stores marked `requires_split` cannot pass. |
| Producer build | Reviewed projection policy pins the source and declaration; `po build-boundary` emits a fresh candidate; raw compiler output is scanned for cross-boundary canaries. |
| Service identities | One non-admin OS identity per workload; no identity switching, shared writable escape, broad sudo, container/hypervisor socket, or broad connector grant. |
| Stores | Separate per-boundary persona, memory, KB, vault and credentials paths; cross-boundary read/write attempts by each workload identity denied and logged. |
| Runtime adapters | Unconverted inbox, Drive, ERP, email, bridge and tool adapters disabled or confined to the declared boundary. |
| Maintenance surfaces | Gateway/API endpoints, agent sockets, approvals and remote-access interfaces inventoried. For each privileged surface on this host, test and record both route denial and credential denial from every workload identity; record rationale for other surfaces. |
| Prompt and tools | G1 producer candidate is inspected now; installed bundle, signature verifier, prompt assembly and tool-call canaries are required as those components land, no later than G2. |
| Rollback | Previous service state and stores can be restored without reintroducing mixed-boundary mounts or credentials. |

Record the test command, timestamp, observed result, operator, evidence
location, and digest for each applicable row. A host passes only when its
declared workload identities cannot read or write data outside their
policy-assigned boundary scope and cannot reach privileged maintenance
surfaces. G1 does **not** provide per-principal record isolation when two
personas share one boundary store; report this explicitly and deliver it at G2.

Production placement, nested-virtualization proof, fresh capacity measurement,
signature verification, consumer lock, and out-of-band anchor provisioning are
G2/G3 work. None is asserted by the G1 candidate manifest. If the owner has
not authorized this host or the content-host inventory is incomplete, leave
its G1 status `not accepted` and list the uncovered host or stores.
