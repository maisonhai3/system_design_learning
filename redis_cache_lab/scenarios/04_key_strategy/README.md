# Key strategy — a key name is an API between services that never agreed to have one

**The claim you are practising:** *"I namespace keys as
`service:schema_version:entity:id:projection`. Every segment prevents a specific
failure I can name."*

## The convention

```
identity:v1:user:42:profile
└──┬───┘ └┬┘ └──┬──┘ └──┬───┘
   │      │     │       └── projection: which view of the entity
   │      │     └────────── entity:id, in that order
   │      └──────────────── schema version of the VALUE's shape
   └─────────────────────── owning service — one team owns this prefix
```

## What each segment prevents

### `service` — the collision

One Redis cluster serves many services. Two teams independently pick the
shortest name that makes sense to them:

```
identity service:  SET user:1 "admin"
billing service:   SET user:1 '{"plan":"pro","seats":12}'
identity service:  GET user:1 → '{"plan":"pro","seats":12}'
```

Neither team did anything wrong in isolation, and nobody reviews the other
team's MRs. **Redis reports no error** — overwriting a key with a different
shape is not an error condition, it is what `SET` is for.

Best case, the parse raises and you get a 500 pointing at a line that is not the
bug. Worst case both sides store strings, the parse succeeds, and one service
authorises using another service's data.

The prefix also buys you two things you will want later: `--scan --pattern
'identity:*'` becomes a meaningful question, and when the cluster fills up you
can attribute memory to a team.

### `v1` — the rolling deploy

This is the segment people leave out, and it is invisible until the first deploy
after you add a field to a cached value. For ten minutes, old pods and new pods
are both live and both writing:

```
old pod: SET identity:user:1:profile '{"role":"guest"}'
new pod: GET identity:user:1:profile → KeyError: 'org_id'
```

Intermittent **by design**: it happens only while both generations are live and
stops right around the time you finish reading the logs.

With the version in the prefix, the two generations write to different keys and
never meet. The old keys are not migrated, backfilled or deleted — they simply
stop being read and expire. **That is the cheapest data migration available to
you, and it exists only because the version is in the key rather than inside the
value.**

### `projection` — the write amplification

One key for the whole user looks tidy:

```json
identity:v1:user:1 = {"role": "guest", "display_name": "Alice"}
```

Then a display-name change — a cosmetic write — invalidates the role that every
authorization check needs. The lab measures it: **1 database read after the
combined-key write, 0 after the split-key write.** Multiply by your write rate.
A cache that converts cosmetic writes into authorization load is worse than no
cache, because it also hides the cause.

The rule: **one key per (entity, projection)**, where a projection is a set of
fields read together *and* invalidated together. If two fields are invalidated by
different writes, they are two keys.

### The rule with no segment: no PII in keys

`identity:v1:user:42:profile`, never `identity:v1:user:alice@acme.test`.

Key names leak into `MONITOR` output, `SLOWLOG`, `--scan` dumps, latency reports
and error messages — all places with looser access control than the data itself.
The value is protected. The key name usually is not.

## What a key costs, measured

The lab measures this on your own machine with `MEMORY USAGE`:

```
'i:1:u:1:p'                                                             72 bytes
'identity-service:cache:version-1:entities:user:1:projections:profile' 136 bytes
10 million keys, difference:                     610 MB of RAM, storing names
```

This is not an argument for cryptic keys — a debuggable key name is worth real
money, and at 10k keys the difference is noise. It is an argument for knowing the
number, and for spending the characters on segments that prevent failures rather
than on decoration.

## Run it

```bash
./lab.sh run 04
./lab.sh keys        # see the keyspace with TTLs after any scenario
```

## The interview answer

> *"How do you name your cache keys?"*

"`service:schema_version:entity:id:projection`. The service prefix stops two
teams sharing a cluster from silently overwriting each other and makes per-team
memory measurable. The schema version is the shape of the value — during a
rolling deploy old and new pods are both writing, and without it new code parses
old values; bumping it is also the cheapest cache migration there is, because the
old keys just expire. The projection is separate because fields invalidated by
different writes belong in different keys — otherwise a display-name change
evicts the role that every request needs. And no PII in key names, because keys
show up in MONITOR, SLOWLOG and error messages, which are less protected than the
data."
