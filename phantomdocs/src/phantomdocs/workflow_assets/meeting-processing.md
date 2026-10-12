# Meeting processing protocol 1.0.0

Validate the authorized recording, preserve its original reference, transcribe
and summarize using installed capabilities, and create separate branded results.
Use WorkflowExecutor.prepare to stop before the terminal index step. Export the
processed-results manifest with byte hashes, roles and lineage. Do not upload,
assign archive sequences, rename or delete originals in a processing adapter.

An independent archiver assigns operational names and publishes derivatives to
the explicitly authorized archive. It returns final Drive file IDs in a receipt
bound to this plan and results hash. The archive consumer independently validates
folder membership and bytes before indexing existing references. A local workflow
identifier is not a Drive operation-ID API. Never infer security policy from file
names; never treat MD5 alone as authenticated request provenance.

The terminal index declaration describes the downstream dependency; prepare does
not invoke it. No minutes or non-meeting routes are synthesized by this protocol.
