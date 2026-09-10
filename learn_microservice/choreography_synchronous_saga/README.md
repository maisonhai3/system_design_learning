# A saga you can watch unwind (sync choreography)

Four services, each owns one provisioning step and its compensation. No
orchestrator: each service knows ONLY its downstream neighbour's URL.

```
client ─▶ vm ─▶ disk ─▶ ip ─▶ dns
         [1]   [2]     [3]   [4]
 undo:  dealloc detach release  —
```

Run: `bash run.sh`

- alpha: all four steps succeed → PROVISIONED
- beta:  reuses alpha's hostname → DNS rejects at step 4 → steps 3,2,1 compensate
  in REVERSE order, then the client hears ROLLED_BACK. Final state proves beta
  leaked nothing and alpha was never touched.

The gem: nobody coded "compensation order." It's just the call stack unwinding.
