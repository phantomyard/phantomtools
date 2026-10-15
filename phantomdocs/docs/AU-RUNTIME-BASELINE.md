# AU runtime baseline: operator inventory of 2026-10-12

Source: `salvaalba-dev/aquaponics-deploy`, commit
`6a8da867e3b28c5bcc7db84555a6531fc3d52f82`,
`inventario/au-pipeline-inventario-20261012.txt`, observed at
`2026-10-12T21:00:52Z`. The operator collected this inventory on the VPS;
development did not access the VPS. This inventory supersedes earlier C7 byte
pins; it does not prove compatibility of changed scripts with reviewed interfaces.

Two additive examples record selected non-secret deployment facts:

- `examples/workflows/au-tool-bindings.inventory-20261012.json` records full
  interpreter and transcription/summary/renderer hashes. The indexer remains
  unpinned and inactive pending operator verification.
- `examples/workflows/au-runtime-baseline.inventory-20261012.json` records
  selected prompt/branding/FFmpeg resources, the ffprobe pin and outstanding
  blockers. It is a provenance report, not a processing contract. Copy reviewed
  resources and probe fields into an operator-selected processing profile.

The original `au-installation.json` still has an empty tool map. Neither example
activates processing, changes timers, edits PhantomOrg or enables a live inbox.
Run read-only inventory on the candidate with the supplied tools overlay:

```bash
pd workflow inventory \
  --installation "$CANDIDATE_INSTALLATION" \
  --tools-file "$TOOLS_INVENTORY_20261012" \
  --processing-profile "$CANDIDATE_PROCESSING_PROFILE" > "$RUNTIME_REPORT"
```

Matching byte hashes do not fill these deployment gaps:

1. Choose and verify a private processing root and the PhantomDocs executable.
2. Record actual Node/Chrome executable paths and byte pins used by the renderer.
3. Verify which Whisper snapshot `--model turbo` selects as the processor user.
   The report lists a root-owned Hugging Face cache and only large model blobs;
   it omits small configuration/tokenizer files. The reported `refs/main` value
   is a revision ID, not a SHA-256 file pin. Do not assume the processor uses or
   can read that cache, download a different model silently, or loosen permissions.
4. Verify service User/Group separately: the supplied service excerpt contains
   ExecStart but does not establish those identities. Read-only examples:

   ```bash
   systemctl show post-meeting.service -p User -p Group -p ExecStart
   sudo -u pb-pepa test -r /opt/whisper-transcribe/transcribe.py
   sudo -u pb-pepa test -x /opt/whisper-transcribe/venv/bin/python
   ```

5. Review changed script interfaces and dependencies, external summary disclosure
   and rendering quality with an isolated authorized trial before switching timers.

No credential contents or hashes are included. Reported package versions belong
to the inspected environment; they do not prove the complete environment used by
a new processor instance. Diarization, minutes generation and the upload monolith
are not bound by these examples. Non-meeting AU routing remains undeclared.
